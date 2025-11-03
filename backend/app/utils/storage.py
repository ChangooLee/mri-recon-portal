from minio import Minio
from minio.error import S3Error
from app.core.config import settings
import io
from typing import Optional


class StorageClient:
    def __init__(self):
        self.client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE
        )
        self._ensure_bucket()
        self._setup_cors()
    
    def _ensure_bucket(self):
        try:
            if not self.client.bucket_exists(settings.MINIO_BUCKET_NAME):
                self.client.make_bucket(settings.MINIO_BUCKET_NAME)
        except S3Error as e:
            print(f"Error ensuring bucket: {e}")
    
    def _setup_cors(self):
        """Setup CORS for MinIO bucket to allow browser access"""
        # MinIO Python 클라이언트의 CORS 설정은 버전에 따라 다를 수 있음
        # 실제 환경에서는 MinIO 콘솔 또는 mc 명령어로 CORS 설정 권장
        pass
    
    def upload_file(self, object_name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Upload file to MinIO and return object name"""
        try:
            data_stream = io.BytesIO(data)
            self.client.put_object(
                settings.MINIO_BUCKET_NAME,
                object_name,
                data_stream,
                length=len(data),
                content_type=content_type
            )
            return object_name
        except S3Error as e:
            raise Exception(f"Failed to upload file: {e}")
    
    def get_file(self, object_name: str) -> Optional[bytes]:
        """Download file from MinIO"""
        try:
            response = self.client.get_object(settings.MINIO_BUCKET_NAME, object_name)
            return response.read()
        except S3Error as e:
            print(f"Error getting file: {e}")
            return None
    
    def get_presigned_url(self, object_name: str, expires_seconds: int = 3600) -> str:
        """Generate URL for file access (uses backend proxy to avoid CORS issues)"""
        try:
            from app.core.config import settings
            # 백엔드 프록시 엔드포인트 사용 (CORS 문제 해결)
            return f"{settings.BACKEND_URL}/api/v1/storage/{object_name}"
        except Exception as e:
            raise Exception(f"Failed to generate file URL: {e}")
    
    def delete_file(self, object_name: str) -> bool:
        """Delete file from MinIO"""
        try:
            self.client.remove_object(settings.MINIO_BUCKET_NAME, object_name)
            return True
        except S3Error as e:
            print(f"Error deleting file: {e}")
            return False


storage_client = StorageClient()

