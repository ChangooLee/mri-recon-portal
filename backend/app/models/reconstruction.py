from sqlalchemy import Column, String, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime
import enum
from app.core.database import Base


class ReconstructionStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    
    def __str__(self):
        return self.value


class Reconstruction(Base):
    __tablename__ = "reconstructions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    task_id = Column(String, nullable=True, index=True)  # Celery task ID
    dicom_url = Column(String, nullable=True)  # MinIO URL for DICOM files
    stl_url = Column(String, nullable=True)  # MinIO URL for STL mesh
    gltf_url = Column(String, nullable=True)  # MinIO URL for GLTF mesh
    status = Column(String(20), default="pending")
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

