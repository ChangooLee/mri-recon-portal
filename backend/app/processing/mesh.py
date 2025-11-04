"""
메쉬 생성 및 내보내기 모듈
마스크에서 3D 메쉬 생성, 좌표 변환, GLB/STL 내보내기
"""
import numpy as np
import SimpleITK as sitk
from skimage import measure
import trimesh
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def mask_to_mesh(mask_img: sitk.Image) -> trimesh.Trimesh:
    """
    마스크 이미지에서 3D 메쉬 생성
    Marching cubes → 좌표 변환 → 정리
    
    Args:
        mask_img: 바이너리 마스크 이미지
        
    Returns:
        trimesh.Trimesh: 3D 메쉬
    """
    a = sitk.GetArrayFromImage(mask_img).astype(np.uint8)
    
    if a.max() == 0:
        logger.warning("Empty mask, returning empty mesh")
        return trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3)))
    
    logger.info("Starting marching cubes algorithm...")
    # 가이드: marching_cubes에 spacing 반드시 전달 (이방성 보정 필수)
    # skimage의 marching_cubes는 (z,y,x) 순서 → spacing도 역순
    spacing = mask_img.GetSpacing()  # (x, y, z) - SimpleITK 순서
    
    # spacing이 제대로 전달되는지 확인
    if spacing is None or len(spacing) != 3:
        raise ValueError(f"Invalid spacing: {spacing}. Must be 3D spacing tuple.")
    
    logger.debug(f"Marching cubes spacing: {spacing} (x,y,z) -> {spacing[::-1]} (z,y,x)")
    
    verts, faces, normals, _ = measure.marching_cubes(
        a, level=0.5, spacing=spacing[::-1]  # (z, y, x) - skimage 순서
    )
    
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
    mesh = trimesh.Trimesh(vertices=p_three, faces=faces, vertex_normals=normals, process=True)
    
    # 메쉬 정리
    mesh.remove_degenerate_faces()
    mesh.remove_unreferenced_vertices()
    
    # 가장 큰 컴포넌트만 선택
    comps = mesh.split(only_watertight=False)
    if len(comps) > 0:
        mesh = max(comps, key=lambda c: len(c.faces))
        logger.info(f"Kept largest component: {len(mesh.faces)} faces")
    
    # 간소화 (50% decimation)
    target_faces = max(int(len(mesh.faces) * 0.5), 1000)
    logger.info(f"Simplifying mesh to {target_faces} faces...")
    try:
        mesh = mesh.simplify_quadratic_decimation(target_faces)
        logger.info(f"Final mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
    except Exception as e:
        logger.warning(f"Mesh simplification failed: {e}, using unsimplified mesh")
    
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

