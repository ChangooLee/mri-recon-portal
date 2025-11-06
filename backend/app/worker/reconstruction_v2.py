"""
새로운 재구성 파이프라인 (모듈화된 구조)
기존 Celery/MinIO 통합 유지하면서 다평면 정합/융합 지원
"""
import SimpleITK as sitk
import numpy as np
from pathlib import Path
import tempfile
import os
import logging
from sqlalchemy.orm import Session

from app.models.reconstruction import Reconstruction
from app.utils.storage import storage_client
from app.processing.pipeline import run_reconstruction, ReconOptions
from app.processing.io import list_series, load_series_by_files

logger = logging.getLogger(__name__)


def process_dicom_to_mesh_v2(
    reconstruction: Reconstruction, 
    db: Session,
    tissues: list = None,
    use_multi_plane: bool = False,  # 기본값: 단일 시리즈 우선 (가이드 권장)
    target_spacing: float = 1.2  # 등방성 간격 (1.0-1.2mm 권장)
) -> dict:
    """
    새로운 재구성 파이프라인 (다평면 지원)
    
    Args:
        reconstruction: Reconstruction 모델 인스턴스
        db: 데이터베이스 세션
        tissues: 조직 타입 리스트 ['bone', 'muscle'] (기본값: ['bone'])
        use_multi_plane: 다평면 정합/융합 사용 여부
        target_spacing: 등방성 리샘플 간격 (mm)
        
    Returns:
        dict: {
            'status': 'success' or 'error',
            'stl_url': str,
            'gltf_url': str,
            'message': str (optional)
        }
    """
    try:
        dicom_files = reconstruction.dicom_url.split(",")
        logger.info(f"Processing {len(dicom_files)} DICOM file(s) for reconstruction {reconstruction.id}")
        
        if not dicom_files:
            return {"status": "error", "message": "No DICOM files"}
        
        if tissues is None:
            tissues = ['bone']  # 기본값: 뼈만
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # 1) MinIO에서 DICOM 파일 다운로드
            dicom_paths = []
            for dicom_obj in dicom_files:
                logger.info(f"Downloading DICOM file: {dicom_obj}")
                file_data = storage_client.get_file(dicom_obj)
                if not file_data:
                    logger.warning(f"Failed to download file: {dicom_obj}")
                    continue
                
                file_path = temp_path / os.path.basename(dicom_obj)
                with open(file_path, 'wb') as f:
                    f.write(file_data)
                dicom_paths.append(str(file_path))
            
            if not dicom_paths:
                return {"status": "error", "message": "Failed to download DICOM files"}
            
            # 2) SeriesInstanceUID별로 그룹화하여 여러 시리즈 디렉터리 생성
            # 다평면 처리를 위해 각 시리즈를 별도 디렉터리로 분리
            import pydicom
            from collections import defaultdict
            
            series_groups = defaultdict(list)
            for dicom_path in dicom_paths:
                try:
                    ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)
                    series_uid = getattr(ds, 'SeriesInstanceUID', None)
                    
                    if not series_uid:
                        continue
                    
                    # LOCALIZER/SCOUT 제외
                    image_type = str(getattr(ds, 'ImageType', '') or '').upper()
                    if 'LOCALIZER' in image_type or 'SCOUT' in image_type:
                        continue
                    
                    series_groups[series_uid].append(dicom_path)
                except Exception as e:
                    logger.warning(f"Failed to read DICOM metadata from {dicom_path}: {e}")
                    continue
            
            series_groups = dict(series_groups)
            
            if not series_groups:
                return {"status": "error", "message": "No valid series found"}
            
            logger.info(f"Found {len(series_groups)} series: {[uid[:16]+'...' for uid in series_groups.keys()]}")
            
            # 가이드: 단일 시리즈 우선 처리 (안정적인 기본 파이프라인)
            # OOM 방지: 큰 데이터셋 또는 여러 시리즈는 자동으로 단일 시리즈만 사용
            total_files = sum(len(files) for files in series_groups.values())
            num_series = len(series_groups)
            
            # OOM 방지 조건: 200개 이상 파일 OR 3개 이상 시리즈
            if use_multi_plane and num_series > 1 and (total_files > 200 or num_series >= 3):
                logger.warning(f"Large dataset detected ({total_files} files, {num_series} series). "
                             f"Using single series to avoid OOM. Multi-plane registration disabled.")
                use_multi_plane = False
            
            # 시리즈 메타데이터 수집 (z-spacing 우선 선택용)
            import pydicom
            series_meta = {}
            for uid, files in series_groups.items():
                try:
                    # 첫 번째 파일로 메타데이터 확인
                    ds = pydicom.dcmread(files[0], stop_before_pixels=True)
                    spacing = getattr(ds, 'PixelSpacing', [1.0, 1.0])
                    if hasattr(ds, 'SliceThickness') and ds.SliceThickness:
                        z_spacing = float(ds.SliceThickness)
                    elif hasattr(ds, 'SpacingBetweenSlices') and ds.SpacingBetweenSlices:
                        z_spacing = float(ds.SpacingBetweenSlices)
                    else:
                        # ImagePositionPatient로 계산 (간단한 추정)
                        z_spacing = 4.0  # fallback
                    series_meta[uid] = {
                        'slices': len(files),
                        'z_spacing': z_spacing,
                        'pixel_spacing': spacing
                    }
                    logger.info(f"Series {uid[:16]}...: slices={len(files)}, z={z_spacing:.2f}mm, pixel={spacing}")
                except Exception as e:
                    logger.warning(f"Failed to read metadata for series {uid[:16]}...: {e}")
                    series_meta[uid] = {'slices': len(files), 'z_spacing': 9.9, 'pixel_spacing': [1.0, 1.0]}
            
            # 시리즈 선택: z-spacing 우선 (작을수록 좋음), 슬라이스 수 하한
            def choose_primary_series(series_groups, series_meta, logger):
                """z-spacing이 작고 슬라이스 수가 많은 시리즈 우선 선택"""
                import os
                force_uid = os.getenv("FORCE_SERIES_UID")
                if force_uid and force_uid in series_groups:
                    logger.warning(f"FORCE_SERIES_UID set. Using {force_uid} regardless of heuristics.")
                    return force_uid
                
                cand = []
                for uid, files in series_groups.items():
                    meta = series_meta.get(uid, {})
                    z = meta.get('z_spacing', 9.9)
                    n = meta.get('slices', len(files))
                    cand.append((uid, n, z))
                
                cand.sort(key=lambda x: (x[2], -x[1]))  # z 오름차순, 슬라이스수 내림차순
                
                def pick(min_z, min_n):
                    for uid, n, z in cand:
                        if z <= min_z and n >= min_n:
                            return uid, n, z
                    return None
                
                picked = pick(2.2, 40) or pick(3.5, 30)
                if picked:
                    uid, n, z = picked
                    logger.info(f"Selected by spacing: uid={uid[:16]}..., slices={n}, z={z:.2f}mm")
                    return uid
                
                uid, n, z = max(cand, key=lambda t: t[1])
                logger.warning(f"No fine spacing series; fallback to largest: uid={uid[:16]}..., slices={n}, z={z:.2f}mm")
                return uid
            
            # 3) 다평면 처리 또는 단일 시리즈 처리
            if use_multi_plane and len(series_groups) > 1:
                # 다평면 정합/융합
                logger.info("Using multi-plane registration and fusion")
                
                # 각 시리즈를 별도 디렉터리로 구성
                series_dirs = []
                for sid, files in series_groups.items():
                    series_dir = temp_path / f"series_{sid[:8]}"
                    series_dir.mkdir(parents=True, exist_ok=True)
                    
                    import shutil
                    for f in files:
                        # 심볼릭 링크 또는 복사 (SimpleITK는 파일 경로 필요)
                        shutil.copy(f, series_dir / os.path.basename(f))
                    
                    series_dirs.append(series_dir)
                
                # 재구성 옵션
                opts = ReconOptions(
                    target_spacing=target_spacing,
                    tissues=tissues,
                    use_superres=False,  # 초해상은 선택적
                    output_dir=temp_path / "output",
                    recon_id=str(reconstruction.id)
                )
                
                # 파이프라인 실행
                result = run_reconstruction(series_dirs, opts, temp_dir=temp_path)
                
            else:
                # 단일 시리즈 처리 (z-spacing 우선 선택)
                logger.info("Using single series (z-spacing priority) processing")
                
                selected_series_uid = choose_primary_series(series_groups, series_meta, logger)
                selected_files = series_groups[selected_series_uid]
                
                if len(series_groups) > 1:
                    meta_info = series_meta.get(selected_series_uid, {})
                    logger.info(f"Selected series: {selected_series_uid[:16]}... "
                             f"({len(selected_files)}/{sum(len(f) for f in series_groups.values())} files, "
                             f"z={meta_info.get('z_spacing', 0):.2f}mm, slices={len(selected_files)})")
                
                # 단일 시리즈 디렉터리 구성
                series_dir = temp_path / "series_single"
                series_dir.mkdir(parents=True, exist_ok=True)
                
                import shutil
                for f in selected_files:
                    shutil.copy(f, series_dir / os.path.basename(f))
                
                opts = ReconOptions(
                    target_spacing=target_spacing,
                    tissues=tissues,
                    use_superres=False,
                    output_dir=temp_path / "output",
                    recon_id=str(reconstruction.id)
                )
                
                result = run_reconstruction([series_dir], opts, temp_dir=temp_path)
            
            # 4) 결과 파일을 MinIO에 업로드
            gltf_path = result['gltf']
            stl_path = result['stl']
            
            # GLB 업로드
            with open(gltf_path, 'rb') as f:
                gltf_data = f.read()
            gltf_obj_name = f"mesh/{reconstruction.id}/mesh.glb"
            storage_client.upload_file(gltf_obj_name, gltf_data, "model/gltf-binary")
            logger.info(f"Uploaded GLB: {gltf_obj_name}")
            
            # STL 업로드
            with open(stl_path, 'rb') as f:
                stl_data = f.read()
            stl_obj_name = f"mesh/{reconstruction.id}/mesh.stl"
            storage_client.upload_file(stl_obj_name, stl_data, "application/octet-stream")
            logger.info(f"Uploaded STL: {stl_obj_name}")
            
            # 로그 출력
            for log_msg in result.get('log', []):
                logger.info(f"Pipeline log: {log_msg}")
            
            return {
                "status": "success",
                "stl_url": stl_obj_name,
                "gltf_url": gltf_obj_name
            }
    
    except Exception as e:
        error_msg = f"Processing failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

