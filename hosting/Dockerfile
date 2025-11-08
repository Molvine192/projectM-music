# ---- worker: yt-dlp + ffmpeg ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# фиксируем версии для кеша и стабильности
RUN pip install --no-cache-dir \
    "fastapi==0.115.0" \
    "uvicorn[standard]==0.30.6" \
    "yt-dlp==2024.11.04"  # можно обновлять вручную по необходимости

COPY app.py /app/app.py

# место для кэша/MP3
VOLUME ["/data"]
ENV MEDIA_ROOT=/data \
    PORT=8080

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
