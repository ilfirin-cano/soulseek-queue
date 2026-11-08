FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/data/tracks.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates
COPY static ./static
RUN mkdir -p /data
EXPOSE 8090
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:8090", "--access-logfile", "-", "app:app"]

