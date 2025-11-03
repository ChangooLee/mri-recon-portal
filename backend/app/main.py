from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api import auth, user, reconstruct, segmentation, storage, viewer

app = FastAPI(title=settings.PROJECT_NAME)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 라우터 등록
app.include_router(auth.router, prefix=settings.API_V1_PREFIX, tags=["auth"])
app.include_router(user.router, prefix=settings.API_V1_PREFIX, tags=["users"])
app.include_router(reconstruct.router, prefix=settings.API_V1_PREFIX, tags=["reconstruction"])
app.include_router(segmentation.router, prefix=settings.API_V1_PREFIX, tags=["segmentation"])
app.include_router(storage.router, prefix=settings.API_V1_PREFIX, tags=["storage"])
app.include_router(viewer.router, prefix=settings.API_V1_PREFIX, tags=["viewer"])


@app.get("/")
async def root():
    return {"message": "MRI 3D Reconstruction Platform API"}


@app.get("/health")
async def health():
    return {"status": "healthy"}

