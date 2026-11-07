# Stage 1: Build Frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Backend Runtime
FROM python:3.11-slim
WORKDIR /app

# Install tzdata (slim lacks timezone data) and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=UTC
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY .agents/ ./.agents/
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Mandatory single-process worker deployment
EXPOSE 8000
CMD ["uvicorn", "backend.app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

