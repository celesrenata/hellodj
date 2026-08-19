FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# data/ is mounted as a shared volume (same NFS as bot + web-ui)
VOLUME /app/data

EXPOSE 8802

CMD ["python", "app.py"]
