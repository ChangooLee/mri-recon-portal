from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from typing import List
import uuid
from datetime import datetime
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.reconstruction import Reconstruction, ReconstructionStatus
from app.utils.storage import storage_client
from app.core.config import settings
from celery import Celery

# Celery 앱 초기화 (worker와 동일한 설정)
celery_app = Celery(
    "mri_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

# 태스크 라우팅 설정 (worker와 동일)
celery_app.conf.task_routes = {
    'app.worker.tasks.process_reconstruction': {'queue': 'reconstruction'},
    'app.worker.tasks.process_segmentation': {'queue': 'segmentation'},
}

router = APIRouter()


@router.post("/reconstruct/upload")
async def upload_dicom(
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """DICOM 파일 업로드 및 재구성 작업 시작"""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    
    # 파일명으로 정렬 (DICOM 시리즈는 보통 파일명 순서가 중요)
    sorted_files = sorted(files, key=lambda f: f.filename or "")
    
    # DICOM 파일들을 MinIO에 업로드
    dicom_files = []
    reconstruction_id = uuid.uuid4()  # 모든 파일을 같은 reconstruction ID로 묶음
    
    for idx, file in enumerate(sorted_files):
        content = await file.read()
        # 같은 reconstruction ID로 묶어서 저장 (디렉토리 구조 개선)
        object_name = f"dicom/{current_user.id}/{reconstruction_id}/{idx:04d}_{file.filename}"
        storage_client.upload_file(object_name, content, file.content_type or "application/dicom")
        dicom_files.append(object_name)
    
    # Reconstruction 레코드 생성
    reconstruction = Reconstruction(
        id=reconstruction_id,
        user_id=current_user.id,
        dicom_url=",".join(dicom_files),  # 여러 파일을 쉼표로 구분
        status="pending"  # enum 값을 문자열로 직접 사용
    )
    db.add(reconstruction)
    db.commit()
    db.refresh(reconstruction)
    
    # Celery 태스크 발행 (worker에서 처리)
    task = celery_app.send_task(
        'app.worker.tasks.process_reconstruction',
        args=[str(reconstruction.id)],
        queue='reconstruction'  # 큐 명시
    )
    reconstruction.task_id = task.id
    db.commit()
    
    return {
        "reconstruction_id": str(reconstruction.id),
        "task_id": task.id,
        "status": reconstruction.status,
        "message": "Reconstruction task started"
    }


@router.get("/reconstruct")
async def list_reconstructions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """사용자의 재구성 작업 목록 조회"""
    reconstructions = db.query(Reconstruction).filter(
        Reconstruction.user_id == current_user.id
    ).order_by(Reconstruction.created_at.desc()).all()
    
    return [
        {
            "id": str(r.id),
            "task_id": r.task_id,
            "status": r.status if isinstance(r.status, str) else (r.status.value if hasattr(r.status, 'value') else str(r.status)),
            "stl_url": r.stl_url,
            "gltf_url": r.gltf_url,
            "dicom_url": r.dicom_url,  # DICOM 파일 정보 추가
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
            "error_message": r.error_message
        }
        for r in reconstructions
    ]


@router.get("/reconstruct/{reconstruction_id}")
async def get_reconstruction(
    reconstruction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """재구성 작업 상태 조회"""
    reconstruction = db.query(Reconstruction).filter(
        Reconstruction.id == reconstruction_id,
        Reconstruction.user_id == current_user.id
    ).first()
    
    if not reconstruction:
        raise HTTPException(status_code=404, detail="Reconstruction not found")
    
    # MinIO에서 presigned URL 생성
    stl_url = None
    gltf_url = None
    dicom_url = None
    
    if reconstruction.stl_url:
        stl_url = storage_client.get_presigned_url(reconstruction.stl_url)
    if reconstruction.gltf_url:
        gltf_url = storage_client.get_presigned_url(reconstruction.gltf_url)
    if reconstruction.dicom_url:
        # 첫 번째 DICOM 파일의 URL 반환 (OHIF Viewer는 디렉토리 기반 접근 필요)
        dicom_files = reconstruction.dicom_url.split(",")
        if dicom_files:
            dicom_url = storage_client.get_presigned_url(dicom_files[0])
    
    return {
        "id": str(reconstruction.id),
        "task_id": reconstruction.task_id,
        "status": reconstruction.status,
        "stl_url": stl_url,
        "gltf_url": gltf_url,
        "dicom_url": dicom_url,
        "created_at": reconstruction.created_at.isoformat(),
        "updated_at": reconstruction.updated_at.isoformat(),
        "error_message": reconstruction.error_message
    }


@router.get("/reconstruct/{reconstruction_id}/download")
async def download_reconstruction(
    reconstruction_id: str,
    format: str = "stl",  # stl or gltf
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """재구성 결과 파일 다운로드"""
    reconstruction = db.query(Reconstruction).filter(
        Reconstruction.id == reconstruction_id,
        Reconstruction.user_id == current_user.id
    ).first()
    
    if not reconstruction:
        raise HTTPException(status_code=404, detail="Reconstruction not found")
    
    if reconstruction.status != "completed":
        raise HTTPException(status_code=400, detail="Reconstruction not completed")
    
    object_name = None
    if format == "stl" and reconstruction.stl_url:
        object_name = reconstruction.stl_url
    elif format == "gltf" and reconstruction.gltf_url:
        object_name = reconstruction.gltf_url
    else:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Presigned URL 반환
    url = storage_client.get_presigned_url(object_name, expires_seconds=3600)
    return {"download_url": url}

