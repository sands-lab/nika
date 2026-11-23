# Build: docker build -t nika-dataset-explorer scripts/offline_recording/explorer
# Run:   docker run --rm -p 8050:8050 -v <dataset>:/data:ro nika-dataset-explorer
FROM node:22-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.30"
COPY backend/ ./backend/
COPY --from=frontend /app/dist ./frontend/dist
ENV EXPLORER_DATA=/data EXPLORER_STATIC=/app/frontend/dist
EXPOSE 8050
USER nobody
CMD ["uvicorn", "--app-dir", "backend", "explorer_backend.app:app", "--host", "0.0.0.0", "--port", "8050"]
