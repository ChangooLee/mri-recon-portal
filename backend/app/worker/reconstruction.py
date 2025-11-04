import SimpleITK as sitk
import numpy as np
from skimage import measure, morphology
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


def score_stack_for_3d(stack_files):
    """
    스택을 3D 볼륨 적합도로 점수화
    반환: (score, metadata_dict)
    높은 점수 = 3D 등방성 볼륨에 가까움
    """
    if not stack_files:
        return (0, {})
    
    first_ds = stack_files[0][1]
    
    # 기본 메타데이터
    slice_thickness = getattr(first_ds, 'SliceThickness', None)
    spacing_between = getattr(first_ds, 'SpacingBetweenSlices', None)
    pixel_spacing = getattr(first_ds, 'PixelSpacing', None)
    image_type = str(getattr(first_ds, 'ImageType', '') or '').upper()
    series_desc = str(getattr(first_ds, 'SeriesDescription', '') or '').upper()
    
    score = 0
    metadata = {
        'slice_thickness': slice_thickness,
        'spacing_between': spacing_between,
        'pixel_spacing': pixel_spacing,
        'image_type': image_type,
        'series_description': series_desc,
        'is_3d': False,
        'reason': []
    }
    
    # 1) 3D 시퀀스 키워드 체크 (높은 가점)
    if any(keyword in series_desc or keyword in image_type 
           for keyword in ['3D', 'VIBE', 'CUBE', 'SPACE', 'BRAVO', 'MPRAGE', 'FSPGR']):
        score += 100
        metadata['is_3d'] = True
        metadata['reason'].append('3D sequence keyword found')
    
    # 2) SliceThickness 체크 (얇을수록 좋음)
    if slice_thickness is not None:
        if slice_thickness <= 1.2:
            score += 50  # 등방성에 가까움
            metadata['reason'].append(f'Thin slices ({slice_thickness}mm)')
        elif slice_thickness <= 1.5:
            score += 30
            metadata['reason'].append(f'Moderate slice thickness ({slice_thickness}mm)')
        elif slice_thickness <= 2.0:
            score += 10
            metadata['reason'].append(f'Thicker slices ({slice_thickness}mm)')
        else:
            score -= 20  # 너무 두꺼움
            metadata['reason'].append(f'Very thick slices ({slice_thickness}mm)')
    
    # 3) SpacingBetweenSlices ≈ SliceThickness (overlap/공백 없음)
    if slice_thickness is not None and spacing_between is not None:
        ratio = spacing_between / slice_thickness if slice_thickness > 0 else None
        if ratio is not None and 0.9 <= ratio <= 1.1:
            score += 20  # 거의 겹침/공백 없음
            metadata['reason'].append(f'Uniform spacing (ratio={ratio:.2f})')
    
    # 4) In-plane spacing이 작을수록 좋음
    if pixel_spacing is not None and len(pixel_spacing) >= 2:
        in_plane_spacing = min(pixel_spacing[0], pixel_spacing[1])
        if in_plane_spacing <= 0.5:
            score += 10
            metadata['reason'].append(f'Fine in-plane spacing ({in_plane_spacing}mm)')
    
    # 5) 파일 수 (충분한 슬라이스)
    if len(stack_files) >= 50:
        score += 10
        metadata['reason'].append(f'Sufficient slices ({len(stack_files)})')
    
    metadata['score'] = score
    return (score, metadata)


def group_by_series_uid(dicom_paths):
    """
    DICOM 파일들을 SeriesInstanceUID별로 그룹화
    반환: dict {series_uid: [(file_path, pydicom.Dataset), ...]}
    """
    by_series = defaultdict(list)
    
    for dicom_path in dicom_paths:
        try:
            ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)
            series_uid = getattr(ds, 'SeriesInstanceUID', None)
            if not series_uid:
                logger.warning(f"No SeriesInstanceUID in {os.path.basename(dicom_path)}, skipping")
                continue
            
            # 로컬라이저 제외
            image_type = str(getattr(ds, 'ImageType', '') or '').upper()
            if 'LOCALIZER' in image_type or 'SCOUT' in image_type:
                logger.info(f"Skipping LOCALIZER/SCOUT: {os.path.basename(dicom_path)}")
                continue
            
            by_series[series_uid].append((dicom_path, ds))
        except Exception as e:
            logger.warning(f"Failed to read DICOM metadata from {dicom_path}: {e}")
            continue
    
    logger.info(f"Grouped {len(dicom_paths)} files into {len(by_series)} series by SeriesInstanceUID")
    return dict(by_series)


def validate_series_geometry(series_files):
    """
    같은 Series 내에서 이미지 크기/PixelSpacing/IOP/IPP 일관성 검증
    반환: (is_valid, errors)
    """
    if not series_files:
        return False, ["Empty series"]
    
    errors = []
    first_ds = series_files[0][1]
    
    # 기준값
    ref_rows = getattr(first_ds, 'Rows', None)
    ref_columns = getattr(first_ds, 'Columns', None)
    ref_pixel_spacing = tuple(getattr(first_ds, 'PixelSpacing', ()) or [])
    
    for f, ds in series_files[1:]:
        # Rows/Columns 체크
        if getattr(ds, 'Rows', None) != ref_rows or getattr(ds, 'Columns', None) != ref_columns:
            errors.append(f"Inconsistent matrix size in {os.path.basename(f)}")
        
        # PixelSpacing 체크
        pixel_spacing = tuple(getattr(ds, 'PixelSpacing', ()) or [])
        if pixel_spacing != ref_pixel_spacing:
            errors.append(f"Inconsistent PixelSpacing in {os.path.basename(f)}")
    
    if errors:
        logger.warning(f"Geometry inconsistencies found: {errors}")
        return False, errors
    
    return True, []


def group_stacks_by_orientation(series_files, cos_eps=1e-3):
    """
    같은 Series 내에서 각도(Orientation)별로 스택으로 분류 (보조 함수)
    반환: [stack1, stack2, ...] (각 스택은 (file_path, ds) 튜플 리스트)
    """
    groups = []
    
    for f, ds in series_files:
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
    
    # orientation 정보가 없는 파일들도 별도 스택으로 추가
    files_without_orientation = [(f, ds) for f, ds in series_files 
                                if not hasattr(ds, 'ImageOrientationPatient') or ds.ImageOrientationPatient is None]
    if files_without_orientation:
        groups.append({'n': None, 'files': files_without_orientation})
    
    stacks = [g['files'] for g in groups]
    logger.info(f"Grouped {len(series_files)} files into {len(stacks)} orientation stack(s) within series")
    return stacks


def read_volume_sorted(stack_files, keep_original_spacing=None):
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
    
    # IPP 기반 정렬
    sorted_files = sorted(stack_files, key=lambda x: get_position_dot(x[1]))
    
    # Outlier 제거: Δt 변동계수 > 10%
    if len(sorted_files) > 2:
        t_values = [get_position_dot(ds) for _, ds in sorted_files]
        deltas = np.diff(np.sort(t_values))
        median_delta = np.median(deltas)
        
        # 변동계수 계산
        if median_delta > 0:
            cv = np.std(deltas) / median_delta
            logger.info(f"Slice spacing CV: {cv:.3f} (median Δt={median_delta:.3f})")
        
        # Outlier 판단: |Δt - median| > 20%
        valid_files = [sorted_files[0]]  # 첫 번째는 항상 포함
        removed_count = 0
        
        for i in range(1, len(sorted_files)):
            prev_t = get_position_dot(sorted_files[i-1][1])
            curr_t = get_position_dot(sorted_files[i][1])
            delta = abs(curr_t - prev_t)
            
            if delta > 0 and abs(delta - median_delta) / median_delta <= 0.2:
                valid_files.append(sorted_files[i])
            else:
                removed_count += 1
                logger.warning(f"Removing outlier slice: Δt={delta:.3f} vs median={median_delta:.3f} ({os.path.basename(sorted_files[i][0])})")
        
        if removed_count > 0:
            logger.info(f"Removed {removed_count} outlier slice(s), keeping {len(valid_files)}")
            sorted_files = valid_files
        else:
            logger.info(f"Sorted by dot(n, IPP), dz={median_delta:.3f}mm, removed_outliers=0")
    
    fnames = [f for f, _ in sorted_files]
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(fnames)
    img = reader.Execute()
    
    original_spacing = img.GetSpacing()
    original_size = img.GetSize()
    
    # 유효 z-spacing 계산 (outlier 제거 후)
    if len(sorted_files) > 1:
        t_values_final = [get_position_dot(ds) for _, ds in sorted_files]
        deltas_final = np.diff(sorted(t_values_final))
        dz_mm = np.median(deltas_final) if len(deltas_final) > 0 else original_spacing[2]
        logger.info(f"Original image size: {original_size}, spacing: {original_spacing}, dz={dz_mm:.3f}mm")
    else:
        logger.info(f"Original image size: {original_size}, spacing: {original_spacing}")
    
    # 표준 방향(RAI)으로 재배향
    try:
        img_oriented = sitk.DICOMOrient(img, 'RAI')
        logger.info(f"After DICOMOrient: size={img_oriented.GetSize()}, spacing={img_oriented.GetSpacing()}")
        
        # 방향 검증
        direction = np.array(img_oriented.GetDirection()).reshape(3, 3)
        is_identity = np.allclose(direction, np.eye(3), atol=0.1)
        logger.info(f"Direction matrix is near identity: {is_identity}")
        if not is_identity:
            logger.warning(f"Direction matrix: {direction}")
    except Exception as e:
        logger.warning(f"DICOMOrient failed: {e}, using original image")
        img_oriented = img
    
    # 이방성 비율 계산: r = slice_thickness / mean(in-plane spacing)
    spacing = np.array(img_oriented.GetSpacing())
    in_plane = min(spacing[0], spacing[1])
    slice_spacing = spacing[2]
    mean_in_plane = (spacing[0] + spacing[1]) / 2
    anisotropy_ratio_r = slice_spacing / mean_in_plane if mean_in_plane > 0 else 999
    
    logger.info(f"Anisotropy metrics: in-plane={in_plane:.3f}mm, slice={slice_spacing:.3f}mm, r={anisotropy_ratio_r:.2f}")
    
    # 등방성 리샘플링 판단
    # keep_original_spacing이 명시적으로 False면 항상 리샘플
    # None이면 자동 판단: 비등방성이 크면 리샘플
    if keep_original_spacing is False:
        should_resample = True
    elif keep_original_spacing is True:
        should_resample = False
    else:
        # 자동 판단: in-plane spacing과 slice spacing 차이가 크면 리샘플
        anisotropy_ratio = max(in_plane, slice_spacing) / min(in_plane, slice_spacing)
        should_resample = anisotropy_ratio > 1.5  # 비율이 1.5배 이상이면 리샘플
        logger.info(f"Anisotropy ratio: {anisotropy_ratio:.2f}, will resample: {should_resample}")
    
    # 품질 경고
    if anisotropy_ratio_r > 3:
        logger.warning(f"⚠️ High anisotropy ratio (r={anisotropy_ratio_r:.2f} > 3): Low quality expected. SVR/3D sequence recommended.")
    
    if not should_resample:
        logger.info("Keeping original spacing (isotropic or user requested)")
        return img_oriented
    
    # 등방성 리샘플링: r에 따른 전략
    spacing_array = np.array(img_oriented.GetSpacing())
    in_plane_min = min(spacing_array[0], spacing_array[1])
    
    if anisotropy_ratio_r <= 1.5:
        # 거의 등방성: 0.6-0.8mm OK
        iso_spacing = min(max(in_plane_min, 0.6), 0.8)
    elif anisotropy_ratio_r <= 3.0:
        # 중간 이방성: 1.0-1.2mm 권장 (과샘플링 금지)
        iso_spacing = min(max(in_plane_min, 1.0), 1.2)
    else:
        # 높은 이방성: 1.2mm 이상 (메모리 절약)
        iso_spacing = max(in_plane_min, 1.2)
        logger.warning(f"⚠️ Very high anisotropy (r={anisotropy_ratio_r:.2f}): Using larger spacing ({iso_spacing}mm) to avoid over-sampling")
    
    spacing_target = (iso_spacing, iso_spacing, iso_spacing)
    logger.info(f"Setting isotropic spacing to {iso_spacing}mm (r={anisotropy_ratio_r:.2f}, was {in_plane_min}mm in-plane)")
    
    size_old = img_oriented.GetSize()
    spacing_old = img_oriented.GetSpacing()
    
    new_size = [int(round(osz * osp / nsp)) 
                for osz, osp, nsp in zip(size_old, spacing_old, spacing_target)]
    
    logger.info(f"Resampling from {size_old} @ {spacing_old} to {new_size} @ {spacing_target} (BSpline interpolation)")
    
    # BSpline 보간 사용 (강도 유지)
    img_iso = sitk.Resample(
        img_oriented,
        new_size,
        sitk.Transform(),
        sitk.sitkBSpline,  # BSpline 보간 (가우시안보다 강도 유지)
        img_oriented.GetOrigin(),
        spacing_target,
        img_oriented.GetDirection(),
        0.0,
        img_oriented.GetPixelID()
    )
    
    logger.info(f"Final isotropic image: size={img_iso.GetSize()}, spacing={img_iso.GetSpacing()}")
    
    return img_iso


def create_body_mask(img_iso: sitk.Image):
    """
    CurvatureFlow 기반 바디마스크 생성
    부드럽게 → Otsu → 가장 큰 연결요소만 남김
    """
    logger.info("Creating body mask using CurvatureFlow...")
    # CurvatureFlow로 부드럽게 (경계 보존하면서 노이즈 제거)
    smoothed = sitk.CurvatureFlow(img_iso, timeStep=0.125, numberOfIterations=5)
    
    # Otsu 임계값으로 바디 마스크 생성
    try:
        body_mask = sitk.OtsuThreshold(smoothed, 0, 1, 200)
        logger.info(f"Otsu body mask created")
    except Exception as e:
        logger.warning(f"Otsu threshold failed: {e}, using median threshold")
        arr = sitk.GetArrayFromImage(smoothed)
        median_threshold = np.median(arr)
        body_mask = sitk.BinaryThreshold(smoothed, median_threshold, 1e9, 1, 0)
    
    # Morphological closing으로 구멍 메우기
    body_mask = sitk.BinaryMorphologicalClosing(body_mask, [2, 2, 2])
    
    # Connected component로 가장 큰 성분만 남기기
    cc = sitk.ConnectedComponent(body_mask)
    relabeled = sitk.RelabelComponent(cc, sortByObjectSize=True)
    body_mask = sitk.BinaryThreshold(relabeled, 1, 1)
    
    body_mask_arr = sitk.GetArrayFromImage(body_mask).astype(bool)
    logger.info(f"Body mask: {np.sum(body_mask_arr)} / {body_mask_arr.size} pixels ({100*np.sum(body_mask_arr)/body_mask_arr.size:.1f}%)")
    
    return body_mask_arr


def create_bone_mask(img_iso: sitk.Image, body_mask):
    """
    경사도(gradient) 기반 뼈 마스크 생성
    MRI에서 뼈는 검은 테두리(피질골)로 보이므로 경계강도 기반으로 추출
    """
    logger.info("Creating bone mask using gradient magnitude...")
    
    # Gradient magnitude 계산 (경계강도)
    gradient = sitk.GradientMagnitudeRecursiveGaussian(img_iso, sigma=1.0)
    gradient_arr = sitk.GetArrayFromImage(gradient)
    
    # 바디 안쪽 영역만 고려
    gradient_in_body = gradient_arr.copy()
    gradient_in_body[~body_mask] = 0
    
    # 상위 15% 경계만 선택 (뼈 경계는 강한 경사도를 가짐)
    non_zero_gradients = gradient_in_body[gradient_in_body > 0]
    if len(non_zero_gradients) > 0:
        threshold_percentile = np.percentile(non_zero_gradients, 85)
        logger.info(f"Gradient threshold (85th percentile): {threshold_percentile:.3f}")
        
        bone_mask = (gradient_in_body >= threshold_percentile) & body_mask
    else:
        logger.warning("No gradients found in body mask, using fallback")
        bone_mask = body_mask.copy()
    
    # 3D 형태학으로 다듬기
    # 작은 파편 제거 (5000 voxel 이하)
    bone_mask = morphology.remove_small_objects(bone_mask, min_size=5000)
    # Closing으로 경계 부드럽게
    bone_mask = morphology.binary_closing(bone_mask, morphology.ball(2))
    
    bone_voxels = np.sum(bone_mask)
    logger.info(f"Bone mask: {bone_voxels} / {bone_mask.size} pixels ({100*bone_voxels/bone_mask.size:.1f}%)")
    
    return bone_mask.astype(np.float32)


def preprocess_mri_for_surface(img_iso: sitk.Image, use_n4_bias_correction=True, mask_type='body'):
    """
    MRI 이미지 전처리: N4 bias correction → 바디마스크 → 경사도 기반 뼈 마스크
    mask_type: 'body' (바디 전체) 또는 'bone' (경사도 기반 뼈만)
    """
    # 0) N4 Bias Field Correction (선택적)
    if use_n4_bias_correction:
        try:
            logger.info("Applying N4 bias field correction...")
            
            # N4 필터는 float 타입을 요구하므로 픽셀 타입 변환
            pixel_id = img_iso.GetPixelID()
            if pixel_id != sitk.sitkFloat32:
                try:
                    pixel_type_str = sitk.GetPixelIDTypeAsString(pixel_id)
                    logger.info(f"Converting pixel type from {pixel_type_str} to Float32 for N4 bias correction")
                except AttributeError:
                    logger.info(f"Converting pixel type (ID: {pixel_id}) to Float32 for N4 bias correction")
                img_for_n4 = sitk.Cast(img_iso, sitk.sitkFloat32)
            else:
                img_for_n4 = img_iso
            
            # Otsu로 거친 바디마스크 생성 (N4에 필요)
            rough_body = sitk.OtsuThreshold(img_for_n4, 0, 1, 200)
            corrector = sitk.N4BiasFieldCorrectionImageFilter()
            corrector.SetMaximumNumberOfIterations([50, 50, 50, 50])  # 4 levels
            img_bias_corrected = corrector.Execute(img_for_n4, rough_body)
            logger.info("N4 bias correction completed")
            img_for_processing = img_bias_corrected
        except Exception as e:
            logger.warning(f"N4 bias correction failed: {e}, proceeding without correction")
            # float로 변환된 이미지가 있으면 사용, 없으면 원본 사용
            if 'img_for_n4' in locals():
                img_for_processing = img_for_n4
            else:
                img_for_processing = img_iso
    else:
        img_for_processing = img_iso
    
    # 1) 바디마스크 생성 (CurvatureFlow 기반)
    body_mask = create_body_mask(img_for_processing)
    
    # 2) 마스크 타입에 따라 선택
    if mask_type == 'bone':
        # 경사도 기반 뼈 마스크
        final_mask = create_bone_mask(img_for_processing, body_mask)
    else:
        # 바디 마스크만 사용
        final_mask = body_mask.astype(np.float32)
    
    return final_mask


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
    
    # 4) ⚠️ 중요: spacing은 이미 marching_cubes에서 적용되었으므로 verts_xyz는 이미 mm 단위!
    # 따라서 추가 곱 없이 direction & origin만 적용 → LPS 좌표 (mm)
    p_lps = (direction @ verts_xyz.T).T + origin
    
    # 5) LPS → Three.js 좌표 변환 + mm → m 변환
    # Three.js 좌표: x = R = -L, y = S, z = P
    # 단위: mm → m (1/1000)
    p_three = np.column_stack([
        -p_lps[:, 0] * 0.001,  # R = -L, mm → m
        p_lps[:, 2] * 0.001,   # S, mm → m
        p_lps[:, 1] * 0.001    # z = P, mm → m
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
    
    # 7) 메쉬 정리: 퇴화된 면/미사용 정점 제거
    try:
        mesh.remove_degenerate_faces()
        mesh.remove_unreferenced_vertices()
        logger.info(f"After cleanup: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    except Exception as e:
        logger.warning(f"Mesh cleanup failed: {e}")
    
    # 8) 작은 연결요소 제거 (파편 제거)
    try:
        logger.info("Removing small connected components...")
        comps = mesh.split(only_watertight=False)
        # 체적이 있는 컴포넌트 우선 (watertight 메쉬)
        comps_with_volume = [c for c in comps if c.is_volume and c.volume > 0]
        if comps_with_volume:
            # 가장 큰 컴포넌트 선택
            mesh = max(comps_with_volume, key=lambda c: c.volume)
            logger.info(f"Kept largest component with volume: {mesh.volume:.1f}")
        else:
            # 체적이 없으면 면 수 기준으로 선택
            mesh = max(comps, key=lambda c: len(c.faces))
            logger.info(f"Kept largest component by face count: {len(mesh.faces)} faces")
        
        logger.info(f"After component filtering: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    except Exception as e:
        logger.warning(f"Component filtering failed: {e}")
    
    # 9) 메시 스무딩/간소화 (후처리)
    try:
        logger.info("Applying Laplacian smoothing...")
        trimesh.smoothing.filter_laplacian(mesh, iterations=5, lamb=0.5)
        
        # Decimation 30-60% (step_size에 따라 조정)
        decimation_ratio = 0.5 if step_size <= 2 else 0.4  # step_size가 클수록 더 간소화
        target_faces = int(mesh.faces.shape[0] * decimation_ratio)
        logger.info(f"Simplifying mesh to {target_faces} faces ({decimation_ratio*100:.0f}% of original)...")
        mesh = mesh.simplify_quadratic_decimation(target_faces)
        logger.info(f"Simplified mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    except Exception as e:
        logger.warning(f"Mesh smoothing/simplification failed: {e}")
    
    return mesh


def process_dicom_to_mesh(reconstruction: Reconstruction, db: Session) -> dict:
    """
    DICOM 파일을 읽어서 3D 메쉬로 변환
    
    이제 새로운 모듈화된 파이프라인을 사용합니다.
    기존 로직은 유지하되, 새로운 파이프라인을 기본으로 사용합니다.
    """
    # 새로운 파이프라인 사용 (다평면 지원)
    from app.worker.reconstruction_v2 import process_dicom_to_mesh_v2
    
    # 다평면 정합 사용 (여러 시리즈가 있으면 정합/융합)
    return process_dicom_to_mesh_v2(
        reconstruction=reconstruction,
        db=db,
        tissues=['bone'],  # 기본값: 뼈만 (근육 추가 시 ['bone', 'muscle'])
        use_multi_plane=True,  # 다평면 정합 활성화
        target_spacing=1.0  # 등방성 간격
    )


def process_dicom_to_mesh_legacy(reconstruction: Reconstruction, db: Session) -> dict:
    """DICOM 파일을 읽어서 3D 메쉬로 변환 (기존 파이프라인 - 레거시)"""
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
            
            # Step 1: SeriesInstanceUID별로 그룹화
            by_series = group_by_series_uid(dicom_paths)
            
            if not by_series:
                return {"status": "error", "message": "No valid series found (missing SeriesInstanceUID)"}
            
            # QC 게이트 1: 여러 SeriesInstanceUID가 있는 경우 가장 큰 그룹 선택
            if len(by_series) > 1:
                series_uids = list(by_series.keys())
                series_sizes = {uid: len(files) for uid, files in by_series.items()}
                
                # 가장 큰 시리즈 선택
                selected_series_uid = max(series_sizes, key=series_sizes.get)
                selected_size = series_sizes[selected_series_uid]
                total_files = sum(series_sizes.values())
                
                logger.warning(f"Mixed series detected ({len(by_series)} different SeriesInstanceUIDs). "
                             f"Selecting largest series: {selected_series_uid[:32]}... "
                             f"({selected_size}/{total_files} files, {100*selected_size/total_files:.1f}%)")
                logger.warning(f"Other series will be ignored. Series UIDs: {[uid[:16]+'...' for uid in series_uids[:5]]}...")
                
                # 사용자에게 경고 메시지 제공 (하지만 계속 진행)
                # return {"status": "error", ...} 대신 경고만 로그하고 진행
            else:
                selected_series_uid = list(by_series.keys())[0]
            
            # 선택된 Series 사용
            series_files = by_series[selected_series_uid]
            
            # QC 게이트 2: Geometry 일관성 검증
            is_valid, geometry_errors = validate_series_geometry(series_files)
            if not is_valid:
                return {
                    "status": "error",
                    "message": f"Inconsistent geometry in series {selected_series_uid[:16]}...: {', '.join(geometry_errors[:3])}"
                }
            
            # Step 2: 같은 Series 내에서 orientation별로 스택 분류 (보조)
            stacks = group_stacks_by_orientation(series_files)
            
            if not stacks:
                return {"status": "error", "message": "No valid stacks found after orientation grouping"}
            
            # Step 3: 스택 점수화 및 최적 스택 선택
            scored_stacks = []
            for stack in stacks:
                score, metadata = score_stack_for_3d(stack)
                scored_stacks.append((score, stack, metadata))
            
            # 점수순 정렬 (높은 점수 = 3D/얇은 슬라이스 우선)
            scored_stacks.sort(key=lambda x: x[0], reverse=True)
            
            best_score, best_stack, best_metadata = scored_stacks[0]
            
            # 메타데이터 추출
            first_ds = best_stack[0][1]
            rows = getattr(first_ds, 'Rows', None)
            columns = getattr(first_ds, 'Columns', None)
            pixel_spacing = getattr(first_ds, 'PixelSpacing', None)
            
            logger.info(f"Selected SeriesInstanceUID={selected_series_uid[:32]}... (files={len(best_stack)}, spacing={pixel_spacing}, matrix={rows}x{columns})")
            logger.info(f"Stack selection results:")
            for idx, (score, stack, metadata) in enumerate(scored_stacks[:3]):  # 상위 3개만 로그
                logger.info(f"  [{idx+1}] Score={score}, Files={len(stack)}, "
                          f"SliceThickness={metadata.get('slice_thickness')}, "
                          f"Is3D={metadata.get('is_3d')}, Reasons={metadata.get('reason')}")
            
            logger.info(f"Selected stack: {len(best_stack)} file(s), score={best_score}")
            logger.info(f"Selected stack metadata: {best_metadata}")
            
            # 정렬 품질 검증
            if hasattr(best_stack[0][1], 'ImagePositionPatient') and best_stack[0][1].ImagePositionPatient:
                first_ds = best_stack[0][1]
                u = np.array(first_ds.ImageOrientationPatient[:3], dtype=float)
                v = np.array(first_ds.ImageOrientationPatient[3:], dtype=float)
                n = np.cross(u, v)
                n /= (np.linalg.norm(n) + 1e-12)
                
                positions = []
                for f, ds in best_stack:
                    if hasattr(ds, 'ImagePositionPatient') and ds.ImagePositionPatient:
                        pos = np.array(ds.ImagePositionPatient, dtype=float)
                        t = np.dot(n, pos)
                        positions.append(t)
                
                if len(positions) > 1:
                    positions = np.array(positions)
                    deltas = np.diff(np.sort(positions))
                    median_delta = np.median(deltas)
                    std_delta = np.std(deltas)
                    non_increasing = np.sum(deltas <= 0)
                    
                    logger.info(f"Slice sorting quality: median Δt={median_delta:.3f}, std={std_delta:.3f}, "
                              f"non-increasing={non_increasing}")
                    
                    if non_increasing > len(positions) * 0.1:  # 10% 이상이 비증가면 경고
                        logger.warning(f"Many non-increasing slice positions ({non_increasing}/{len(positions)})")
            
            # 볼륨 읽기 및 표준화 (자동 판단: 비등방성이 크면 리샘플)
            img_iso = read_volume_sorted(best_stack, keep_original_spacing=None)
            
            # 이미지 크기 검증
            image_array = sitk.GetArrayFromImage(img_iso)
            if len(image_array.shape) < 3 or any(dim < 2 for dim in image_array.shape):
                error_msg = f"DICOM image is too small for 3D reconstruction. Shape: {image_array.shape}."
                logger.error(error_msg)
                return {"status": "error", "message": error_msg}
            
            # 메쉬 생성 (전처리, ROI 크롭, 좌표 변환 포함)
            try:
                # 메쉬 생성 전 ROI 크롭 적용 (이미지 자체 크롭)
                image_array = sitk.GetArrayFromImage(img_iso)
                binary_mask = preprocess_mri_for_surface(img_iso)
                
                # 마스크 바운딩박스로 이미지 크롭 (배경 슬랩 제거)
                coords = np.argwhere(binary_mask > 0)
                if len(coords) > 0:
                    bbox_min = coords.min(axis=0)
                    bbox_max = coords.max(axis=0)
                    margin_voxels = np.array([15, 15, 15]) / np.array(img_iso.GetSpacing())
                    crop_min = np.maximum(0, (bbox_min - margin_voxels).astype(int))
                    crop_max = np.minimum(np.array(image_array.shape), (bbox_max + margin_voxels).astype(int))
                    
                    # 이미지와 마스크 크롭
                    image_cropped = image_array[crop_min[0]:crop_max[0], 
                                                crop_min[1]:crop_max[1], 
                                                crop_min[2]:crop_max[2]]
                    mask_cropped = binary_mask[crop_min[0]:crop_max[0], 
                                              crop_min[1]:crop_max[1], 
                                              crop_min[2]:crop_max[2]]
                    
                    # SimpleITK Image 재생성 (원점 보정)
                    origin = np.array(img_iso.GetOrigin())
                    spacing = np.array(img_iso.GetSpacing())
                    new_origin = origin + (crop_min * spacing)
                    
                    img_cropped = sitk.GetImageFromArray(image_cropped)
                    img_cropped.SetSpacing(img_iso.GetSpacing())
                    img_cropped.SetOrigin(new_origin)
                    img_cropped.SetDirection(img_iso.GetDirection())
                    
                    logger.info(f"Image cropped: {image_array.shape} → {image_cropped.shape}, new origin: {new_origin}")
                    
                    # 크롭된 이미지로 메쉬 생성 (bone 마스크 사용)
                    mesh = mesh_from_image_with_coordinate_transform(img_cropped, binary_mask=mask_cropped, level=0.5, step_size=3)
                else:
                    logger.warning("No mask found for cropping, using full image")
                    mesh = mesh_from_image_with_coordinate_transform(img_iso, binary_mask=binary_mask, level=0.5, step_size=3)
                
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
