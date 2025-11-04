"""
정합 및 융합 모듈
다평면 MRI 스택을 정합하고 융합
"""
import SimpleITK as sitk
from typing import List
import logging

logger = logging.getLogger(__name__)


def rigid_register(fixed: sitk.Image, moving: sitk.Image) -> sitk.Image:
    """
    Rigid Registration (강체 정합)
    가이드: SimpleITK 다중해상도 옵티마이저/매트릭 조합 사용
    방향·간격·원점 메타데이터 엄격히 보존
    
    Args:
        fixed: 기준 볼륨
        moving: 정합할 볼륨
        
    Returns:
        sitk.Image: 정합된 볼륨 (fixed의 공간으로 변환됨)
    """
    logger.info("Starting rigid registration (Mattes Mutual Information)...")
    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsMattesMutualInformation(32)
    reg.SetInterpolator(sitk.sitkLinear)  # 정합 중에는 Linear 사용
    
    # 가이드: 다중해상도 옵티마이저 사용 (RegularStepGradientDescent 권장)
    reg.SetOptimizerAsRegularStepGradientDescent(4.0, 1e-3, 200)
    reg.SetShrinkFactorsPerLevel([4, 2, 1])  # 다중해상도
    reg.SetSmoothingSigmasPerLevel([2, 1, 0])  # 다중해상도 스무딩
    
    # 초기 변환 (기하학적 중심 기반)
    init = sitk.CenteredTransformInitializer(
        fixed, moving, 
        sitk.Euler3DTransform(),
        sitk.CenteredTransformInitializerFilter.GEOMETRY
    )
    reg.SetInitialTransform(init, inPlace=False)
    
    # 정합 실행
    tx = reg.Execute(fixed, moving)
    
    # 정합된 볼륨을 fixed 공간으로 리샘플
    # 가이드: BSpline 보간 사용 (스칼라 값에는 BSpline 권장, Nearest 아님)
    resampled = sitk.Resample(
        moving, fixed, tx, 
        sitk.sitkBSpline,  # BSpline 보간 (가이드 권장)
        0.0, 
        moving.GetPixelID()
    )
    
    # 가이드: 방향/원점/간격 메타데이터 보존 (fixed의 공간 사용)
    resampled.SetDirection(fixed.GetDirection())
    resampled.SetOrigin(fixed.GetOrigin())
    resampled.SetSpacing(fixed.GetSpacing())
    
    logger.info("Rigid registration completed")
    return resampled


def fuse_max(vols: List[sitk.Image]) -> sitk.Image:
    """
    여러 볼륨을 최대값으로 융합
    다평면 스택을 하나의 등방 볼륨으로 합침
    
    Args:
        vols: 정합된 볼륨 리스트
        
    Returns:
        sitk.Image: 융합된 볼륨
    """
    logger.info(f"Fusing {len(vols)} volumes using max fusion...")
    out = vols[0]
    for i, v in enumerate(vols[1:], 1):
        out = sitk.Maximum(out, v)
        logger.debug(f"Fused volume {i+1}/{len(vols)}")
    
    logger.info("Max fusion completed")
    return out

