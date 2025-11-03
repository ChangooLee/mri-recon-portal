from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import uuid
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.reconstruction import Reconstruction, ReconstructionStatus
from app.models.segment import Segment
from app.core.config import settings
from celery import Celery

# Celery 앱 초기화
celery_app = Celery(
    "mri_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

router = APIRouter()


@router.post("/segmentation/{reconstruction_id}")
async def start_segmentation(
    reconstruction_id: str,
    label: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """재구성된 볼륨에 대한 AI 세그멘테이션 작업 시작"""
    reconstruction = db.query(Reconstruction).filter(
        Reconstruction.id == reconstruction_id,
        Reconstruction.user_id == current_user.id
    ).first()
    
    if not reconstruction:
        raise HTTPException(status_code=404, detail="Reconstruction not found")
    
    if reconstruction.status != "completed":
        raise HTTPException(status_code=400, detail="Reconstruction must be completed first")
    
    # Segment 레코드 생성
    segment = Segment(
        id=uuid.uuid4(),
        recon_id=reconstruction.id,
        label=label
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    
    # Celery 태스크 발행 (worker에서 처리)
    task = celery_app.send_task(
        'app.worker.tasks.process_segmentation',
        args=[str(reconstruction.id), str(segment.id), label],
        queue='segmentation'  # 큐 명시
    )
    
    return {
        "segment_id": str(segment.id),
        "task_id": task.id,
        "label": label,
        "message": "Segmentation task started"
    }


@router.get("/segmentation/{reconstruction_id}")
async def list_segments(
    reconstruction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """재구성 작업의 세그멘테이션 목록 조회"""
    reconstruction = db.query(Reconstruction).filter(
        Reconstruction.id == reconstruction_id,
        Reconstruction.user_id == current_user.id
    ).first()
    
    if not reconstruction:
        raise HTTPException(status_code=404, detail="Reconstruction not found")
    
    segments = db.query(Segment).filter(
        Segment.recon_id == reconstruction.id
    ).all()
    
    from app.utils.storage import storage_client
    
    return [
        {
            "id": str(s.id),
            "label": s.label,
            "mask_url": storage_client.get_presigned_url(s.mask_url) if s.mask_url else None,
            "mesh_url": storage_client.get_presigned_url(s.mesh_url) if s.mesh_url else None,
            "created_at": s.created_at.isoformat()
        }
        for s in segments
    ]

