from app.models.reconstruction import Reconstruction
from app.models.segment import Segment
from app.utils.storage import storage_client
from app.core.config import settings
from sqlalchemy.orm import Session
import SimpleITK as sitk
import numpy as np
from skimage import measure
import trimesh
import io
import tempfile
import os


def process_ai_segmentation(reconstruction: Reconstruction, segment: Segment, label: str, db: Session) -> dict:
    """MONAI 기반 AI 세그멘테이션 처리"""
    try:
        # 재구성된 볼륨 데이터 가져오기
        # 실제로는 재구성된 볼륨을 저장하거나 재구성해야 함
        # 여기서는 간단히 DICOM을 다시 읽어서 처리
        
        dicom_files = reconstruction.dicom_url.split(",")
        if not dicom_files:
            return {"status": "error", "message": "No DICOM files"}
        
        # DICOM 읽기
        reader = sitk.ImageSeriesReader()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            dicom_paths = []
            for dicom_obj in dicom_files:
                file_data = storage_client.get_file(dicom_obj)
                if not file_data:
                    continue
                file_path = os.path.join(temp_dir, os.path.basename(dicom_obj))
                with open(file_path, 'wb') as f:
                    f.write(file_data)
                dicom_paths.append(file_path)
            
            if not dicom_paths:
                return {"status": "error", "message": "Failed to download DICOM files"}
            
            reader.SetFileNames(dicom_paths)
            image = reader.Execute()
            image_array = sitk.GetArrayFromImage(image)
        
        # TODO: MONAI 모델을 사용한 세그멘테이션
        # 현재는 간단한 임계값 기반 세그멘테이션 사용
        # 실제로는 사전 학습된 MONAI 모델을 로드하여 사용
        
        # 예시: 간단한 레이블별 임계값 세그멘테이션
        threshold_map = {
            "brain": 80,
            "skull": 150,
            "soft_tissue": 40
        }
        
        threshold = threshold_map.get(label.lower(), 50)
        mask_array = (image_array > threshold).astype(np.uint8)
        
        # 마스크를 저장
        mask_obj_name = f"segmentation/{reconstruction.id}/{segment.id}/mask.nii.gz"
        mask_image = sitk.GetImageFromArray(mask_array)
        mask_image.CopyInformation(image)
        
        with tempfile.NamedTemporaryFile(suffix='.nii.gz', delete=False) as tmp_file:
            sitk.WriteImage(mask_image, tmp_file.name)
            with open(tmp_file.name, 'rb') as f:
                mask_data = f.read()
            os.unlink(tmp_file.name)
        
        storage_client.upload_file(mask_obj_name, mask_data, "application/octet-stream")
        
        # 마스크에서 메쉬 생성
        try:
            verts, faces, normals, values = measure.marching_cubes(
                mask_array.astype(np.float32),
                level=0.5,
                spacing=image.GetSpacing()[::-1]
            )
            
            mesh = trimesh.Trimesh(vertices=verts, faces=faces, vertex_normals=normals)
            
            # 세그멘테이션 메쉬 저장
            mesh_buffer = io.BytesIO()
            mesh.export(mesh_buffer, file_type='glb')
            mesh_data = mesh_buffer.getvalue()
            mesh_obj_name = f"segmentation/{reconstruction.id}/{segment.id}/mesh.glb"
            storage_client.upload_file(mesh_obj_name, mesh_data, "model/gltf-binary")
            
            return {
                "status": "success",
                "mask_url": mask_obj_name,
                "mesh_url": mesh_obj_name
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Mesh generation failed: {str(e)}"}
        
    except Exception as e:
        return {"status": "error", "message": f"Segmentation failed: {str(e)}"}

