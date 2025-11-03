from celery import Celery
import sys
import os
# PYTHONPATH에 /app/backend가 있으므로 backend.app로 접근
sys.path.insert(0, '/app/backend')
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.reconstruction import Reconstruction, ReconstructionStatus
from app.models.segment import Segment
from app.worker.reconstruction import process_dicom_to_mesh
from app.worker.segmentation import process_ai_segmentation

celery_app = Celery(
    "mri_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

celery_app.conf.task_routes = {
    'app.worker.tasks.process_reconstruction': {'queue': 'reconstruction'},
    'app.worker.tasks.process_segmentation': {'queue': 'segmentation'},
}


@celery_app.task(name="app.worker.tasks.process_reconstruction")
def process_reconstruction(reconstruction_id: str):
    """DICOM 파일을 3D 메쉬로 변환하는 태스크"""
    db = SessionLocal()
    try:
        reconstruction = db.query(Reconstruction).filter(
            Reconstruction.id == reconstruction_id
        ).first()
        
        if not reconstruction:
            return {"status": "error", "message": "Reconstruction not found"}
        
        reconstruction.status = "processing"
        db.commit()
        
        # DICOM 처리 및 메쉬 생성
        result = process_dicom_to_mesh(reconstruction, db)
        
        if result["status"] == "success":
            reconstruction.status = "completed"
            reconstruction.stl_url = result.get("stl_url")
            reconstruction.gltf_url = result.get("gltf_url")
            reconstruction.error_message = None
        else:
            reconstruction.status = "failed"
            reconstruction.error_message = result.get("error", "Unknown error")
        
        db.commit()
        return result
        
    except Exception as e:
        if reconstruction:
            reconstruction.status = "failed"
            reconstruction.error_message = str(e)
            db.commit()
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


@celery_app.task(name="app.worker.tasks.process_segmentation")
def process_segmentation(reconstruction_id: str, segment_id: str, label: str):
    """AI 세그멘테이션 태스크"""
    db = SessionLocal()
    try:
        reconstruction = db.query(Reconstruction).filter(
            Reconstruction.id == reconstruction_id
        ).first()
        
        segment = db.query(Segment).filter(
            Segment.id == segment_id
        ).first()
        
        if not reconstruction or not segment:
            return {"status": "error", "message": "Reconstruction or segment not found"}
        
        # AI 세그멘테이션 처리
        result = process_ai_segmentation(reconstruction, segment, label, db)
        
        if result["status"] == "success":
            segment.mask_url = result.get("mask_url")
            segment.mesh_url = result.get("mesh_url")
            db.commit()
        
        return result
        
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        db.close()

