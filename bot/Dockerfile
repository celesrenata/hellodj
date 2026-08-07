FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (FFmpeg for Lavalink audio)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py player.py session.py storage.py blacklist.py ./
COPY cogs/ ./cogs/

# Run the bot
CMD ["python", "bot.py"]
