FROM node:18-alpine AS builder

WORKDIR /app

# 의존성 설치
COPY package.json package-lock.json* ./
RUN npm install --legacy-peer-deps

# 소스 코드 복사 및 빌드
COPY . .
RUN npm run build

# Production stage
FROM nginx:alpine

# 빌드된 파일을 nginx로 복사
COPY --from=builder /app/dist /usr/share/nginx/html

# nginx 설정 파일 복사 (선택사항)
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

