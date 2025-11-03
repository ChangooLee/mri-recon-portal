# 🧠 MRI 3D Reconstruction Web Platform (Full Stack Edition)

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Build](https://img.shields.io/github/actions/workflow/status/yourname/mri-3d-web/ci.yml)](https://github.com/yourname/mri-3d-web/actions)

---

## 📋 Overview
이 프로젝트는 의료용 MRI DICOM 데이터를 업로드 → 3D 모델 재구성 → 웹 시각화 → AI 세그멘테이션 → 결과 저장 및 다운로드까지 제공하는 **통합 MRI 3D 리컨스트럭션 플랫폼**입니다.  
Google 로그인을 통해 안전한 사용자 인증을 지원하며, 결과 및 사용자 데이터는 PostgreSQL DB에 저장됩니다.

---

## 🧱 Architecture
```mermaid
graph TD
A[Browser + Google Login] --> B[FastAPI Server (OAuth2, REST)]
B --> |Store| S[MinIO / S3 Object Storage]
B --> |Queue| C[Celery + Redis Broker]
C --> D[Worker (SimpleITK + MONAI + PyTorch)]
D --> |Export STL/GLTF| S
S --> |View| F[OHIF Viewer (React + vtk.js)]
B --> DB[(PostgreSQL)]
````

---

## 📂 Repository Structure

```
mri-3d-web/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── auth.py         # Google OAuth2 로그인
│   │   │   ├── user.py         # 사용자 관리
│   │   │   └── reconstruct.py  # DICOM → 3D 재구성 API
│   │   ├── worker/
│   │   ├── core/                # 설정, DB 연결, OAuth2 설정
│   │   ├── models/            # SQLAlchemy 모델
│   │   └── utils/
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend/
│   ├── ohif-custom/
│   ├── src/
│   │   ├── pages/
│   │   ├── components/
│   │   └── config/
│   ├── package.json
│   └── Dockerfile
│
├── worker/
│   └── Dockerfile
│
├── docker-compose.yml
├── README.md
└── LICENSE
```

---

## 🔐 Authentication – Google OAuth2

* 로그인 버튼 → Google OAuth2 Redirect → 토큰 수신 → JWT 발급
* 세션 정보와 사용자 프로필(Google email, name, picture)은 PostgreSQL DB에 저장
* FastAPI의 `fastapi_users` 또는 `Authlib` OAuth2 모듈 활용

**환경변수 예시**

```
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_secret
GOOGLE_REDIRECT_URI=https://yourdomain.com/api/v1/auth/google/callback
JWT_SECRET_KEY=supersecretjwtkey
```

**엔드포인트**

| Method | Endpoint                       | 설명                   |
| ------ | ------------------------------ | -------------------- |
| GET    | `/api/v1/auth/google/login`    | Google 로그인 URL 리턴    |
| GET    | `/api/v1/auth/google/callback` | OAuth 콜백 처리 + JWT 발급 |
| GET    | `/api/v1/auth/me`              | 로그인 사용자 프로필 조회       |

---

## 🗄️ Database (PostgreSQL)

DB는 SQLAlchemy ORM 으로 관리하며 Alembic 마이그레이션을 지원합니다.

| 테이블             | 컬럼                                     | 설명                |
| --------------- | -------------------------------------- | ----------------- |
| users           | id, email, name, avatar_url            | Google 로그인 사용자 정보 |
| reconstructions | id, user_id, task_id, file_url, status | MRI 3D 재구성 작업 기록  |
| segments        | id, recon_id, label, mask_url          | 세그멘테이션 결과 (선택)    |

---

## 🧩 Core Components

| Component | Role                  | Framework                      |
| --------- | --------------------- | ------------------------------ |
| Backend   | API, OAuth2, DB 연결    | FastAPI + Authlib + SQLAlchemy |
| Worker    | 볼륨/메쉬 재구성 및 AI 세그멘테이션 | Celery + SimpleITK + MONAI     |
| Storage   | DICOM / STL / GLTF 저장 | MinIO / S3                     |
| Frontend  | 로그인 + 뷰어 UI           | React (OHIF Viewer) + vtk.js   |
| Database  | 사용자 및 작업 이력           | PostgreSQL                     |

---

## ⚙️ Environment Variables

| Key                  | Example                                           | Description         |
| -------------------- | ------------------------------------------------- | ------------------- |
| CELERY_BROKER_URL    | `redis://redis:6379/0`                            | 비동기 작업큐 URL         |
| DATABASE_URL         | `postgresql+psycopg2://postgres:pw@db:5432/mri3d` | DB 연결 URL           |
| MINIO_ROOT_USER      | `admin`                                           | MinIO 사용자명          |
| MINIO_ROOT_PASSWORD  | `password`                                        | MinIO 비밀번호          |
| GOOGLE_CLIENT_ID     | —                                                 | Google OAuth ID     |
| GOOGLE_CLIENT_SECRET | —                                                 | Google OAuth Secret |

---

## 🧠 Reconstruction Pipeline

### 개선된 DICOM → 3D 메쉬 파이프라인

1️⃣ **DICOM 업로드 및 시리즈 선택**
   - SeriesInstanceUID별 자동 그룹화
   - 혼합 시리즈 감지 시 가장 큰 시리즈 자동 선택
   - Geometry 일관성 검증 (Rows/Columns/PixelSpacing)

2️⃣ **IPP 기반 정렬 및 Outlier 제거**
   - ImagePositionPatient 기반 정확한 슬라이스 정렬
   - 비정상 간격 슬라이스 자동 제거 (Δt 변동계수 기반)

3️⃣ **고급 전처리**
   - N4 Bias Field Correction (MRI 신호 불균일 보정)
   - 비등방 가우시안 스무딩 (z 방향 강화)
   - Intensity windowing → 3D Otsu 임계값
   - 연결성 필터링 (최대 성분 선택, 중심 기반 스코링)

4️⃣ **스마트 리샘플링**
   - 이방성 비율(r) 기반 자동 전략
   - 2D 두꺼운 슬라이스(≥3mm) 감지 시 원본 해상도 유지
   - 등방성 리샘플링: r ≤ 1.5 → 0.6-0.8mm, r ≤ 3.0 → 1.0-1.2mm

5️⃣ **Marching Cubes 및 좌표 변환**
   - Spacing 이중 적용 버그 수정
   - LPS → Three.js 좌표계 변환
   - 단위 일원화: mm → m (1/1000)

6️⃣ **메쉬 후처리**
   - Laplacian smoothing
   - Quadratic decimation (30-60%)
   - ROI 크롭 (배경/테이블 제거)

7️⃣ **결과 저장 및 시각화**
   - STL/GLB 형식 내보내기
   - Draco 압축 지원
   - Three.js 기반 3D 뷰어 (자동 카메라 맞춤)
   - DICOM 슬라이스 뷰어 (윈도잉 기능)

---

## 🧰 Dependencies

### Backend
* **FastAPI**, **Authlib**, **SQLAlchemy**, **Alembic**
* **Celery**, **Redis**
* **SimpleITK** (N4 bias correction, DICOM 읽기, 리샘플링)
* **scikit-image** (marching cubes, thresholding)
* **scipy** (ndimage: morphological operations, connected components)
* **trimesh** (메쉬 생성, smoothing, decimation)
* **pydicom** (DICOM 메타데이터 파싱)
* **numpy**

### AI/ML
* **MONAI**, **PyTorch** (세그멘테이션)

### Frontend
* **React**, **Vite**
* **Three.js**, **@react-three/fiber**, **@react-three/drei** (3D 메쉬 뷰어)
* **DRACOLoader**, **MeshoptDecoder** (압축 메쉬 로딩)

### 기타
* **@gltf-transform/cli** (Draco 압축, Node.js 기반)
* **MinIO** (Object Storage)
* **PostgreSQL** (데이터베이스)

---

## 🚀 Quick Start

### Prerequisites

- Docker and Docker Compose
- Google OAuth2 credentials (Client ID and Secret)

### Setup

1. **환경변수 설정**

프로젝트 루트에 `.env` 파일을 생성하고 다음 변수들을 설정하세요:

```bash
# .env 파일 생성 (.env.example 참고)
SECRET_KEY=your-secret-key-change-in-production
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
```

2. **Google OAuth2 설정**

- [Google Cloud Console](https://console.cloud.google.com/)에서 OAuth 2.0 Client ID 생성
- 승인된 리디렉션 URI에 `http://localhost:8000/api/v1/auth/google/callback` 추가

3. **Docker Compose로 실행**

```bash
docker compose up --build -d
```

4. **서비스 접속**

- Frontend: http://localhost:5173
- Backend API: http://localhost:8000
- MinIO Console: http://localhost:9001 (admin/password)
- API Docs: http://localhost:8000/docs

### 데이터베이스 마이그레이션

Docker Compose로 실행하면 자동으로 마이그레이션이 실행됩니다. 수동 실행이 필요한 경우:

```bash
docker compose exec backend alembic upgrade head
```

---

## 📈 Implementation Status

| Phase   | Goal                       | Status      |
| ------- | -------------------------- | ------------ |
| Phase 1 | MVP: DICOM → 3D Mesh + 로그인 | ✅ 완료 |
| Phase 1.1 | DICOM Viewer 통합 | ✅ 완료 (슬라이스 뷰어 + 윈도잉 기능) |
| Phase 1.2 | 고품질 재구성 파이프라인 | ✅ 완료 (N4 bias correction, IPP 정렬, 스마트 리샘플링, 좌표 변환 개선) |
| Phase 2 | AI 세그멘테이션(MONAI) 통합        | ✅ 완료 (기본 구현) |
| Phase 3 | PACS 연동 및 결과 검색            | 🔄 향후 계획 |
| Phase 4 | K8s + GPU 스케일링 배포          | 🔄 향후 계획 |

### 주요 개선사항 (v1.2)

- ✅ **SeriesInstanceUID 자동 선택**: 혼합 시리즈 감지 시 가장 큰 시리즈 자동 선택
- ✅ **IPP 기반 정렬**: ImagePositionPatient 기반 정확한 슬라이스 정렬 및 outlier 제거
- ✅ **N4 Bias Correction**: MRI 신호 불균일 보정으로 전처리 품질 향상
- ✅ **스마트 리샘플링**: 이방성 비율 기반 자동 전략 (가짜 구조 방지)
- ✅ **좌표 변환 개선**: Spacing 이중 적용 버그 수정, mm → m 단위 일원화
- ✅ **3D 뷰어 개선**: 자동 카메라 맞춤, 원점 정렬, Draco 압축 지원

## 🔧 개발 및 테스트

### 로컬 개발 환경

```bash
# Backend 개발 서버
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend 개발 서버
cd frontend
npm install
npm run dev

# Celery Worker
cd worker
celery -A backend.app.worker.tasks.celery_app worker --loglevel=info
```

### API 엔드포인트

#### 인증
- `GET /api/v1/auth/google/login` - Google 로그인 시작
- `GET /api/v1/auth/google/callback` - OAuth 콜백
- `GET /api/v1/auth/me` - 현재 사용자 정보

#### 재구성
- `POST /api/v1/reconstruct/upload` - DICOM 파일 업로드
- `GET /api/v1/reconstruct` - 재구성 목록 조회
- `GET /api/v1/reconstruct/{id}` - 재구성 상세 조회
- `GET /api/v1/reconstruct/{id}/download` - 결과 파일 다운로드

#### 세그멘테이션
- `POST /api/v1/segmentation/{reconstruction_id}` - 세그멘테이션 시작
- `GET /api/v1/segmentation/{reconstruction_id}` - 세그멘테이션 목록 조회

### 주의사항

- Google OAuth2 자격증명은 반드시 설정해야 합니다 (현재는 BYPASS_AUTH=True로 개발 모드)
- DICOM 파일은 `.dcm` 또는 `.dicom` 확장자를 지원합니다
- **혼합 시리즈 처리**: 여러 SeriesInstanceUID가 있는 경우 가장 큰 시리즈가 자동 선택됩니다
- **권장 데이터**: 단일 3D 등방성 시퀀스 (SPACE, CUBE, VIBE 등) 또는 얇은 슬라이스(≤2mm) 2D 시리즈
- **메모리 요구사항**: 대용량 볼륨 처리 시 Worker 메모리 8GB 이상 권장
- MONAI 세그멘테이션은 기본 임계값 기반 구현으로 되어 있으며, 실제 프로덕션에서는 사전 학습된 모델을 사용해야 합니다

---

## 📜 License

Apache License 2.0
저작권 (C) 2025 [Your Organization]

---

## 🧑‍💻 References

* [OHIF Viewer](https://github.com/OHIF/Viewers)
* [FastAPI Users / Authlib for Google OAuth2](https://docs.authlib.org/en/latest/client/starlette.html)
* [MONAI Label](https://github.com/Project-MONAI/MONAILabel)
* [SimpleITK](https://simpleitk.org)
* [vtk.js](https://kitware.github.io/vtk-js)

```