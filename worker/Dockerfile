FROM python:3.11-slim

WORKDIR /app

# 시스템 의존성 설치
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    build-essential \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Node.js 및 @gltf-transform/cli 설치 (Draco 압축용)
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @gltf-transform/cli \
    && rm -rf /var/lib/apt/lists/*

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Backend 코드는 docker-compose에서 volume으로 마운트됨
# 실제 배포 시에는 별도로 복사 필요

# Worker 코드 복사
COPY . .

# Python path 설정
ENV PYTHONPATH=/app:/app/backend

# Celery 워커 실행 (backend 모듈 사용)
CMD ["celery", "-A", "backend.app.worker.tasks", "worker", "--loglevel=info", "--queues=reconstruction,segmentation"]

