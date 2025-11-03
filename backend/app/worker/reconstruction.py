import SimpleITK as sitk
import numpy as np
from skimage import measure
import trimesh
from app.models.reconstruction import Reconstruction
from app.utils.storage import storage_client
from app.core.config import settings
from sqlalchemy.orm import Session
import io


def process_dicom_to_mesh(reconstruction: Reconstruction, db: Session) -> dict:
    """DICOM 파일을 읽어서 3D 메쉬로 변환"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        # DICOM 파일 다운로드
        dicom_files = reconstruction.dicom_url.split(",")
        logger.info(f"Processing {len(dicom_files)} DICOM file(s) for reconstruction {reconstruction.id}")
        
        if not dicom_files:
            return {"status": "error", "message": "No DICOM files"}
        
        # SimpleITK로 DICOM 시리즈 읽기
        reader = sitk.ImageSeriesReader()
        
        # MinIO에서 파일들을 임시로 다운로드하여 처리
        import tempfile
        import os
        
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
                logger.info(f"Downloaded {len(file_data)} bytes to {file_path}")
            
            if not dicom_paths:
                return {"status": "error", "message": "Failed to download DICOM files"}
            
            # DICOM 시리즈 읽기 - 같은 크기의 파일들만 그룹화
            logger.info(f"Reading DICOM series with {len(dicom_paths)} file(s)")
            
            # 파일들을 크기별로 그룹화
            from collections import defaultdict
            size_groups = defaultdict(list)
            
            for dicom_path in dicom_paths:
                try:
                    single_reader = sitk.ImageFileReader()
                    single_reader.SetFileName(dicom_path)
                    single_reader.ReadImageInformation()
                    size = single_reader.GetSize()
                    size_key = tuple(size)
                    size_groups[size_key].append(dicom_path)
                    logger.debug(f"File {os.path.basename(dicom_path)}: size={size}")
                except Exception as e:
                    logger.warning(f"Failed to read DICOM info for {dicom_path}: {e}")
                    continue
            
            # 가장 많은 파일을 가진 그룹 선택
            if not size_groups:
                return {"status": "error", "message": "No valid DICOM files found"}
            
            largest_group = max(size_groups.items(), key=lambda x: len(x[1]))
            selected_paths = sorted(largest_group[1])
            group_size = largest_group[0]
            
            logger.info(f"Found {len(size_groups)} different image size groups:")
            for size_key, paths in size_groups.items():
                logger.info(f"  Size {size_key}: {len(paths)} file(s)")
            logger.info(f"Using largest group: {len(selected_paths)} file(s) with size {group_size}")
            
            # 선택된 파일들로만 DICOM 시리즈 읽기
            reader.SetFileNames(selected_paths)
            image = reader.Execute()
            
            logger.info(f"DICOM image size: {image.GetSize()}, spacing: {image.GetSpacing()}")
            
            # NumPy 배열로 변환
            image_array = sitk.GetArrayFromImage(image)
            logger.info(f"Image array shape: {image_array.shape}, dtype: {image_array.dtype}, min: {image_array.min()}, max: {image_array.max()}")
            
            # 이미지 크기 검증
            if len(image_array.shape) < 3 or any(dim < 2 for dim in image_array.shape):
                error_msg = f"DICOM image is too small for 3D reconstruction. Shape: {image_array.shape}. Need at least 2x2x2 for 3D mesh generation."
                logger.error(error_msg)
                return {"status": "error", "message": error_msg}
            
            # 이미지 정규화 및 임계값 처리
            # 간단한 임계값 기반 세그멘테이션 (실제로는 더 정교한 전처리 필요)
            threshold = np.percentile(image_array, 50)  # 중앙값 사용
            logger.info(f"Using threshold: {threshold}")
            binary_array = image_array > threshold
            
            logger.info(f"Binary array shape: {binary_array.shape}, True count: {np.sum(binary_array)}")
            
            # Marching Cubes 알고리즘으로 메쉬 생성
            try:
                logger.info("Starting marching cubes algorithm...")
                verts, faces, normals, values = measure.marching_cubes(
                    binary_array.astype(np.float32),
                    level=0.5,
                    spacing=image.GetSpacing()[::-1]  # SimpleITK는 (z,y,x), scikit-image는 (x,y,z)
                )
                logger.info(f"Marching cubes generated {len(verts)} vertices and {len(faces)} faces")
                
                # Trimesh로 메쉬 생성 (품질 유지, 간소화 없음)
                mesh = trimesh.Trimesh(vertices=verts, faces=faces, vertex_normals=normals)
                logger.info(f"Mesh created: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
                
                # STL 내보내기
                stl_buffer = io.BytesIO()
                mesh.export(stl_buffer, file_type='stl')
                stl_data = stl_buffer.getvalue()
                stl_size_mb = len(stl_data) / (1024 * 1024)
                logger.info(f"STL file size: {stl_size_mb:.2f} MB")
                stl_obj_name = f"mesh/{reconstruction.id}/mesh.stl"
                storage_client.upload_file(stl_obj_name, stl_data, "application/octet-stream")
                
                # GLTF 내보내기 (Draco 압축 적용 - 무손실)
                try:
                    # 먼저 기본 GLB로 내보내기
                    gltf_buffer = io.BytesIO()
                    mesh.export(gltf_buffer, file_type='glb')
                    uncompressed_glb = gltf_buffer.getvalue()
                    uncompressed_size_mb = len(uncompressed_glb) / (1024 * 1024)
                    logger.info(f"Uncompressed GLB size: {uncompressed_size_mb:.2f} MB")
                    
                    # Draco 압축 적용 (무손실 모드)
                    import tempfile
                    import subprocess
                    import os
                    
                    with tempfile.NamedTemporaryFile(suffix='.glb', delete=False) as tmp_input:
                        tmp_input.write(uncompressed_glb)
                        tmp_input_path = tmp_input.name
                    
                    tmp_output_path = tmp_input_path.replace('.glb', '_draco.glb')
                    
                    try:
                        # gltf-transform을 사용하여 Draco 압축 적용 (무손실 모드)
                        # npx를 통해 @gltf-transform/cli 실행
                        import shutil
                        result = subprocess.run(
                            [
                                'npx', '-y', '@gltf-transform/cli', 'compress',
                                tmp_input_path,
                                tmp_output_path,
                                '--draco-compression-level', '10',
                                '--draco-quantize-position', '14',  # 무손실에 가까운 정밀도
                                '--draco-quantize-normal', '10',
                                '--draco-quantize-color', '8',
                                '--draco-quantize-texcoord', '12'
                            ],
                            capture_output=True,
                            text=True,
                            timeout=300  # 5분 타임아웃
                        )
                        
                        if result.returncode == 0 and os.path.exists(tmp_output_path):
                            with open(tmp_output_path, 'rb') as f:
                                gltf_data = f.read()
                            
                            compressed_size_mb = len(gltf_data) / (1024 * 1024)
                            compression_ratio = (1 - len(gltf_data) / len(uncompressed_glb)) * 100
                            logger.info(f"Draco compressed GLB size: {compressed_size_mb:.2f} MB ({compression_ratio:.1f}% reduction, lossless)")
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
                        # 임시 파일 정리
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

