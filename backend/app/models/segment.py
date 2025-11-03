from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime
from app.core.database import Base


class Segment(Base):
    __tablename__ = "segments"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recon_id = Column(UUID(as_uuid=True), ForeignKey("reconstructions.id"), nullable=False, index=True)
    label = Column(String, nullable=False)  # Segment label/name
    mask_url = Column(String, nullable=True)  # MinIO URL for segmentation mask
    mesh_url = Column(String, nullable=True)  # MinIO URL for segmented mesh
    created_at = Column(DateTime, default=datetime.utcnow)

