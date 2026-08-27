FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    which gunicorn && \
    gunicorn --version

COPY app.py .
COPY auth_middleware.py .
COPY credentials.py .
COPY blueprints/ ./blueprints/
COPY services/ ./services/
COPY templates/ ./templates/
COPY static/ ./static/

# Create data/config/backup directories
RUN mkdir -p /app/data /app/config /app/config-backups

EXPOSE 8080

# Use python -m to avoid path issues
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "app:app"]
