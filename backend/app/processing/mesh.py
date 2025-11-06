"""
메쉬 생성 및 내보내기 모듈
SDF 기반 메싱 + Taubin smoothing으로 블록 아티팩트 제거
"""
import numpy as np
import SimpleITK as sitk
from skimage import measure
import trimesh
from pathlib import Path
from scipy.ndimage import distance_transform_edt as edt, gaussian_filter
import logging

logger = logging.getLogger(__name__)


def mesh_from_mask(mask: np.ndarray, spacing, logger=None):
    """
    바이너리 마스크에서 얇은 피질을 최대 보존하여 메쉬 생성.
    - 가우시안 0.6로 살짝만 블러
    - SDF(level=0.0) 기반 marching_cubes(step_size=1)
    - Taubin 1회, 구멍 작게만 메움, 과한 디시메이션 금지
    
    Args:
        mask: (Z, Y, X) bool 배열
        spacing: (x, y, z) 또는 (z, y, x) - skimage는 (z, y, x) 순서
        logger: 로거 인스턴스
        
    Returns:
        (mesh, stats)
    """
    a = mask.astype(np.float32)
    if a.sum() < 2000:
        raise ValueError("Mask too small for mesh.")
    
    a = gaussian_filter(a, sigma=0.6)
    sdf = edt(a >= 0.5) - edt(a < 0.5)
    
    # spacing이 (x,y,z)면 (z,y,x)로 변환
    if len(spacing) == 3:
        spacing_zyx = spacing[::-1]  # (x,y,z) -> (z,y,x)
    else:
        spacing_zyx = spacing
    
    # step_size=1 강제 (피질 보존)
    import os
    step = int(os.getenv("MC_STEP_SIZE", "1"))
    if step != 1:
        logger.warning(f"MC_STEP_SIZE={step} requested, but forcing step_size=1 for cortical preservation")
        step = 1
    
    verts, faces, normals, _ = measure.marching_cubes(
        sdf, level=0.0, spacing=spacing_zyx, step_size=step
    )
    
    # 좌표계 변환 (LPS -> Three.js)
    p_three = np.zeros_like(verts)
    p_three[:, 0] = -verts[:, 0]  # -x
    p_three[:, 1] = verts[:, 2]   # +z
    p_three[:, 2] = verts[:, 1]   # +y
    # 단위 변환 (mm -> m)
    p_three = p_three * 0.001
    
    mesh = trimesh.Trimesh(vertices=p_three, faces=faces, vertex_normals=normals, process=False)
    
    try:
        trimesh.smoothing.filter_taubin(mesh, lamb=0.5, nu=-0.53, iterations=1)
    except Exception:
        pass
    
    try:
        trimesh.repair.fill_holes(mesh, max_hole_size=80)
    except Exception:
        pass
    
    # 디시메이션은 얼굴수 과도할 때만 25% 축소
    try:
        if mesh.faces.shape[0] > 150_000:
            mesh = mesh.simplify_quadratic_decimation(int(mesh.faces.shape[0]*0.75))
    except Exception:
        if logger:
            logger.info("Decimation skipped (no backend).")
    
    stats = {"faces": int(mesh.faces.shape[0])}
    if logger:
        logger.info(f"Mesh faces: {stats['faces']:,}")
    
    return mesh, stats


def mask_to_mesh(mask_img: sitk.Image) -> trimesh.Trimesh:
    """
    마스크 이미지에서 3D 메쉬 생성 (SDF 기반)
    부호 거리장(SDF) 0-등치면 추출 → Taubin smoothing → decimation
    
    Args:
        mask_img: 바이너리 마스크 이미지
        
    Returns:
        trimesh.Trimesh: 3D 메쉬
    """
    a = sitk.GetArrayFromImage(mask_img).astype(np.uint8)
    
    if a.max() == 0:
        logger.warning("Empty mask, returning empty mesh")
        return trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3)))
    
    logger.info("Creating SDF (Signed Distance Field) for smooth meshing...")
    spacing = mask_img.GetSpacing()  # (x, y, z) - SimpleITK 순서
    
    if spacing is None or len(spacing) != 3:
        raise ValueError(f"Invalid spacing: {spacing}. Must be 3D spacing tuple.")
    
    # 안티앨리어싱: SDF 전에 이진 마스크에 가우시안 필터
    a_f = gaussian_filter(a.astype(np.float32), sigma=0.8)
    a_binary = a_f > 0.5
    
    # 부호 거리장 계산: 내부는 +, 외부는 -
    # scipy.ndimage.distance_transform_edt는 (z, y, x) 순서
    sdf_pos = edt(a_binary, sampling=spacing[::-1])  # 내부 거리
    sdf_neg = edt(~a_binary, sampling=spacing[::-1])  # 외부 거리
    sdf = sdf_pos - sdf_neg
    
    logger.info(f"SDF range: [{sdf.min():.3f}, {sdf.max():.3f}]")
    
    # SDF 0-등치면 추출 (level=0.0)
    # step_size=2로 marching_cubes 자체에서 면수 감소 (의존성 없음)
    level = 0.0
    try:
        verts, faces, normals, _ = measure.marching_cubes(
            sdf, level=level, spacing=spacing[::-1], step_size=2  # step_size로 면수 감소
        )
    except ValueError as e:
        if "Surface level must be within volume data range" in str(e):
            # 데이터 범위 문제 - level 조정
            data_min, data_max = sdf.min(), sdf.max()
            level = (data_min + data_max) / 2.0
            logger.warning(f"Marching cubes failed with level 0.0, retrying with level {level}")
            verts, faces, normals, _ = measure.marching_cubes(
                sdf, level=level, spacing=spacing[::-1]
            )
        else:
            raise
    
    logger.info(f"Marching cubes generated {len(verts)} vertices and {len(faces)} faces")
    
    # (z,y,x) → (x,y,z)로 변환
    verts_xyz = verts[:, [2, 1, 0]]
    
    # Spacing은 이미 marching_cubes에서 적용되었으므로 verts_xyz는 mm 단위
    # 좌표 변환: LPS → Three.js (mm → m)
    origin = np.array(mask_img.GetOrigin())  # LPS 좌표
    direction = np.array(mask_img.GetDirection()).reshape(3, 3)
    
    # LPS 좌표로 변환
    p_lps = (direction @ verts_xyz.T).T + origin
    
    # Three.js 좌표 변환 + mm → m 변환
    # Three.js: x = R = -L, y = S, z = P
    p_three = np.column_stack([
        -p_lps[:, 0] * 0.001,  # R = -L, mm → m
        p_lps[:, 2] * 0.001,   # S, mm → m
        p_lps[:, 1] * 0.001    # z = P, mm → m
    ])
    
    logger.info(f"Converted to Three.js coordinates (m units)")
    
    # Trimesh 메쉬 생성
    mesh = trimesh.Trimesh(vertices=p_three, faces=faces, vertex_normals=normals, process=False)
    
    # 메쉬 정리
    mesh.remove_degenerate_faces()
    mesh.remove_unreferenced_vertices()
    
    # 가장 큰 컴포넌트만 선택
    comps = mesh.split(only_watertight=False)
    if len(comps) > 0:
        mesh = max(comps, key=lambda c: len(c.faces))
        logger.info(f"Kept largest component: {len(mesh.faces)} faces")
    
    # Taubin smoothing → 블록/톱니 제거 (과스무딩 금지: 2회로 완화)
    logger.info("Applying Taubin smoothing to remove block artifacts...")
    original_faces = len(mesh.faces)
    try:
        trimesh.smoothing.filter_taubin(mesh, lamb=0.5, nu=-0.53, iterations=2)  # 3→2회로 완화
        logger.info(f"Taubin smoothing completed (2 iterations, {original_faces} -> {len(mesh.faces)} faces)")
    except Exception as e:
        logger.warning(f"Taubin smoothing failed: {e}, skipping")
    
    # 작은 구멍 메우기
    try:
        mesh.fill_holes()
        logger.info("Filled small holes in mesh")
    except Exception as e:
        logger.warning(f"Hole filling failed: {e}, skipping")
    
    # 간소화: 선택적 (step_size로 이미 감소했으므로)
    # open3d 없이도 동작하는 경우만 시도
    if len(mesh.faces) > 200000:  # 20만 이상일 때만 시도
        target_faces = max(8000, int(mesh.faces.shape[0] * 0.7))  # 30%만 줄임
        logger.info(f"Simplifying mesh to {target_faces} faces (70% of original {len(mesh.faces)} faces)...")
        try:
            mesh = mesh.simplify_quadratic_decimation(target_faces)
            logger.info(f"Final mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces (decimation: {original_faces} -> {len(mesh.faces)})")
        except Exception as e:
            logger.info(f"Mesh simplification skipped (may require open3d): {e}, using current mesh")
            logger.info(f"Final mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces (step_size=2 already reduced from {original_faces})")
    else:
        logger.info(f"Final mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces (step_size=2 reduction sufficient)")
    
    return mesh


def export_meshes(meshes: list, out_glb: Path, out_stl: Path) -> tuple:
    """
    여러 메쉬를 하나로 합쳐서 GLB/STL 내보내기
    
    Args:
        meshes: 메쉬 리스트
        out_glb: 출력 GLB 경로
        out_stl: 출력 STL 경로
        
    Returns:
        tuple: (glb_path, stl_path)
    """
    if not meshes:
        raise ValueError("No meshes to export")
    
    logger.info(f"Exporting {len(meshes)} mesh(es)...")
    
    # 여러 조직을 하나로 합침
    combo = trimesh.util.concatenate(meshes)
    
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    out_stl.parent.mkdir(parents=True, exist_ok=True)
    
    # GLB 내보내기
    combo.export(str(out_glb))
    logger.info(f"Exported GLB: {out_glb}")
    
    # STL 내보내기
    combo.export(str(out_stl))
    logger.info(f"Exported STL: {out_stl}")
    
    return out_glb, out_stl
