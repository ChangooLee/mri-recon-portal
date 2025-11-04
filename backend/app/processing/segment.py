"""
세그멘테이션 모듈
경사도 기반 뼈 마스크, K-means 기반 근육 마스크
가이드: 그래디언트(경계) × 저신호 결합 + 연결성분 필터
"""
import numpy as np
import SimpleITK as sitk
from skimage import morphology
from sklearn.cluster import KMeans
from scipy.ndimage import gaussian_gradient_magnitude, binary_closing, binary_opening, generate_binary_structure, label
import logging

logger = logging.getLogger(__name__)


def edge_mask(img_iso: sitk.Image, body: sitk.Image) -> sitk.Image:
    """
    경사도 기반 뼈 마스크 생성
    MRI에서 뼈는 검은 테두리(피질골)로 보이므로 경계강도 기반으로 추출
    가이드: 그래디언트(경계) × 저신호 결합 + 연결성분으로 큰 두 성분만 유지
    
    Args:
        img_iso: 등방성 이미지
        body: 바디 마스크
        
    Returns:
        sitk.Image: 뼈 마스크 (binary)
    """
    logger.info("Creating bone mask using gradient magnitude...")
    
    # 가이드: scipy.ndimage.gaussian_gradient_magnitude 사용
    from scipy.ndimage import gaussian_gradient_magnitude
    
    arr = sitk.GetArrayFromImage(img_iso).astype(np.float32)
    body_arr = sitk.GetArrayFromImage(body).astype(bool)
    
    # 그래디언트 계산 (경계강도)
    edge = gaussian_gradient_magnitude(arr, sigma=1.0)
    
    # 바디 안쪽 영역만 고려
    gradient_in_body = edge.copy()
    gradient_in_body[~body_arr] = 0
    
    # 상위 15% 경계만 선택 (뼈 경계는 강한 경사도를 가짐)
    # 가이드: 경계 × 저신호 결합
    non_zero_gradients = gradient_in_body[gradient_in_body > 0]
    if len(non_zero_gradients) > 0:
        thresh = np.percentile(non_zero_gradients, 70)  # 가이드: 70th percentile
        logger.info(f"Gradient threshold (70th percentile): {thresh:.3f}")
        
        # 저신호 영역도 고려 (MRI에서 피질골은 저신호)
        low_signal = arr < np.percentile(arr[body_arr], 30)
        
        # 경계 + 저신호 결합
        m = (gradient_in_body >= thresh) & body_arr & low_signal
    else:
        logger.warning("No gradients found in body mask, using fallback")
        m = body_arr
    
    # 3D 형태학으로 다듬기 (가이드: 열기 → 제거 → 닫기)
    structure = generate_binary_structure(3, 1)
    m = binary_opening(m, structure=structure)  # 가이드: 열기 먼저
    m = morphology.remove_small_objects(m, 5000)  # 작은 파편 제거
    m = binary_closing(m, structure=structure)  # 가이드: 닫기
    
    # 가이드: 연결성분 필터로 가장 큰 1-2개 성분만 유지
    lbl, n_components = label(m)
    if n_components > 0:
        counts = np.bincount(lbl.ravel())
        counts[0] = 0
        # 가장 큰 1-2개 성분만 선택
        keep_labels = np.argsort(counts)[-2:]  # 상위 2개
        m = np.isin(lbl, keep_labels)
        logger.info(f"Kept {len(keep_labels)} largest components from {n_components} total")
    
    out = sitk.GetImageFromArray(m.astype(np.uint8))
    out.CopyInformation(img_iso)  # 방향/원점/간격 메타데이터 보존
    
    bone_voxels = np.sum(m)
    logger.info(f"Bone mask: {bone_voxels} / {m.size} pixels ({100*bone_voxels/m.size:.1f}%)")
    
    return out


def muscle_mask(img_iso: sitk.Image, body: sitk.Image) -> sitk.Image:
    """
    K-means 기반 근육 마스크 생성
    바디마스크 내부에서 3클러스터(지방/근육/수분)로 분리한 뒤 근육 클러스터 선택
    
    Args:
        img_iso: 등방성 이미지
        body: 바디 마스크
        
    Returns:
        sitk.Image: 근육 마스크 (binary)
    """
    logger.info("Creating muscle mask using K-means clustering...")
    
    arr = sitk.GetArrayFromImage(img_iso).astype(np.float32)
    b = sitk.GetArrayFromImage(body).astype(bool)
    
    # 바디 영역의 픽셀값 추출
    X = arr[b].reshape(-1, 1)
    
    if X.size < 10:
        logger.warning("Not enough pixels in body mask, returning body mask")
        return body
    
    # K-means 3클러스터 (지방/근육/수분)
    try:
        km = KMeans(n_clusters=3, n_init=10, random_state=0).fit(X)
        labels = np.zeros_like(arr, dtype=np.uint8)
        labels[b] = km.labels_ + 1
        
        # 각 클러스터의 평균값 계산
        means = [arr[labels == (i + 1)].mean() for i in range(3)]
        
        # 가장 중간값(근육) 클러스터 선택
        mid_idx = np.argsort(means)[1] + 1
        m = (labels == mid_idx)
        
        # 형태학적 정제
        m = morphology.binary_opening(m, morphology.ball(2))
        m = morphology.remove_small_objects(m, 5000)
        
        out = sitk.GetImageFromArray(m.astype(np.uint8))
        out.CopyInformation(img_iso)
        
        muscle_voxels = np.sum(m)
        logger.info(f"Muscle mask: {muscle_voxels} / {m.size} pixels ({100*muscle_voxels/m.size:.1f}%)")
        
        return out
    except Exception as e:
        logger.warning(f"K-means clustering failed: {e}, returning body mask")
        return body

