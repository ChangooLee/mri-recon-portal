"""
DICOM I/O 모듈
SeriesInstanceUID별 그룹화 및 볼륨 로딩
"""
from pathlib import Path
from typing import Dict, List
import SimpleITK as sitk
import logging

logger = logging.getLogger(__name__)


def list_series(dicom_root: Path) -> Dict[str, List[str]]:
    """
    루트 폴더 하위의 DICOM을 SeriesInstanceUID 기준으로 그룹화하여 반환
    
    Args:
        dicom_root: DICOM 파일들이 있는 디렉토리 경로
        
    Returns:
        dict: {SeriesInstanceUID: [file_paths]}
    """
    r = sitk.ImageSeriesReader()
    series_ids = r.GetGDCMSeriesIDs(str(dicom_root)) or []
    
    out = {}
    for sid in series_ids:
        files = r.GetGDCMSeriesFileNames(str(dicom_root), sid)
        out[sid] = list(files)
    
    logger.info(f"Found {len(series_ids)} series in {dicom_root}")
    return out


def load_series_by_files(files: List[str]) -> sitk.Image:
    """
    파일 경로 리스트로부터 SimpleITK Image 로딩
    가이드: 방향/원점/간격 메타데이터 엄격히 보존
    필수 태그: PixelSpacing, ImagePositionPatient, ImageOrientationPatient
    
    Args:
        files: DICOM 파일 경로 리스트
        
    Returns:
        sitk.Image: 로딩된 볼륨 이미지 (LPS 좌표계, 방향/간격 포함)
    """
    r = sitk.ImageSeriesReader()
    r.SetFileNames(files)
    
    # 가이드: DICOM 메타데이터 보존 설정
    r.MetaDataDictionaryArrayUpdateOn()
    r.LoadPrivateTagsOn()
    
    img = r.Execute()  # 방향/원점/간격 보존
    
    # 메타데이터 확인
    spacing = img.GetSpacing()
    direction = img.GetDirection()
    origin = img.GetOrigin()
    
    logger.info(f"Loaded series with size={img.GetSize()}, spacing={spacing}, origin={origin}")
    logger.debug(f"Direction preserved: {direction[:9] if len(direction) >= 9 else direction}")
    
    return img

