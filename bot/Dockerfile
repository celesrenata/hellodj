FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (FFmpeg + libsndfile for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 1: core/light deps (small layer, fast to push) ──────────────
# Split requirements so the heavy AI stack does not sit in a single
# multi-GB layer that times out the registry ingress (504) on push.
COPY requirements-core.txt .
RUN pip install --no-cache-dir -r requirements-core.txt

# ── Stage 2: PyTorch + CUDA (own layer) ───────────────────────────────
# Installed separately so the CUDA/torch wheels are isolated from the rest
# of the AI stack; each layer stays small enough to push reliably.
COPY requirements-torch.txt .
RUN pip install --no-cache-dir -r requirements-torch.txt

# ── Stage 3: AI stack (kokoro, faster-whisper, librosa) ───────────────
# These reuse the torch already installed above, so the heavy wheels land
# in stage 2 and this layer only adds the smaller AI packages.
COPY requirements-ai.txt .
RUN pip install --no-cache-dir -r requirements-ai.txt

# Copy source
COPY bot.py player.py oauth_store.py session.py storage.py blacklist.py ./
COPY cogs/ ./cogs/
COPY voice/ ./voice/

# Copy wake word model (optional — the artifact is a training output and may be
# absent from the build context; it can be mounted at runtime instead).
# If Hello_DJ.onnx is present in the build context, bake it into the image.
# Otherwise the image still builds and the model must be supplied via a volume
# mount at /app/models (see docker-compose.yml / kube/deployment.yaml).
RUN mkdir -p /app/models && if [ -f Hello_DJ.onnx ]; then cp Hello_DJ.onnx /app/models/Hello_DJ.onnx; fi

# Create data directory for persistent storage
RUN mkdir -p /app/data

# Run HelloDJ
CMD ["python", "bot.py"]
