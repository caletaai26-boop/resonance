# --- frontend build
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- runtime
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fluidsynth fluid-soundfont-gm libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt \
    && pip install --no-cache-dir --no-deps basic-pitch==0.4.0
COPY backend/ backend/
COPY --from=web /web/dist frontend/dist
ENV RESONANCE_DATA_DIR=/data RESONANCE_FRONTEND_DIST=/app/frontend/dist PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
WORKDIR /app/backend
# One process: the job worker runs inside the API process, so keep a single uvicorn worker.
CMD ["uvicorn", "resonance.main:app", "--host", "0.0.0.0", "--port", "8000"]
