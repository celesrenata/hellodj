FROM python:3.11-slim

WORKDIR /app

# ── System dependencies: FFmpeg 9 (compiled with QSV/VPL) + Intel VA-API driver ───
# Debian trixie's stock ffmpeg is 7.1.x which has an HLS muxer bug causing
# audio starvation in the first segment when combined with QSV hardware encode.
# We build ffmpeg 9 from source with libvpl (QSV) support to get the April 2026
# HLS interleave fix (commit 45cb56c520).
RUN apt-get update && apt-get install -y --no-install-recommends \
    intel-media-va-driver \
    libmfx-gen1.2 \
    libvpl-dev \
    libva-dev \
    vainfo \
    libsndfile1 \
    # Build deps for ffmpeg
    build-essential \
    nasm \
    pkg-config \
    libx264-dev \
    libx265-dev \
    libopus-dev \
    libdav1d-dev \
    libssl-dev \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build ffmpeg 9 from release tarball with QSV + common codecs + TLS (HTTPS)
RUN wget -q https://ffmpeg.org/releases/ffmpeg-9.0.tar.xz \
    && tar xf ffmpeg-9.0.tar.xz \
    && cd ffmpeg-9.0 \
    && ./configure \
        --prefix=/usr \
        --enable-gpl \
        --enable-libvpl \
        --enable-vaapi \
        --enable-libx264 \
        --enable-libx265 \
        --enable-libopus \
        --enable-libdav1d \
        --enable-openssl \
        --enable-nonfree \
        --disable-doc \
        --disable-debug \
    && make -j$(nproc) \
    && make install \
    && cd / \
    && rm -rf ffmpeg-9.0 ffmpeg-9.0.tar.xz \
    # Remove build tools only (keep runtime libs and -dev since they're small)
    && apt-get purge -y build-essential nasm wget \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Build-time verification: confirm QSV is listed as a hardware accelerator.
RUN ffmpeg -version | head -1 && ffmpeg -hwaccels 2>/dev/null | grep -q qsv \
    && echo "✓ QSV hardware acceleration available in ffmpeg 9"

# Install yt-dlp for YouTube video downloading (video streaming feature).
RUN pip install --no-cache-dir yt-dlp

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
COPY bot.py metrics.py player.py oauth_store.py session.py storage.py blacklist.py allowlist.py guild_settings.py guild_policy.py permissions.py voice_debug.py sounds.py whosampled.py tidal.py file_handler.py sleep_settings.py artist_info.py stems.py debug.py stream_resolver.py credentials.py config.py migrate_to_db.py render_lavalink_config.py ./
COPY cogs/ ./cogs/
COPY voice/ ./voice/
COPY video/ ./video/
COPY playback/ ./playback/
COPY views/ ./views/

# Copy sticker zip archives for whiteboard sticker catalog
COPY stickers/ ./stickers/

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
