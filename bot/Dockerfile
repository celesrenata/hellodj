FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (FFmpeg + libsndfile for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py player.py session.py storage.py blacklist.py ./
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
