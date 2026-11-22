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
# CPU-only PyTorch (no multi-GB CUDA libraries). Demucs and Basic Pitch are installed without their declared
# dependencies: Demucs lists 'sphn' (needs a Rust/C toolchain on arm64) and Basic Pitch lists TensorFlow;
# RESONANCE uses neither, only the packages installed explicitly below.
RUN pip install --no-cache-dir torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
    && grep -v -i -E '^(torch|demucs)==' backend/requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir einops julius pyyaml tqdm \
    && pip install --no-cache-dir --no-deps demucs==4.1.0 basic-pitch==0.4.0 \
    && python -c "import demucs.pretrained, demucs.apply, basic_pitch, librosa, onnxruntime; print('audio stack imports OK')"
COPY backend/ backend/
COPY --from=web /web/dist frontend/dist
ENV RESONANCE_DATA_DIR=/data RESONANCE_FRONTEND_DIST=/app/frontend/dist PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000
WORKDIR /app/backend
# One process: the job worker runs inside the API process, so keep a single uvicorn worker.
CMD ["uvicorn", "resonance.main:app", "--host", "0.0.0.0", "--port", "8000"]
