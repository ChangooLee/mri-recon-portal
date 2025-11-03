from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from app.core.config import settings
from app.core.database import get_db, SessionLocal
from app.core.security import create_access_token, verify_token
from app.models.user import User
from starlette.responses import RedirectResponse
import uuid

router = APIRouter()

# 인증 바이패스 모드에서는 OAuth2 비활성화
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/google/callback",
    auto_error=False  # 토큰 없어도 에러 발생 안 함
)

# OAuth 설정
config = Config()
oauth = OAuth(config)
oauth.register(
    name='google',
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile'
    }
)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    # 인증 바이패스 모드: 항상 테스트 사용자 반환
    if settings.BYPASS_AUTH:
        test_user = db.query(User).filter(User.email == "test@mri-recon.local").first()
        if not test_user:
            test_user = User(
                id=uuid.uuid4(),
                email="test@mri-recon.local",
                name="Test User",
                avatar_url=None
            )
            db.add(test_user)
            db.commit()
            db.refresh(test_user)
        return test_user
    
    # 정상 인증 흐름 (바이패스 모드가 아닐 때만)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    user_id = payload.get("sub")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.get("/auth/google/login")
async def google_login(request: Request):
    # 인증 바이패스 모드
    if settings.BYPASS_AUTH:
        # 테스트 토큰 생성 및 반환
        from app.core.security import create_access_token
        from app.core.database import SessionLocal
        import uuid
        
        db = SessionLocal()
        try:
            test_user = db.query(User).filter(User.email == "test@mri-recon.local").first()
            if not test_user:
                test_user = User(
                    id=uuid.uuid4(),
                    email="test@mri-recon.local",
                    name="Test User",
                    avatar_url=None
                )
                db.add(test_user)
                db.commit()
                db.refresh(test_user)
            
            token = create_access_token(data={"sub": str(test_user.id), "email": test_user.email})
            frontend_url = f"{settings.FRONTEND_URL}/auth/callback?token={token}"
            return RedirectResponse(url=frontend_url)
        finally:
            db.close()
    
    # 정상 OAuth 흐름
    redirect_uri = settings.GOOGLE_REDIRECT_URI
    try:
        redirect_response = await oauth.google.authorize_redirect(request, redirect_uri)
        return redirect_response
    except Exception as e:
        # Google OAuth URL 직접 생성 (fallback)
        redirect_uri = settings.GOOGLE_REDIRECT_URI
        authorization_url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id={settings.GOOGLE_CLIENT_ID}&redirect_uri={redirect_uri}&response_type=code&scope=openid+email+profile&access_type=online"
        return RedirectResponse(url=authorization_url)


@router.get("/auth/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        
        if not user_info:
            raise HTTPException(status_code=400, detail="Failed to get user info")
        
        email = user_info.get("email")
        name = user_info.get("name")
        avatar_url = user_info.get("picture")
        
        # 사용자 조회 또는 생성
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                id=uuid.uuid4(),
                email=email,
                name=name,
                avatar_url=avatar_url
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # 사용자 정보 업데이트
            user.name = name
            user.avatar_url = avatar_url
            db.commit()
            db.refresh(user)
        
        # JWT 토큰 생성
        access_token = create_access_token(data={"sub": str(user.id), "email": user.email})
        
        # 프론트엔드로 리다이렉트 (토큰 포함)
        frontend_url = f"{settings.FRONTEND_URL}/auth/callback?token={access_token}"
        return RedirectResponse(url=frontend_url)
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authentication failed: {str(e)}")


@router.get("/auth/me")
async def get_current_user_info(
    request: Request,
    db: Session = Depends(get_db)
):
    # 인증 바이패스 모드
    if settings.BYPASS_AUTH:
        test_user = db.query(User).filter(User.email == "test@mri-recon.local").first()
        if not test_user:
            test_user = User(
                id=uuid.uuid4(),
                email="test@mri-recon.local",
                name="Test User",
                avatar_url=None
            )
            db.add(test_user)
            db.commit()
            db.refresh(test_user)
        return {
            "id": str(test_user.id),
            "email": test_user.email,
            "name": test_user.name,
            "avatar_url": test_user.avatar_url
        }
    
    # 정상 인증 흐름
    from fastapi import Header
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    token = authorization.replace("Bearer ", "")
    current_user = get_current_user(token, db)
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "name": current_user.name,
        "avatar_url": current_user.avatar_url
    }

