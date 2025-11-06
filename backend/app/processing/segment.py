"""
세그멘테이션 모듈
GMM 기반 확률 지도 세그멘테이션 (뼈/근육)
가이드: 반전강도 + 그래디언트 + 로컬 대비 → GMM → 확률 지도 → 마스크
"""
import numpy as np
import SimpleITK as sitk
from skimage import morphology
from sklearn.mixture import GaussianMixture
from scipy.ndimage import gaussian_filter, binary_opening, binary_closing, binary_fill_holes, label, gaussian_gradient_magnitude, generate_binary_structure
import logging

logger = logging.getLogger(__name__)

# 0.8~8% 커버리지 밴드
TARGET_MIN, TARGET_MAX = 0.008, 0.08


def _largest_k_2d(mask2d: np.ndarray, k: int = 2) -> np.ndarray:
    """2D 마스크에서 상위 k개 연결 컴포넌트만 유지"""
    se = generate_binary_structure(2, 1)
    lbl, n = label(mask2d, structure=se)
    if n == 0:
        return mask2d
    vals, cnt = np.unique(lbl[lbl > 0], return_counts=True)
    if len(vals) <= k:
        return mask2d
    keep = vals[np.argsort(cnt)[-k:]]
    return np.isin(lbl, keep)


def segment_bone_25d(vol3d: np.ndarray, logger=None) -> np.ndarray:
    """
    2.5D 슬라이스 기반 뼈 세그멘트:
      - in-plane 저신호(=뼈) + 경사(엣지)
      - 형태학적 보정 + 상위 컴포넌트 유지
      - 이전 슬라이스와 최소 20% 이상 겹침(연속성)
      - 전역 커버리지 자동 튜닝(밴드: 0.8~8%)
    
    입력:  (Z,Y,X) float32 [0..1]
    출력:  (Z,Y,X) bool
    """
    assert vol3d.ndim == 3
    Z, H, W = vol3d.shape
    out = np.zeros_like(vol3d, dtype=bool)
    prev = None
    
    # 아주 느슨한 body
    body_thresh = np.percentile(vol3d, 5)
    body = vol3d > body_thresh
    if logger:
        logger.info(f"Body mask loose threshold: p5={body_thresh:.3f}")
    
    for z in range(Z):
        sl = vol3d[z].astype(np.float32)
        bm = body[z]
        # 뼈는 저신호: 음영 반전
        inv = 1.0 - sl
        gy, gx = np.gradient(inv)
        grad = np.hypot(gx, gy)
        
        # 초기 임계값을 더 엄격하게 (과포섭 방지)
        low_p, grad_p = 8, 85  # 12,80 → 8,85로 강화
        for _ in range(3):       # 최대 3회 자동 보정
            if bm.sum() == 0:
                break
            low = np.percentile(inv[bm], low_p)
            grd = np.percentile(grad[bm], grad_p)
            cand = (inv >= low) & (grad >= grd) & bm
            
            # 후처리: opening을 먼저 적용하여 작은 노이즈 제거
            cand = binary_opening(cand, iterations=1)  # 먼저 opening
            cand = binary_closing(cand, iterations=1)  # 그 다음 closing
            cand = binary_fill_holes(cand)
            cand = _largest_k_2d(cand, k=2)
            
            if prev is not None:
                inter = (cand & prev).sum()
                if inter < 0.2 * max(prev.sum(), 1):
                    # 연속성 부족 → 조건 강화/완화
                    low_p = max(5, low_p - 3)
                    grad_p = min(95, grad_p + 5)
                    continue
            break
        
        out[z] = cand
        prev = cand
    
    # 전역 커버리지 밴드 튜닝(1회)
    cov = out.sum() / max(body.sum(), 1)
    if logger:
        logger.info(f"Bone coverage(2.5D before banding): {cov*100:.2f}%")
    
    if cov > TARGET_MAX:
        # 과포섭 → 더 강력한 축소
        if logger:
            logger.info(f"Coverage {cov*100:.2f}% too high, applying aggressive reduction...")
        # 방법 1: opening을 더 많이 반복
        for z in range(Z):
            m = out[z]
            # opening을 3-4회 반복하여 강력하게 축소
            for _ in range(3):
                m = binary_opening(m, iterations=1)
            out[z] = m
        # 방법 2: 연결 컴포넌트 필터링 (상위 1개만 - 더 엄격하게)
        for z in range(Z):
            out[z] = _largest_k_2d(out[z], k=1)  # k=2 → k=1로 변경
        # 방법 3: 전체적으로 다시 한 번 opening
        for z in range(Z):
            out[z] = binary_opening(out[z], iterations=1)
        cov2 = out.sum() / max(body.sum(), 1)
        if logger:
            logger.info(f"Bone coverage(2.5D after banding): {cov2*100:.2f}%")
        # 여전히 높으면 경고
        if cov2 > TARGET_MAX:
            logger.warning(f"Coverage still high ({cov2*100:.2f}%), may need manual threshold adjustment")
    elif cov < TARGET_MIN:
        # 과소포섭 → closing으로 확장
        if logger:
            logger.info(f"Coverage {cov*100:.2f}% too low, applying closing...")
        for z in range(Z):
            m = out[z]
            m = binary_closing(m, iterations=1)
            out[z] = m
        cov2 = out.sum() / max(body.sum(), 1)
        if logger:
            logger.info(f"Bone coverage(2.5D after banding): {cov2*100:.2f}%")
    else:
        if logger:
            logger.info(f"Bone coverage(2.5D) within target range: {cov*100:.2f}%")
    
    return out


def _postprocess_bone_mask(bone_mask: np.ndarray, body_mask: np.ndarray) -> tuple:
    """
    뼈 마스크 후처리: 구멍과 작은 조각 정리
    - 연결 컴포넌트 중 상위 3개만 유지
    - closing → opening → hole fill
    - 커버리지 계산
    
    Returns:
        (bone_mask, coverage_ratio)
    """
    # 구조요소: 3D 6-연결 기반
    se = generate_binary_structure(3, 1)
    
    # closing → opening → hole fill
    bone = binary_closing(bone_mask, structure=se, iterations=2)
    bone = binary_opening(bone, structure=se, iterations=1)
    bone = binary_fill_holes(bone)
    
    # 연결 컴포넌트 중 큰 것만 남기기 (상위 3개)
    lbl, n_components = label(bone, structure=se)
    if n_components > 0:
        vals, counts = np.unique(lbl[lbl > 0], return_counts=True)
        if len(vals) > 3:
            keep = vals[np.argsort(counts)[-3:]]
            bone = np.isin(lbl, keep)
            logger.info(f"Kept top 3 components from {n_components} total")
        else:
            logger.info(f"Kept {len(vals)} components")
    
    # 커버리지 계산
    cov = bone.sum() / max(body_mask.sum(), 1)
    return bone, cov


def bone_mask_adaptive(volume_f32: np.ndarray, body_mask: np.ndarray) -> tuple:
    """
    커버리지 밴드 기반 자동 튜닝 뼈 마스크
    목표: 0.8% ~ 8% (body 대비)
    
    Returns:
        (bone_mask, coverage_ratio)
    """
    TARGET_MIN, TARGET_MAX = 0.008, 0.08  # 0.8% ~ 8%
    
    # 반전 강도 (MRI 뼈=저신호 → 반전하면 고신호)
    inv = 1.0 - volume_f32
    
    # 그래디언트 계산
    gy, gx, gz = np.gradient(inv)
    grad = np.sqrt(gx*gx + gy*gy + gz*gz)
    
    # 1차 임계: 저신호 + 고경사 (AND)
    lo_p, gr_p = 12, 80  # 시작점: 12%, 80%
    body_indices = body_mask > 0
    lo_thr = np.percentile(inv[body_indices], lo_p)
    gr_thr = np.percentile(grad[body_indices], gr_p)
    
    bone = (inv >= lo_thr) & (grad >= gr_thr) & body_indices
    bone, cov = _postprocess_bone_mask(bone, body_mask)
    
    # 커버리지 밴드 튜닝 1회
    if cov > TARGET_MAX:  # 과포섭 → 강화
        lo_p = max(5, lo_p - 4)  # 더 저신호만
        gr_p = min(95, gr_p + 10)  # 더 고경사만
        logger.info(f"Coverage {cov*100:.1f}% too high, tightening: lo_p={lo_p}, gr_p={gr_p}")
    elif cov < TARGET_MIN:  # 과소 → 완화
        lo_p = min(25, lo_p + 6)
        gr_p = max(60, gr_p - 10)
        logger.info(f"Coverage {cov*100:.1f}% too low, relaxing: lo_p={lo_p}, gr_p={gr_p}")
    else:
        logger.info(f"Bone coverage: {cov*100:.1f}% (target 0.8~8%) [lo_p={lo_p}, gr_p={gr_p}]")
        return bone, cov
    
    # 재시도
    lo_thr = np.percentile(inv[body_indices], lo_p)
    gr_thr = np.percentile(grad[body_indices], gr_p)
    bone = (inv >= lo_thr) & (grad >= gr_thr) & body_indices
    bone, cov = _postprocess_bone_mask(bone, body_mask)
    
    logger.info(f"Bone coverage: {cov*100:.1f}% (target 0.8~8%) [lo_p={lo_p}, gr_p={gr_p}]")
    return bone, cov


def _body_mask(arr: np.ndarray) -> np.ndarray:
    """
    부드럽게 + Otsu로 배경 제거
    """
    blurred = gaussian_filter(arr, sigma=0.8)
    arr_sitk = sitk.GetImageFromArray(blurred.astype(np.float32))
    thr_img = sitk.OtsuThreshold(arr_sitk, 0, 1, 200)
    thr_val = sitk.GetArrayFromImage(thr_img)
    # Otsu 결과가 이미지면 threshold 값 추출
    if thr_val.size > 1:
        # 마스크 이미지로 반환된 경우
        m = thr_val > 0
    else:
        # threshold 값으로 반환된 경우
        threshold = thr_val.item() if thr_val.size == 1 else np.median(blurred)
        m = blurred > threshold
    # 작은 조각 제거
    m = binary_closing(m, structure=np.ones((3, 3, 3)))
    return m


def segment_bone_and_muscle(vol_nii: sitk.Image, want=('bone',)) -> dict:
    """
    GMM 기반 뼈/근육 세그멘테이션
    특징: (반전강도, 그래디언트, 지역대비) → GMM → 확률 지도 → 마스크
    
    Args:
        vol_nii: 등방성 이미지
        want: 원하는 조직 목록 ('bone', 'muscle')
        
    Returns:
        dict: {'bone': np.ndarray, 'muscle': np.ndarray} (bool 마스크)
    """
    logger.info("Starting GMM-based segmentation...")
    
    vol = sitk.GetArrayFromImage(vol_nii).astype(np.float32)  # z,y,x
    
    # 정규화 (5-95 percentile)
    p5, p95 = np.percentile(vol, [5, 95])
    vol = (vol - p5) / (p95 - p5 + 1e-6)
    vol = np.clip(vol, 0, 1)
    
    # 바디 마스크 생성
    body = _body_mask(vol)
    if body.sum() < 1000:
        logger.warning(f"Body mask too small ({body.sum()} pixels), returning empty masks")
        return {'bone': np.zeros_like(vol, dtype=bool), 'muscle': np.zeros_like(vol, dtype=bool)}
    
    body_ratio = body.sum() / vol.size
    logger.info(f"Body mask: {body.sum()} / {vol.size} pixels ({body_ratio*100:.1f}%)")
    
    # 특징 추출: (반전강도, 그래디언트, 지역대비)
    inv = 1.0 - vol  # 반전 강도 (뼈는 저신호 → 반전하면 고신호)
    
    # 그래디언트 계산
    gy, gx, gz = np.gradient(vol)
    grad = np.sqrt(gx*gx + gy*gy + gz*gz)
    
    # 지역 대비 (로컬 평균과의 차이)
    local_mean = gaussian_filter(vol, sigma=1.6)
    local = np.abs(vol - local_mean)
    
    # 바디 내부 픽셀의 특징 추출
    feats = np.stack([
        inv[body],
        grad[body],
        local[body]
    ], axis=1)
    
    if feats.shape[0] < 100:
        logger.warning("Not enough features for GMM, returning empty masks")
        return {'bone': np.zeros_like(vol, dtype=bool), 'muscle': np.zeros_like(vol, dtype=bool)}
    
    # GMM 학습 (3-components: 지방/근육/뼈)
    try:
        gmm = GaussianMixture(n_components=3, covariance_type='full', random_state=0, max_iter=100)
        gmm.fit(feats)
        logger.info(f"GMM fitted with {gmm.n_components} components")
    except Exception as e:
        logger.error(f"GMM fitting failed: {e}", exc_info=True)
        return {'bone': np.zeros_like(vol, dtype=bool), 'muscle': np.zeros_like(vol, dtype=bool)}
    
    # 뼈 마스크: 커버리지 밴드 기반 자동 튜닝
    # GMM 기반 확률 지도 대신 직접 임계값 기반으로 전환 (더 안정적)
    bone, bone_ratio = bone_mask_adaptive(vol, body)
    
    bone_ratio_final = bone.sum() / float(body.sum())
    logger.info(f"Bone mask: {bone.sum()} / {vol.size} pixels ({bone_ratio_final*100:.1f}% of body, {bone_ratio*100:.1f}% coverage)")
    
    # 근육 마스크: GMM 기반으로 근육만 처리
    muscle = np.zeros_like(bone, dtype=bool)
    if 'muscle' in want:
        # GMM 결과 사용 (이미 계산됨)
        try:
            # 반전강도가 중간인 군집 선택 (지방은 높고, 뼈는 낮고, 근육은 중간)
            means = gmm.means_
            post_probs = gmm.predict_proba(feats)
            sorted_indices = np.argsort(means[:, 0])  # 반전강도 기준 정렬
            muscle_idx = sorted_indices[1]  # 중간값 군집 = 근육
            
            muscle_post = np.zeros_like(vol, dtype=np.float32)
            muscle_post[body] = post_probs[:, muscle_idx]
            
            muscle = muscle_post > np.percentile(muscle_post[body], 65)
            muscle = binary_opening(muscle, structure=np.ones((2, 2, 2)))
            
            muscle_ratio = muscle.sum() / float(body.sum())
            logger.info(f"Muscle mask: {muscle.sum()} / {vol.size} pixels ({muscle_ratio*100:.1f}% of body)")
        except Exception as e:
            logger.warning(f"Muscle segmentation failed: {e}, returning empty mask")
    
    return {'bone': bone, 'muscle': muscle}


def edge_mask(img_iso: sitk.Image, body: sitk.Image) -> sitk.Image:
    """
    GMM 기반 세그멘테이션 사용 (레거시 호환)
    """
    result = segment_bone_and_muscle(img_iso, want=('bone',))
    bone_mask = result['bone']
    
    out = sitk.GetImageFromArray(bone_mask.astype(np.uint8))
    out.CopyInformation(img_iso)
    return out


def muscle_mask(img_iso: sitk.Image, body: sitk.Image) -> sitk.Image:
    """
    GMM 기반 세그멘테이션 사용 (레거시 호환)
    """
    result = segment_bone_and_muscle(img_iso, want=('bone', 'muscle'))
    muscle_mask = result['muscle']
    
    out = sitk.GetImageFromArray(muscle_mask.astype(np.uint8))
    out.CopyInformation(img_iso)
    return out
