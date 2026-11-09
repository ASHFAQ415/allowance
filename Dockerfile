# Multi-stage production Dockerfile for Allowance v5.0
# Stage 1: Build React 18 + Tailwind Frontend
FROM node:18-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# Stage 2: Production Python Runtime
FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=. \
    PORT=8000 \
    DB_PATH=/data/allowance.db \
    ALLOW_DEMO_RESET=true

# Create data directory for persistent SQLite database
RUN mkdir -p /data

# Copy backend application and server scripts
COPY backend/ backend/
COPY scripts/ scripts/

# Copy pre-built frontend distribution from stage 1
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

EXPOSE 8000

# Start zero-dependency full-stack server
CMD ["python", "scripts/dev_server.py"]
