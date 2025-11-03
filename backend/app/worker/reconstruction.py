import SimpleITK as sitk
import numpy as np
from skimage import measure
from skimage.filters import threshold_otsu
from scipy import ndimage as ndi
import trimesh
from app.models.reconstruction import Reconstruction
from app.utils.storage import storage_client
from app.core.config import settings
from sqlalchemy.orm import Session
import io
import tempfile
import os
import logging
import subprocess
from collections import defaultdict
import pydicom

logger = logging.getLogger(__name__)


def is_same_protocol(ds_a, ds_b):
    """시리즈 프로토콜이 동일한지 확인 (로컬라이저 제외)"""
    def get(ds, k):
        val = getattr(ds, k, None)
        if k == 'PixelSpacing':
            return tuple(val) if val is not None else ()
        return val
    
    keys_to_check = ['SeriesInstanceUID', 'Rows', 'Columns', 'PixelSpacing', 'ImageType']
    
    # SeriesInstanceUID 체크
    if get(ds_a, 'SeriesInstanceUID') != get(ds_b, 'SeriesInstanceUID'):
        return False
    
    # 로컬라이저 제외
    image_type_a = str(get(ds_a, 'ImageType') or '').upper()
    image_type_b = str(get(ds_b, 'ImageType') or '').upper()
    if 'LOCALIZER' in image_type_a or 'SCOUT' in image_type_a:
        return False
    if 'LOCALIZER' in image_type_b or 'SCOUT' in image_type_b:
        return False
    
    # Rows, Columns, PixelSpacing 체크
    return (get(ds_a, 'Rows') == get(ds_b, 'Rows') and
            get(ds_a, 'Columns') == get(ds_b, 'Columns') and
            get(ds_a, 'PixelSpacing') == get(ds_b, 'PixelSpacing'))


def group_stacks_by_orientation(dicom_paths, cos_eps=1e-3):
    """
    DICOM 파일들을 각도(Orientation)별로 스택으로 자동 분류
    SeriesInstanceUID, Rows/Columns/PixelSpacing도 함께 고려
    """
    by_for = defaultdict(list)
    
    for dicom_path in dicom_paths:
        try:
            ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)
            for_uid = getattr(ds, 'FrameOfReferenceUID', None)
            if for_uid:
                by_for[for_uid].append((dicom_path, ds))
            else:
                by_for['default'].append((dicom_path, ds))
        except Exception as e:
            logger.warning(f"Failed to read DICOM metadata from {dicom_path}: {e}")
            continue
    
    stacks = []
    
    for for_uid, items in by_for.items():
        # SeriesInstanceUID + 프로토콜별로 먼저 분리
        protocol_groups = defaultdict(list)
        for f, ds in items:
            series_uid = getattr(ds, 'SeriesInstanceUID', 'unknown')
            protocol_key = (series_uid, 
                          getattr(ds, 'Rows', None),
                          getattr(ds, 'Columns', None),
                          tuple(getattr(ds, 'PixelSpacing', ()) or []))
            protocol_groups[protocol_key].append((f, ds))
        
        # 각 프로토콜 그룹 내에서 orientation 기반 클러스터링
        for protocol_key, protocol_items in protocol_groups.items():
            groups = []
            
            for f, ds in protocol_items:
                # 로컬라이저 제외
                image_type = str(getattr(ds, 'ImageType', '') or '').upper()
                if 'LOCALIZER' in image_type or 'SCOUT' in image_type:
                    logger.info(f"Skipping LOCALIZER/SCOUT: {os.path.basename(f)}")
                    continue
                
                if not hasattr(ds, 'ImageOrientationPatient') or ds.ImageOrientationPatient is None:
                    continue
                
                try:
                    u = np.array(ds.ImageOrientationPatient[:3], dtype=float)
                    v = np.array(ds.ImageOrientationPatient[3:], dtype=float)
                    
                    n_cross = np.cross(u, v)
                    n_norm = np.linalg.norm(n_cross)
                    if n_norm < 1e-6:
                        continue
                    n = n_cross / n_norm
                    
                    placed = False
                    for g in groups:
                        if abs(np.dot(n, g['n'])) > 1 - cos_eps:
                            g['files'].append((f, ds))
                            placed = True
                            break
                    
                    if not placed:
                        groups.append({'n': n, 'files': [(f, ds)]})
                        
                except Exception as e:
                    logger.warning(f"Error processing orientation for {os.path.basename(f)}: {e}")
                    continue
            
            stacks.extend([g['files'] for g in groups])
            
            # orientation 정보가 없는 파일들도 별도 스택으로 추가 (프로토콜별로)
            files_without_orientation = [(f, ds) for f, ds in protocol_items 
                                        if not hasattr(ds, 'ImageOrientationPatient') or ds.ImageOrientationPatient is None]
            if files_without_orientation:
                stacks.append(files_without_orientation)
    
    logger.info(f"Grouped {len(dicom_paths)} files into {len(stacks)} stack(s)")
    return stacks


def read_volume_sorted(stack_files, keep_original_spacing=True):
    """
    스택 내 파일들을 법선 벡터 기준으로 정렬하고 볼륨을 읽음
    표준 방향(RAI)으로 재배향, spacing은 원본 유지 (선택적)
    """
    if not stack_files:
        raise ValueError("Empty stack")
    
    first_ds = stack_files[0][1]
    
    if hasattr(first_ds, 'ImageOrientationPatient') and first_ds.ImageOrientationPatient:
        u = np.array(first_ds.ImageOrientationPatient[:3], dtype=float)
        v = np.array(first_ds.ImageOrientationPatient[3:], dtype=float)
        n = np.cross(u, v)
        n /= (np.linalg.norm(n) + 1e-12)
    else:
        logger.warning("No ImageOrientationPatient, using InstanceNumber for sorting")
        sorted_files = sorted(stack_files, key=lambda x: getattr(x[1], 'InstanceNumber', 0))
        fnames = [f for f, _ in sorted_files]
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(fnames)
        img = reader.Execute()
        return img
    
    def get_position_dot(ds):
        if hasattr(ds, 'ImagePositionPatient') and ds.ImagePositionPatient:
            pos = np.array(ds.ImagePositionPatient, dtype=float)
            return np.dot(n, pos)
        else:
            return getattr(ds, 'InstanceNumber', 0)
    
    sorted_files = sorted(stack_files, key=lambda x: get_position_dot(x[1]))
    
    fnames = [f for f, _ in sorted_files]
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(fnames)
    img = reader.Execute()
    
    logger.info(f"Original image size: {img.GetSize()}, spacing: {img.GetSpacing()}, direction: {img.GetDirection()}")
    
    # 표준 방향(RAI)으로 재배향
    try:
        img_oriented = sitk.DICOMOrient(img, 'RAI')
        logger.info(f"After DICOMOrient: size={img_oriented.GetSize()}, spacing={img_oriented.GetSpacing()}")
        
        # 방향 검증
        direction = np.array(img_oriented.GetDirection()).reshape(3, 3)
        is_identity = np.allclose(direction, np.eye(3), atol=0.1)
        logger.info(f"Direction matrix is near identity: {is_identity}")
    except Exception as e:
        logger.warning(f"DICOMOrient failed: {e}, using original image")
        img_oriented = img
    
    # 원본 spacing 유지 (기본값)
    if keep_original_spacing:
        logger.info("Keeping original spacing")
        return img_oriented
    
    # 등방성 리샘플링 (선택적, 0.8-1.2mm 권장)
    spacing_target = (1.0, 1.0, 1.0)
    size_old = img_oriented.GetSize()
    spacing_old = img_oriented.GetSpacing()
    
    new_size = [int(round(osz * osp / nsp)) 
                for osz, osp, nsp in zip(size_old, spacing_old, spacing_target)]
    
    logger.info(f"Resampling from {size_old} @ {spacing_old} to {new_size} @ {spacing_target}")
    
    img_iso = sitk.Resample(
        img_oriented,
        new_size,
        sitk.Transform(),
        sitk.sitkLinear,
        img_oriented.GetOrigin(),
        spacing_target,
        img_oriented.GetDirection(),
        0.0,
        img_oriented.GetPixelID()
    )
    
    logger.info(f"Final isotropic image: size={img_iso.GetSize()}, spacing={img_iso.GetSpacing()}")
    
    return img_iso


def preprocess_mri_for_surface(img_iso: sitk.Image):
    """
    MRI 이미지 전처리: 윈도잉 → 가우시안 스무딩 → Otsu 임계값 → 연결성 필터
    """
    arr = sitk.GetArrayFromImage(img_iso).astype(np.float32)  # (z, y, x)
    
    logger.info(f"Original array range: [{arr.min():.1f}, {arr.max():.1f}]")
    
    # 1) Intensity windowing (백분위 기반)
    p1, p99 = np.percentile(arr, (1, 99))
    logger.info(f"Percentiles: 1%={p1:.1f}, 99%={p99:.1f}")
    arr = np.clip((arr - p1) / max(p99 - p1, 1e-6), 0, 1)
    
    # 2) 가우시안 스무딩 (물리 단위 1.0mm)
    # SimpleITK의 스무딩은 물리 단위를 사용
    smoothed_img = sitk.SmoothingRecursiveGaussian(img_iso, sigma=1.0)
    smoothed = sitk.GetArrayFromImage(smoothed_img).astype(np.float32)
    
    # 원본 배열에 윈도잉 적용
    arr_original = sitk.GetArrayFromImage(img_iso).astype(np.float32)
    arr_windowed = np.clip((arr_original - p1) / max(p99 - p1, 1e-6), 0, 1)
    
    # 스무딩된 배열에 윈도잉 적용
    p1_smooth, p99_smooth = np.percentile(smoothed, (1, 99))
    smoothed = np.clip((smoothed - p1_smooth) / max(p99_smooth - p1_smooth, 1e-6), 0, 1)
    
    logger.info(f"After smoothing: range=[{smoothed.min():.3f}, {smoothed.max():.3f}]")
    
    # 3) 3D Otsu 임계값
    try:
        t = threshold_otsu(smoothed)
        logger.info(f"Otsu threshold: {t:.3f}")
        mask = smoothed > t
    except Exception as e:
        logger.warning(f"Otsu threshold failed: {e}, using median")
        t = np.median(smoothed)
        mask = smoothed > t
    
    logger.info(f"Binary mask: {np.sum(mask)} / {mask.size} pixels ({100*np.sum(mask)/mask.size:.1f}%)")
    
    # 4) 작은 덩어리 제거 + 구멍 메우기 (연결요소 필터)
    structure = ndi.generate_binary_structure(3, 2)
    mask = ndi.binary_opening(mask, structure=structure)
    
    lbl, n_components = ndi.label(mask)
    counts = np.bincount(lbl.ravel())
    counts[0] = 0  # 배경 제외
    
    if len(counts) > 1:
        keep = np.argmax(counts)  # 가장 큰 성분
        logger.info(f"Keeping largest component: {keep} ({counts[keep]} voxels)")
        mask = (lbl == keep)
    else:
        logger.warning("No components found after labeling")
    
    # 5) 선택적: closing (구멍 메우기)
    mask = ndi.binary_closing(mask, structure=ndi.generate_binary_structure(3, 1))
    
    logger.info(f"Final mask: {np.sum(mask)} / {mask.size} pixels ({100*np.sum(mask)/mask.size:.1f}%)")
    
    return mask.astype(np.float32)


def mesh_from_image_with_coordinate_transform(img_iso, binary_mask=None, level=0.5, step_size=2):
    """
    이미지에서 메쉬를 생성하고, 월드 좌표(LPS→Three.js)로 변환
    spacing 이중 적용 문제 수정 + 스무딩/간소화 추가
    """
    spacing = np.array(img_iso.GetSpacing())  # (x, y, z)
    origin = np.array(img_iso.GetOrigin())    # LPS 좌표
    direction = np.array(img_iso.GetDirection()).reshape(3, 3)
    
    logger.info(f"Image geometry - origin: {origin}, spacing: {spacing}, direction shape: {direction.shape}")
    
    # 1) 마스크가 없으면 전처리로 생성
    if binary_mask is None:
        binary_mask = preprocess_mri_for_surface(img_iso)
    
    # 2) Marching cubes (spacing은 여기서 적용)
    logger.info("Starting marching cubes algorithm...")
    verts_zyx, faces, normals, values = measure.marching_cubes(
        binary_mask.astype(np.float32),
        level=level,
        spacing=spacing[::-1],  # (x,y,z) → (z,y,x)
        step_size=step_size
    )
    logger.info(f"Marching cubes generated {len(verts_zyx)} vertices and {len(faces)} faces")
    
    # 3) (z,y,x) → (x,y,z)로 변환
    verts_xyz = verts_zyx[:, [2, 1, 0]]
    
    # 4) ⚠️ 중요: spacing은 이미 marching_cubes에서 적용되었으므로 곱하지 않음!
    # direction & origin만 적용 → LPS 좌표
    p_lps = (direction @ verts_xyz.T).T + origin
    
    # 5) LPS → Three.js 좌표 변환
    # x = R = -L, y = S, z = P
    p_three = np.column_stack([
        -p_lps[:, 0],  # R = -L
        p_lps[:, 2],   # S
        p_lps[:, 1]    # z = P
    ])
    
    logger.info(f"Converted vertices from LPS to Three.js coordinates")
    logger.info(f"LPS range: x=[{p_lps[:, 0].min():.1f}, {p_lps[:, 0].max():.1f}], "
                f"y=[{p_lps[:, 1].min():.1f}, {p_lps[:, 1].max():.1f}], "
                f"z=[{p_lps[:, 2].min():.1f}, {p_lps[:, 2].max():.1f}]")
    logger.info(f"Three.js range: x=[{p_three[:, 0].min():.1f}, {p_three[:, 0].max():.1f}], "
                f"y=[{p_three[:, 1].min():.1f}, {p_three[:, 1].max():.1f}], "
                f"z=[{p_three[:, 2].min():.1f}, {p_three[:, 2].max():.1f}]")
    
    # 6) Trimesh 메쉬 생성
    mesh = trimesh.Trimesh(vertices=p_three, faces=faces, vertex_normals=normals, process=False)
    logger.info(f"Mesh created: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    
    # 7) 메시 스무딩/간소화 (선택적)
    try:
        logger.info("Applying Laplacian smoothing...")
        trimesh.smoothing.filter_laplacian(mesh, iterations=5, lamb=0.5)
        
        target_faces = int(mesh.faces.shape[0] * 0.5)
        logger.info(f"Simplifying mesh to {target_faces} faces...")
        mesh = mesh.simplify_quadratic_decimation(target_faces)
        logger.info(f"Simplified mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    except Exception as e:
        logger.warning(f"Mesh smoothing/simplification failed: {e}")
    
    return mesh


def process_dicom_to_mesh(reconstruction: Reconstruction, db: Session) -> dict:
    """DICOM 파일을 읽어서 3D 메쉬로 변환 (개선된 파이프라인)"""
    try:
        dicom_files = reconstruction.dicom_url.split(",")
        logger.info(f"Processing {len(dicom_files)} DICOM file(s) for reconstruction {reconstruction.id}")
        
        if not dicom_files:
            return {"status": "error", "message": "No DICOM files"}
        
        with tempfile.TemporaryDirectory() as temp_dir:
            dicom_paths = []
            for dicom_obj in dicom_files:
                logger.info(f"Downloading DICOM file: {dicom_obj}")
                file_data = storage_client.get_file(dicom_obj)
                if not file_data:
                    logger.warning(f"Failed to download file: {dicom_obj}")
                    continue
                
                file_path = os.path.join(temp_dir, os.path.basename(dicom_obj))
                with open(file_path, 'wb') as f:
                    f.write(file_data)
                dicom_paths.append(file_path)
            
            if not dicom_paths:
                return {"status": "error", "message": "Failed to download DICOM files"}
            
            # 각도별 스택 자동 분류 (프로토콜 필터링 포함)
            stacks = group_stacks_by_orientation(dicom_paths)
            
            if not stacks:
                return {"status": "error", "message": "No valid stacks found after grouping"}
            
            # 가장 큰 스택 선택
            largest_stack = max(stacks, key=len)
            logger.info(f"Using largest stack with {len(largest_stack)} file(s)")
            
            # 볼륨 읽기 및 표준화 (원본 spacing 유지)
            img_iso = read_volume_sorted(largest_stack, keep_original_spacing=True)
            
            # 이미지 크기 검증
            image_array = sitk.GetArrayFromImage(img_iso)
            if len(image_array.shape) < 3 or any(dim < 2 for dim in image_array.shape):
                error_msg = f"DICOM image is too small for 3D reconstruction. Shape: {image_array.shape}."
                logger.error(error_msg)
                return {"status": "error", "message": error_msg}
            
            # 메쉬 생성 (전처리 및 좌표 변환 포함)
            try:
                mesh = mesh_from_image_with_coordinate_transform(img_iso, binary_mask=None, level=0.5, step_size=2)
                
                # STL 내보내기
                stl_buffer = io.BytesIO()
                mesh.export(stl_buffer, file_type='stl')
                stl_data = stl_buffer.getvalue()
                stl_size_mb = len(stl_data) / (1024 * 1024)
                logger.info(f"STL file size: {stl_size_mb:.2f} MB")
                stl_obj_name = f"mesh/{reconstruction.id}/mesh.stl"
                storage_client.upload_file(stl_obj_name, stl_data, "application/octet-stream")
                
                # GLTF 내보내기 (Draco 압축 적용)
                try:
                    gltf_buffer = io.BytesIO()
                    mesh.export(gltf_buffer, file_type='glb')
                    uncompressed_glb = gltf_buffer.getvalue()
                    uncompressed_size_mb = len(uncompressed_glb) / (1024 * 1024)
                    logger.info(f"Uncompressed GLB size: {uncompressed_size_mb:.2f} MB")
                    
                    with tempfile.NamedTemporaryFile(suffix='.glb', delete=False) as tmp_input:
                        tmp_input.write(uncompressed_glb)
                        tmp_input_path = tmp_input.name
                    
                    tmp_output_path = tmp_input_path.replace('.glb', '_draco.glb')
                    
                    try:
                        result = subprocess.run(
                            [
                                'npx', '-y', '@gltf-transform/cli', 'compress',
                                tmp_input_path,
                                tmp_output_path,
                                '--draco-compression-level', '10',
                                '--draco-quantize-position', '14',
                                '--draco-quantize-normal', '10',
                                '--draco-quantize-color', '8',
                                '--draco-quantize-texcoord', '12'
                            ],
                            capture_output=True,
                            text=True,
                            timeout=300
                        )
                        
                        if result.returncode == 0 and os.path.exists(tmp_output_path):
                            with open(tmp_output_path, 'rb') as f:
                                gltf_data = f.read()
                            
                            compressed_size_mb = len(gltf_data) / (1024 * 1024)
                            compression_ratio = (1 - len(gltf_data) / len(uncompressed_glb)) * 100
                            logger.info(f"Draco compressed GLB size: {compressed_size_mb:.2f} MB ({compression_ratio:.1f}% reduction)")
                        else:
                            logger.warning(f"Draco compression failed: {result.stderr}, using uncompressed GLB")
                            gltf_data = uncompressed_glb
                            
                    except subprocess.TimeoutExpired:
                        logger.warning("Draco compression timeout, using uncompressed GLB")
                        gltf_data = uncompressed_glb
                    except FileNotFoundError:
                        logger.warning("gltf-transform not found, using uncompressed GLB")
                        gltf_data = uncompressed_glb
                    except Exception as e:
                        logger.warning(f"Draco compression error: {e}, using uncompressed GLB")
                        gltf_data = uncompressed_glb
                    finally:
                        if os.path.exists(tmp_input_path):
                            os.unlink(tmp_input_path)
                        if os.path.exists(tmp_output_path):
                            os.unlink(tmp_output_path)
                    
                    gltf_size_mb = len(gltf_data) / (1024 * 1024)
                    logger.info(f"Final GLB file size: {gltf_size_mb:.2f} MB ({len(mesh.faces)} faces)")
                    
                except Exception as e:
                    logger.error(f"Failed to export GLB: {e}", exc_info=True)
                    raise
                    
                gltf_obj_name = f"mesh/{reconstruction.id}/mesh.glb"
                storage_client.upload_file(gltf_obj_name, gltf_data, "model/gltf-binary")
                
                return {
                    "status": "success",
                    "stl_url": stl_obj_name,
                    "gltf_url": gltf_obj_name
                }
                
            except Exception as e:
                error_msg = f"Mesh generation failed: {str(e)}"
                logger.error(error_msg, exc_info=True)
                return {"status": "error", "message": error_msg}
        
    except Exception as e:
        error_msg = f"Processing failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}
