from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.utils.storage import storage_client

router = APIRouter()


@router.get("/storage/{object_path:path}")
async def get_storage_file(
    object_path: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """MinIO에서 파일을 가져와서 프록시로 제공 (CORS 문제 해결)"""
    try:
        file_data = storage_client.get_file(object_path)
        if not file_data:
            raise HTTPException(status_code=404, detail="File not found")
        
        # 파일 확장자로 Content-Type 결정
        content_type = "application/octet-stream"
        if object_path.endswith('.glb') or object_path.endswith('.gltf'):
            content_type = "model/gltf-binary" if object_path.endswith('.glb') else "model/gltf+json"
        elif object_path.endswith('.stl'):
            content_type = "application/octet-stream"
        elif object_path.endswith('.dcm') or object_path.endswith('.dicom'):
            content_type = "application/dicom"
        elif object_path.endswith('.nii.gz'):
            content_type = "application/gzip"
        
        from io import BytesIO
        return StreamingResponse(
            BytesIO(file_data),
            media_type=content_type,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET",
                "Access-Control-Allow-Headers": "*",
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve file: {str(e)}")

