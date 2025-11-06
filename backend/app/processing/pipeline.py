"""
재구성 파이프라인 모듈
다평면 MRI 스택을 정합→융합→세그멘테이션→메쉬 생성하는 전체 파이프라인
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional
import shutil
import subprocess
import SimpleITK as sitk
import numpy as np
import logging

from .io import list_series, load_series_by_files
from .preprocess import n4_bias, to_isotropic, body_mask, resample_to_spacing
from .register import rigid_register, fuse_max
from .segment import edge_mask, muscle_mask, segment_bone_25d
from .mesh import mask_to_mesh, export_meshes, mesh_from_mask

logger = logging.getLogger(__name__)


class TissueOption(str):
    """조직 타입 옵션"""
    pass


@dataclass
class ReconOptions:
    """재구성 옵션"""
    target_spacing: float = 1.2  # 등방성 리샘플 간격 (mm, 1.0-1.2 권장)
    tissues: List[str] = None  # ['bone', 'muscle']
    use_superres: bool = False  # 초해상 재구성 사용 여부
    output_dir: Optional[Path] = None
    recon_id: Optional[str] = None
    
    def __post_init__(self):
        if self.tissues is None:
            self.tissues = ['bone']


def _try_superres(niftis: List[Path], spacing: float, out_vol: Path) -> bool:
    """
    NiftyMIC 또는 SVRTK가 설치되어 있으면 호출하여 초해상 등방 볼륨을 생성.
    성공 시 True. 실패/미설치 시 False.
    """
    exe = shutil.which("niftymic_reconstruct_volume") or shutil.which("svrtk_reconstruct")
    if not exe:
        logger.info("Super-resolution tools (NiftyMIC/SVRTK) not found, skipping")
        return False
    
    try:
        # NiftyMIC 형태로 호출 (SVRTK면 적절히 바꾸어 사용 가능)
        cmd = ["niftymic_reconstruct_volume", "--stacks"] + [str(p) for p in niftis] + \
              ["--spacing", str(spacing), "--output", str(out_vol)]
        subprocess.check_call(cmd, timeout=3600)
        return out_vol.exists()
    except Exception as e:
        logger.warning(f"Super-resolution failed: {e}")
        return False


def _to_nifti(img: sitk.Image, path: Path) -> Path:
    """SimpleITK Image를 NIfTI로 변환"""
    try:
        import nibabel as nib
        arr = sitk.GetArrayFromImage(img)  # (z,y,x)
        
        # Affine 행렬 생성
        aff = np.eye(4, dtype=np.float32)
        sp = img.GetSpacing()
        aff[0, 0], aff[1, 1], aff[2, 2] = sp[0], sp[1], sp[2]
        
        # Origin 반영
        origin = img.GetOrigin()
        aff[0, 3] = origin[0]
        aff[1, 3] = origin[1]
        aff[2, 3] = origin[2]
        
        nib.Nifti1Image(arr, aff).to_filename(str(path))
        return path
    except ImportError:
        logger.warning("nibabel not available, skipping NIfTI conversion")
        raise


def run_reconstruction(
    series_roots: List[Path], 
    opts: ReconOptions,
    temp_dir: Optional[Path] = None
) -> Dict:
    """
    다평면 MRI 재구성 파이프라인 실행
    
    Args:
        series_roots: DICOM 시리즈 디렉터리 경로 리스트
        opts: 재구성 옵션
        temp_dir: 임시 디렉터리 (MinIO 파일 다운로드용)
        
    Returns:
        dict: {
            'gltf': Path,
            'stl': Path,
            'log': List[str],
            'meshes': List[trimesh.Trimesh]  # 선택적
        }
    """
    log = []
    
    # 1) 각 디렉터리에서 SeriesInstanceUID별 스택을 가져와 첫 스택만 사용(혼합 방지)
    # 단일 시리즈 우선 처리 (가이드 권장: 안정적인 기본 파이프라인)
    stacks = []
    for root in series_roots:
        try:
            groups = list_series(root)
            if not groups:
                log.append(f"No series found in {root}")
                continue
            
            # 첫 번째 시리즈만 사용 (혼합 방지)
            # 가이드: 한 가지 시리즈로 먼저 "깨끗한" 3D를 얻는 것이 목표
            sid, files = list(groups.items())[0]
            img = load_series_by_files(files)
            
            # 방향/원점/간격 메타데이터 확인
            spacing = img.GetSpacing()
            direction = np.array(img.GetDirection()).reshape(3, 3)
            origin = img.GetOrigin()
            
            logger.info(f"Series {sid[:16]}... metadata: spacing={spacing}, origin={origin}")
            logger.debug(f"Direction matrix: {direction}")
            
            stacks.append(img)
            log.append(f"Loaded series {sid[:16]}... with size {img.GetSize()} spacing {spacing}")
        except Exception as e:
            logger.error(f"Failed to load series from {root}: {e}", exc_info=True)
            log.append(f"Error loading {root}: {e}")
            continue
    
    if not stacks:
        raise ValueError("No DICOM stacks loaded")
    
    # 2) 전처리 + 등방 리샘플 + 바디마스크
    iso_vols = []
    masks = []
    for i, s in enumerate(stacks):
        try:
            # N4 bias correction
            v_bias = n4_bias(s)
            
            # 등방성 리샘플링
            v = to_isotropic(v_bias, opts.target_spacing)
            
            # 바디마스크 생성
            m = body_mask(v)
            
            iso_vols.append(v)
            masks.append(m)
            log.append(f"Isotropic resample [{i+1}]: size {v.GetSize()} spacing {v.GetSpacing()}")
        except Exception as e:
            logger.error(f"Preprocessing failed for stack {i+1}: {e}", exc_info=True)
            raise
    
    # 3) 정합 & 융합 (첫 볼륨을 기준)
    # 가이드: 단일 시리즈 우선 처리로 먼저 "깨끗한" 3D를 얻는 것이 목표
    if len(iso_vols) == 1:
        # 단일 스택인 경우 정합 불필요 (가이드 권장: 안정적인 기본 파이프라인)
        fused = iso_vols[0]
        log.append("Single stack, skipping registration (recommended for stable 3D)")
    else:
        # 가이드: 다평면 정합 시 올바른 방식 적용
        # 1) 기준 시리즈 고정
        fixed = iso_vols[0]
        logger.info(f"Fixed volume: size={fixed.GetSize()}, spacing={fixed.GetSpacing()}")
        regs = [fixed]
        
        # 2) 나머지 평면을 강체 등록으로 기준에 정합
        # 메모리 최적화: 정합 후 원본 볼륨을 즉시 해제하지 않지만, 융합은 점진적으로
        for i, mv in enumerate(iso_vols[1:], 1):
            try:
                logger.info(f"Registering stack {i+1}/{len(iso_vols)-1}...")
                reg_mv = rigid_register(fixed, mv)
                regs.append(reg_mv)
                log.append(f"Registered stack {i+1} to fixed volume")
                
                # 메모리 정리 힌트 (Python GC는 자동으로 처리하지만 명시적으로)
                del mv  # 정합된 볼륨만 유지
            except Exception as e:
                logger.warning(f"Registration failed for stack {i+1}: {e}, using original")
                regs.append(mv)
                log.append(f"Registration failed for stack {i+1}, using original")
        
        # 3) 융합: 가이드에서 median 또는 가중 평균 권장, 현재는 max 사용
        # 가이드: 전부 등방성으로 재표본화 후 융합
        # 메모리 최적화: fuse_max는 점진적으로 처리하므로 메모리 사용량이 증가하지 않음
        num_stacks = len(regs)
        logger.info(f"Fusing {num_stacks} volumes...")
        fused = fuse_max(regs)
        
        # 정합된 볼륨들 해제 (융합 결과만 유지)
        del regs
        del iso_vols  # 원본 볼륨들도 해제
        
        log.append(f"Rigid registration + max fusion complete ({num_stacks} stacks)")
        
        # 가이드: 정합 전후 모두 방향·간격·원점 엄격히 유지 확인
        logger.debug(f"Fused volume: size={fused.GetSize()}, spacing={fused.GetSpacing()}, "
                    f"origin={fused.GetOrigin()}")
    
    # 3-옵션) 초해상 (설치되어 있으면 사용)
    if opts.use_superres and len(iso_vols) > 1:
        try:
            niftis = []
            if temp_dir:
                tmpd = temp_dir / "tmp_nifti"
            else:
                tmpd = Path(opts.output_dir) / "tmp_nifti" if opts.output_dir else Path.cwd() / "tmp_nifti"
            tmpd.mkdir(parents=True, exist_ok=True)
            
            for i, v in enumerate(iso_vols):
                niftis.append(_to_nifti(v, tmpd / f"stack_{i}.nii.gz"))
            
            out_nifti = tmpd / "recon_iso.nii.gz"
            
            if _try_superres(niftis, opts.target_spacing, out_nifti):
                # 초해상 결과를 다시 SITK로
                import nibabel as nib
                ni = nib.load(str(out_nifti))
                arr = ni.get_fdata().astype("float32")
                fused = sitk.GetImageFromArray(arr)
                fused.SetSpacing((opts.target_spacing,) * 3)
                log.append("Super-resolution reconstruction applied")
            else:
                log.append("Super-resolution tool not found or failed, using fused volume")
        except Exception as e:
            logger.warning(f"Super-resolution pipeline failed: {e}")
            log.append(f"Super-resolution failed: {e}")
    
    # 4) 조직별 마스크 (2.5D 세그멘테이션 강제 + 마스크만 등방 업샘플)
    meshes = []
    
    if "bone" in opts.tissues:
        try:
            # === 2.5D 세그멘테이션 강제 파이프라인 ===
            import os
            force_25d = os.getenv("FORCE_25D", "1") != "0"
            
            # 원본 spacing 확인
            orig_spacing = fused.GetSpacing()  # (x, y, z) - SimpleITK 순서
            if len(orig_spacing) == 3:
                sz = orig_spacing[2]  # z 간격
                logger.info(f"PIPELINE: 2.5D segmentation enforced={force_25d} (z={sz:.2f}mm)")
                
                # 1) 세그멘트 전용 준등방 리샘플 (2.5D): in-plane만 고해상, z는 억제
                seg_spacing_z = min(sz, 3.0)
                seg_spacing = (0.8, 0.8, seg_spacing_z)  # (x, y, z)
                logger.info(f"Segmentation spacing (2.5D): {seg_spacing}")
                vol_seg = resample_to_spacing(fused, seg_spacing, order=1)  # 선형
                
                # 2) 2.5D 슬라이스-연속성 기반 세그멘트
                vol_arr = sitk.GetArrayFromImage(vol_seg).astype(np.float32)
                # 정규화 (0..1)
                vol_arr = (vol_arr - vol_arr.min()) / (vol_arr.max() - vol_arr.min() + 1e-6)
                bone_mask_25d = segment_bone_25d(vol_arr, logger=logger)
                
                # 3) 메싱 직전에만 등방 업샘플(마스크만, 최근접)
                iso_spacing = (opts.target_spacing, opts.target_spacing, opts.target_spacing)
                bone_mask_25d_sitk = sitk.GetImageFromArray(bone_mask_25d.astype(np.uint8))
                bone_mask_25d_sitk.CopyInformation(vol_seg)
                bone_mask_iso = resample_to_spacing(bone_mask_25d_sitk, iso_spacing, order=0)  # Nearest
                bone_mask_arr = sitk.GetArrayFromImage(bone_mask_iso).astype(bool)
                
                # 4) 메싱: 얇은 피질 보존(step_size=1)
                bone_mesh, stats = mesh_from_mask(bone_mask_arr, iso_spacing, logger=logger)
                meshes.append(bone_mesh)
                log.append(f"Bone mask (2.5D) -> mesh complete: {stats['faces']:,} faces")
            else:
                # fallback
                logger.warning("Invalid spacing, using fallback 3D segmentation")
                bmask = body_mask(fused)
                bone = edge_mask(fused, bmask)
                bone_mesh = mask_to_mesh(bone)
                meshes.append(bone_mesh)
                log.append("Bone mask -> mesh complete")
        except Exception as e:
            logger.error(f"Bone segmentation failed: {e}", exc_info=True)
            log.append(f"Bone segmentation failed: {e}")
    
    if "muscle" in opts.tissues:
        try:
            # muscle_mask는 body_mask가 필요
            if 'bmask' not in locals():
                bmask = body_mask(fused)
            mus = muscle_mask(fused, bmask)
            muscle_mesh = mask_to_mesh(mus)
            meshes.append(muscle_mesh)
            log.append("Muscle mask -> mesh complete")
        except Exception as e:
            logger.error(f"Muscle segmentation failed: {e}", exc_info=True)
            log.append(f"Muscle segmentation failed: {e}")
    
    if not meshes:
        raise ValueError("No meshes generated (check tissue options)")
    
    # 5) 내보내기
    if opts.output_dir and opts.recon_id:
        glb = Path(opts.output_dir) / f"{opts.recon_id}.glb"
        stl = Path(opts.output_dir) / f"{opts.recon_id}.stl"
    else:
        glb = Path("output") / "mesh.glb"
        stl = Path("output") / "mesh.stl"
    
    glb_path, stl_path = export_meshes(meshes, glb, stl)
    log.append(f"Exported: {glb_path.name}, {stl_path.name}")
    
    return {
        "gltf": glb_path,
        "stl": stl_path,
        "log": log,
        "meshes": meshes  # 선택적
    }

