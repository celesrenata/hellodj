FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# data/ is mounted as a shared volume (same NFS as bot + web-ui)
VOLUME /app/data

EXPOSE 8801

CMD ["python", "app.py"]
