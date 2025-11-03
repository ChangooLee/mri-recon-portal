from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.reconstruction import Reconstruction
from app.utils.storage import storage_client
import SimpleITK as sitk
import numpy as np
from PIL import Image
import io

router = APIRouter()


@router.get("/viewer/{reconstruction_id}/slice/{slice_index}")
async def get_dicom_slice(
    reconstruction_id: str,
    slice_index: int,
    window_center: float = Query(None, description="Window center for windowing"),
    window_width: float = Query(None, description="Window width for windowing"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """DICOM 슬라이스를 PNG 이미지로 반환"""
    try:
        reconstruction = db.query(Reconstruction).filter(
            Reconstruction.id == reconstruction_id,
            Reconstruction.user_id == current_user.id
        ).first()
        
        if not reconstruction:
            raise HTTPException(status_code=404, detail="Reconstruction not found")
        
        if not reconstruction.dicom_url:
            raise HTTPException(status_code=404, detail="DICOM files not found")
        
        # DICOM 파일 목록
        dicom_files = reconstruction.dicom_url.split(",")
        
        if slice_index < 0 or slice_index >= len(dicom_files):
            raise HTTPException(status_code=400, detail="Slice index out of range")
        
        # 해당 슬라이스 DICOM 파일 읽기
        dicom_obj = dicom_files[slice_index]
        file_data = storage_client.get_file(dicom_obj)
        
        if not file_data:
            raise HTTPException(status_code=404, detail="DICOM file not found")
        
        # 임시 파일로 저장하여 SimpleITK로 읽기
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(suffix='.dcm', delete=False) as tmp_file:
            tmp_file.write(file_data)
            tmp_path = tmp_file.name
        
        try:
            # SimpleITK로 DICOM 읽기
            reader = sitk.ImageFileReader()
            reader.SetFileName(tmp_path)
            image = reader.Execute()
            
            # NumPy 배열로 변환
            image_array = sitk.GetArrayFromImage(image)
            
            # 2D 슬라이스 추출 (SimpleITK는 (z, y, x) 순서)
            if len(image_array.shape) == 3:
                slice_2d = image_array[0, :, :]  # 첫 번째 슬라이스
            elif len(image_array.shape) == 2:
                slice_2d = image_array
            else:
                slice_2d = image_array.flatten().reshape(1, -1)
            
            # Windowing (HU 값 조정)
            if window_center is None or window_width is None:
                # 자동 윈도잉: 이미지 범위 기반
                img_min = float(slice_2d.min())
                img_max = float(slice_2d.max())
                window_center = (img_min + img_max) / 2
                window_width = img_max - img_min
            
            window_min = window_center - window_width / 2
            window_max = window_center + window_width / 2
            
            # 윈도잉 적용
            windowed = np.clip(slice_2d, window_min, window_max)
            
            # 0-255 범위로 정규화
            normalized = ((windowed - window_min) / (window_max - window_min) * 255).astype(np.uint8)
            
            # PIL Image로 변환
            pil_image = Image.fromarray(normalized)
            
            # PNG로 인코딩
            img_buffer = io.BytesIO()
            pil_image.save(img_buffer, format='PNG')
            img_data = img_buffer.getvalue()
            
            return Response(content=img_data, media_type="image/png")
            
        finally:
            # 임시 파일 삭제
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process DICOM slice: {str(e)}")


@router.get("/viewer/{reconstruction_id}/info")
async def get_dicom_info(
    reconstruction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """DICOM 시리즈 정보 반환 (슬라이스 개수 등)"""
    try:
        reconstruction = db.query(Reconstruction).filter(
            Reconstruction.id == reconstruction_id,
            Reconstruction.user_id == current_user.id
        ).first()
        
        if not reconstruction:
            raise HTTPException(status_code=404, detail="Reconstruction not found")
        
        if not reconstruction.dicom_url:
            raise HTTPException(status_code=404, detail="DICOM files not found")
        
        dicom_files = reconstruction.dicom_url.split(",")
        
        # 첫 번째 DICOM 파일로부터 정보 가져오기
        first_dicom = dicom_files[0]
        file_data = storage_client.get_file(first_dicom)
        
        if not file_data:
            raise HTTPException(status_code=404, detail="DICOM file not found")
        
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(suffix='.dcm', delete=False) as tmp_file:
            tmp_file.write(file_data)
            tmp_path = tmp_file.name
        
        try:
            reader = sitk.ImageFileReader()
            reader.SetFileName(tmp_path)
            
            # 메타데이터 읽기
            reader.ReadImageInformation()
            size = reader.GetSize()
            spacing = reader.GetSpacing() if hasattr(reader, 'GetSpacing') else None
            
            # 환자 정보 읽기 (UTF-8 인코딩 문제 방지)
            patient_name = "Unknown"
            study_date = None
            modality = None
            
            try:
                if reader.HasMetaDataKey("0010|0010"):
                    patient_name_raw = reader.GetMetaData("0010|0010")
                    # DICOM 문자열은 보통 ASCII 또는 특정 인코딩 사용
                    patient_name = patient_name_raw.encode('latin1', errors='ignore').decode('utf-8', errors='ignore') if patient_name_raw else "Unknown"
            except Exception as e:
                print(f"Error reading patient name: {e}")
            
            try:
                if reader.HasMetaDataKey("0008|0020"):
                    study_date = reader.GetMetaData("0008|0020")
            except Exception as e:
                print(f"Error reading study date: {e}")
            
            try:
                if reader.HasMetaDataKey("0008|0060"):
                    modality = reader.GetMetaData("0008|0060")
            except Exception as e:
                print(f"Error reading modality: {e}")
            
            return {
                "total_slices": len(dicom_files),
                "image_size": list(size) if size else None,
                "spacing": list(spacing) if spacing else None,
                "patient_name": patient_name,
                "study_date": study_date,
                "modality": modality,
            }
            
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get DICOM info: {str(e)}")

