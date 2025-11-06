"""
전처리 모듈
N4 bias correction, 등방성 리샘플링, 바디마스크 생성
방향/원점/간격 메타데이터 엄격히 보존
"""
import SimpleITK as sitk
import numpy as np
import logging

logger = logging.getLogger(__name__)


def n4_bias(img: sitk.Image) -> sitk.Image:
    """
    N4 Bias Field Correction
    MRI 신호 불균일 보정
    
    Args:
        img: 입력 SimpleITK Image
        
    Returns:
        sitk.Image: bias 보정된 이미지
    """
    logger.info("Applying N4 bias field correction...")
    
    # N4 필터는 float 타입을 요구하므로 픽셀 타입 변환
    pixel_id = img.GetPixelID()
    if pixel_id != sitk.sitkFloat32:
        try:
            pixel_type_str = sitk.GetPixelIDTypeAsString(pixel_id)
            logger.info(f"Converting pixel type from {pixel_type_str} to Float32 for N4 bias correction")
        except AttributeError:
            logger.info(f"Converting pixel type (ID: {pixel_id}) to Float32 for N4 bias correction")
        img_float = sitk.Cast(img, sitk.sitkFloat32)
    else:
        img_float = img
    
    # Otsu로 거친 바디마스크 생성 (N4에 필요)
    mask = sitk.OtsuThreshold(img_float, 0, 1, 200)
    
    try:
        corrector = sitk.N4BiasFieldCorrectionImageFilter()
        corrector.SetMaximumNumberOfIterations([50, 50, 50, 50])  # 4 levels
        corrected = corrector.Execute(img_float, mask)
        logger.info("N4 bias correction completed")
        return corrected
    except Exception as e:
        logger.warning(f"N4 bias correction failed: {e}, returning original image")
        return img_float


def to_isotropic(img: sitk.Image, iso: float = 1.2) -> sitk.Image:
    """
    등방성 리샘플링 (1.0-1.2mm 권장)
    방향/원점/간격 메타데이터 엄격히 보존
    
    Args:
        img: 입력 이미지
        iso: 목표 등방성 간격 (mm, 기본 1.2mm)
        
    Returns:
        sitk.Image: 등방성 리샘플된 이미지
    """
    # 원본 메타데이터 보존
    original_direction = img.GetDirection()
    original_origin = img.GetOrigin()
    original_spacing = img.GetSpacing()
    
    new_sp = [iso, iso, iso]
    size = img.GetSize()
    spacing = img.GetSpacing()
    new_sz = [int(round(s * z / p)) for s, z, p in zip(size, spacing, new_sp)]
    
    # BSpline 보간 (스칼라 값에는 BSpline 권장, Nearest 아님)
    res = sitk.ResampleImageFilter()
    res.SetInterpolator(sitk.sitkBSpline)
    res.SetOutputSpacing(new_sp)
    res.SetSize(new_sz)
    res.SetOutputDirection(original_direction)  # 방향 보존
    res.SetOutputOrigin(original_origin)  # 원점 보존
    res.SetOutputPixelType(sitk.sitkFloat32)  # Float32로 명시적 변환
    
    resampled = res.Execute(img)
    
    # 메타데이터 보존 확인
    logger.info(f"Resampled to isotropic: size={resampled.GetSize()}, spacing={resampled.GetSpacing()}")
    logger.debug(f"Direction preserved: {np.allclose(np.array(resampled.GetDirection()), np.array(original_direction))}")
    logger.debug(f"Origin preserved: {np.allclose(np.array(resampled.GetOrigin()), np.array(original_origin))}")
    
    return resampled


def resample_to_spacing(img: sitk.Image, target_spacing: tuple, order: int = 1) -> sitk.Image:
    """
    지정된 간격으로 리샘플링 (준등방/등방 지원)
    
    Args:
        img: 입력 SimpleITK Image
        target_spacing: 목표 간격 (x, y, z) 또는 (z, y, x) - SimpleITK는 (x,y,z)
        order: 보간 차수 (0=Nearest, 1=Linear, 3=BSpline)
        
    Returns:
        sitk.Image: 리샘플된 이미지
    """
    original_direction = img.GetDirection()
    original_origin = img.GetOrigin()
    original_spacing = img.GetSpacing()
    
    # SimpleITK spacing은 (x, y, z) 순서
    if len(target_spacing) == 3:
        new_sp = list(target_spacing)
    else:
        raise ValueError(f"target_spacing must be 3-tuple, got {target_spacing}")
    
    size = img.GetSize()
    spacing = img.GetSpacing()
    new_sz = [int(round(s * z / p)) for s, z, p in zip(size, spacing, new_sp)]
    
    res = sitk.ResampleImageFilter()
    if order == 0:
        res.SetInterpolator(sitk.sitkNearestNeighbor)
    elif order == 1:
        res.SetInterpolator(sitk.sitkLinear)
    else:
        res.SetInterpolator(sitk.sitkBSpline)
    
    res.SetOutputSpacing(new_sp)
    res.SetSize(new_sz)
    res.SetOutputDirection(original_direction)
    res.SetOutputOrigin(original_origin)
    res.SetOutputPixelType(sitk.sitkFloat32)
    
    resampled = res.Execute(img)
    return resampled


def body_mask(img_iso: sitk.Image) -> sitk.Image:
    """
    CurvatureFlow 기반 바디마스크 생성
    부드럽게 → Otsu → 가장 큰 연결요소만 남김
    
    Args:
        img_iso: 등방성 리샘플된 이미지
        
    Returns:
        sitk.Image: 바디 마스크 (binary)
    """
    logger.info("Creating body mask using CurvatureFlow...")
    # CurvatureFlow로 부드럽게 (경계 보존하면서 노이즈 제거)
    sm = sitk.CurvatureFlow(img_iso, timeStep=0.125, numberOfIterations=5)
    
    # Otsu 임계값으로 바디 마스크 생성
    try:
        m = sitk.OtsuThreshold(sm, 0, 1, 200)
    except Exception as e:
        logger.warning(f"Otsu threshold failed: {e}, using median threshold")
        arr = sitk.GetArrayFromImage(sm)
        median_threshold = np.median(arr)
        m = sitk.BinaryThreshold(sm, median_threshold, 1e9, 1, 0)
    
    # Morphological closing으로 구멍 메우기
    m = sitk.BinaryMorphologicalClosing(m, [2, 2, 2])
    
    # Connected component로 가장 큰 성분만 남기기
    cc = sitk.ConnectedComponent(m)
    rel = sitk.RelabelComponent(cc, sortByObjectSize=True)
    m = sitk.BinaryThreshold(rel, 1, 1)
    
    mask_arr = sitk.GetArrayFromImage(m).astype(bool)
    logger.info(f"Body mask: {np.sum(mask_arr)} / {mask_arr.size} pixels ({100*np.sum(mask_arr)/mask_arr.size:.1f}%)")
    
    return m

