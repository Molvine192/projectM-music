# FastAPI + yt-dlp + ffmpeg
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# App deps
RUN pip install --no-cache-dir fastapi uvicorn[standard] yt-dlp

# App
WORKDIR /app
COPY app.py /app/app.py

# Media/cache dir
VOLUME ["/data"]
ENV MEDIA_ROOT=/data
ENV PORT=8080

EXPOSE 8080
CMD ["python", "app.py"]
