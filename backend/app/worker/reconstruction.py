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
    
    # 리샘플링 정책: 2D 슬라이스(두께≥3mm)는 등방 리샘플 금지
    is_2d_thick_slices = slice_spacing >= 3.0
    
    # 등방성 리샘플링 판단
    # keep_original_spacing이 명시적으로 False면 항상 리샘플
    # None이면 자동 판단: 비등방성이 크면 리샘플 (단, 2D 두꺼운 슬라이스 제외)
    if keep_original_spacing is False:
        should_resample = True
    elif keep_original_spacing is True:
        should_resample = False
    else:
        # 자동 판단
        if is_2d_thick_slices:
            # 2D 두꺼운 슬라이스: 등방 리샘플 금지 (원본 해상도 유지)
            should_resample = False
            logger.info(f"2D thick slices (≥3mm) detected: Skipping isotropic resampling to preserve quality")
        else:
            # 일반 케이스: in-plane spacing과 slice spacing 차이가 크면 리샘플
            anisotropy_ratio = max(in_plane, slice_spacing) / min(in_plane, slice_spacing)
            should_resample = anisotropy_ratio > 1.5  # 비율이 1.5배 이상이면 리샘플
            logger.info(f"Anisotropy ratio: {anisotropy_ratio:.2f}, will resample: {should_resample}")
    
    # 품질 경고
    if anisotropy_ratio_r > 3:
        logger.warning(f"⚠️ High anisotropy ratio (r={anisotropy_ratio_r:.2f} > 3): Low quality expected. SVR/3D sequence recommended.")
    
    if not should_resample:
        logger.info("Keeping original spacing (isotropic or 2D thick slices)")
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


def preprocess_mri_for_surface(img_iso: sitk.Image, use_n4_bias_correction=True):
    """
    MRI 이미지 전처리: N4 bias correction → 윈도잉 → 가우시안 스무딩 → Otsu 임계값 → 연결성 필터
    """
    # 0) N4 Bias Field Correction (선택적)
    if use_n4_bias_correction:
        try:
            logger.info("Applying N4 bias field correction...")
            # SimpleITK의 N4BiasFieldCorrectionImageFilter 사용
            corrector = sitk.N4BiasFieldCorrectionImageFilter()
            corrector.SetMaximumNumberOfIterations([50, 50, 50, 50])  # 4 levels
            img_bias_corrected = corrector.Execute(img_iso)
            logger.info("N4 bias correction completed")
            img_for_processing = img_bias_corrected
        except Exception as e:
            logger.warning(f"N4 bias correction failed: {e}, proceeding without correction")
            img_for_processing = img_iso
    else:
        img_for_processing = img_iso
    
    arr = sitk.GetArrayFromImage(img_for_processing).astype(np.float32)  # (z, y, x)
    
    logger.info(f"Original array range: [{arr.min():.1f}, {arr.max():.1f}]")
    
    # 1) Intensity windowing (백분위 기반)
    p1, p99 = np.percentile(arr, (1, 99))
    logger.info(f"Percentiles: 1%={p1:.1f}, 99%={p99:.1f}")
    arr_windowed = np.clip((arr - p1) / max(p99 - p1, 1e-6), 0, 1)
    
    # 2) 비등방 가우시안 스무딩 (z 방향만 더 세게)
    spacing = np.array(img_for_processing.GetSpacing())
    mean_in_plane = (spacing[0] + spacing[1]) / 2
    slice_spacing = spacing[2]
    
    # 원본 스택이 두꺼운 경우 z 방향 스무딩 강화
    if slice_spacing > mean_in_plane * 2:
        # z 방향 스무딩: slice_thickness * 0.4-0.6
        sigma_z = slice_spacing * 0.5
        sigma_xy = min(mean_in_plane * 0.8, 1.0)
        logger.info(f"Anisotropic smoothing: σx=σy={sigma_xy:.3f}mm, σz={sigma_z:.3f}mm (z-direction enhanced)")
        # SimpleITK는 등방성 스무딩만 지원하므로 가중 평균 사용
        sigma_mm = np.sqrt((sigma_xy**2 + sigma_xy**2 + sigma_z**2) / 3)
    else:
        sigma_mm = min(mean_in_plane * 0.8, 1.0)
        logger.info(f"Isotropic smoothing: σ={sigma_mm:.3f}mm")
    
    smoothed_img = sitk.SmoothingRecursiveGaussian(img_for_processing, sigma=sigma_mm)
    smoothed = sitk.GetArrayFromImage(smoothed_img).astype(np.float32)
    
    # 스무딩된 배열에 윈도잉 적용
    p1_smooth, p99_smooth = np.percentile(smoothed, (1, 99))
    smoothed_windowed = np.clip((smoothed - p1_smooth) / max(p99_smooth - p1_smooth, 1e-6), 0, 1)
    
    logger.info(f"After smoothing (σ={sigma_mm}mm): range=[{smoothed_windowed.min():.3f}, {smoothed_windowed.max():.3f}]")
    
    # 3) 3D Otsu 임계값
    try:
        t = threshold_otsu(smoothed_windowed)
        logger.info(f"Otsu threshold: {t:.3f}")
        mask = smoothed_windowed > t
    except Exception as e:
        logger.warning(f"Otsu threshold failed: {e}, using median")
        t = np.median(smoothed_windowed)
        mask = smoothed_windowed > t
    
    logger.info(f"Binary mask: {np.sum(mask)} / {mask.size} pixels ({100*np.sum(mask)/mask.size:.1f}%)")
    
    # 4) 작은 덩어리 제거 + 연결성 분석
    structure = ndi.generate_binary_structure(3, 2)
    mask = ndi.binary_opening(mask, structure=structure)
    
    lbl, n_components = ndi.label(mask)
    counts = np.bincount(lbl.ravel())
    counts[0] = 0  # 배경 제외
    
    logger.info(f"Found {n_components} connected components")
    
    # 성분 수 경고
    if n_components > 2:
        logger.warning(f"⚠️ Multiple components ({n_components}): Table/background suspected. Will apply ROI cropping after selection.")
    
    if n_components > 0:
        # 가장 큰 성분 찾기
        largest_idx = np.argmax(counts)
        largest_size = counts[largest_idx]
        
        # 메모리 절약: 작은 컴포넌트는 즉시 제외 (최대 성분의 1% 미만)
        min_size_threshold = largest_size * 0.01
        
        # 중심부에 가까운 성분 우선 선택 (외곽 테이블/코일 제외)
        center = np.array(arr.shape) / 2
        best_component = largest_idx
        best_score = largest_size
        
        # 최대 성분의 30% 이상인 것만 고려 (메모리 절약)
        for comp_id in range(1, len(counts)):
            if counts[comp_id] < largest_size * 0.3:
                continue
            if counts[comp_id] < min_size_threshold:
                continue
            
            # 무게중심 계산 (메모리 효율적: 샘플링)
            comp_mask = (lbl == comp_id)
            # 모든 좌표를 저장하지 않고 샘플링하여 메모리 절약
            comp_coords = np.argwhere(comp_mask)
            if len(comp_coords) == 0:
                continue
            
            # 너무 많은 좌표면 샘플링
            if len(comp_coords) > 100000:
                step = len(comp_coords) // 50000
                comp_coords = comp_coords[::step]
            
            centroid = comp_coords.mean(axis=0)
            dist_to_center = np.linalg.norm(centroid - center)
            
            # 점수: 크기 - 중심으로부터의 거리 (정규화)
            max_dist = np.linalg.norm(center)
            normalized_dist = dist_to_center / max_dist if max_dist > 0 else 1.0
            score = counts[comp_id] * (1.0 - normalized_dist * 0.3)  # 거리 페널티 30%
            
            if score > best_score:
                best_score = score
                best_component = comp_id
                logger.info(f"Component {comp_id} closer to center: size={counts[comp_id]}, dist={dist_to_center:.1f}, score={score:.0f}")
        
        keep = best_component
        logger.info(f"Keeping component: {keep} (size={counts[keep]} voxels, score={best_score:.0f})")
        mask = (lbl == keep)
        
        # 작은 컴포넌트 정리 (메모리 해제)
        del lbl, counts
        
        # 컴포넌트 바운딩 박스 로그
        coords = np.argwhere(mask)
        if len(coords) > 0:
            bbox_min = coords.min(axis=0)
            bbox_max = coords.max(axis=0)
            bbox_size = bbox_max - bbox_min
            logger.info(f"Bounding box: min={bbox_min}, max={bbox_max}, size={bbox_size}")
    else:
        logger.warning("No components found after labeling")
    
    # 5) Closing (구멍 메우기) - 반경 1-2
    mask = ndi.binary_closing(mask, structure=ndi.generate_binary_structure(3, 1))
    
    kept_components = 1 if n_components > 0 else 0
    logger.info(f"Final mask: {np.sum(mask)} / {mask.size} pixels ({100*np.sum(mask)/mask.size:.1f}%), kept_components={kept_components}")
    
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
    
    # 7) 메시 스무딩/간소화 (후처리)
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
            
            # Step 1: SeriesInstanceUID별로 그룹화
            by_series = group_by_series_uid(dicom_paths)
            
            if not by_series:
                return {"status": "error", "message": "No valid series found (missing SeriesInstanceUID)"}
            
            # QC 게이트 1: 단일 SeriesInstanceUID만 허용
            if len(by_series) > 1:
                series_uids = list(by_series.keys())
                return {
                    "status": "error", 
                    "message": f"Mixed series detected ({len(by_series)} different SeriesInstanceUIDs). Only single series reconstruction supported. Series UIDs: {[uid[:16]+'...' for uid in series_uids]}"
                }
            
            # 단일 Series 선택
            selected_series_uid = list(by_series.keys())[0]
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
                
                # 마스크 바운딩박스로 이미지 크롭
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
                    
                    # 크롭된 이미지로 메쉬 생성
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
