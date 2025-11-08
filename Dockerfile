# ---- tiny, fast build for the Render proxy ----
FROM python:3.11-slim

# faster, cleaner Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# security & smaller image
RUN useradd -m appuser

# workdir
WORKDIR /app

# install only what we really need
# (пиную версии, чтобы кэшировалось и не дёргало новые релизы на каждом деплое)
RUN pip install --no-cache-dir \
    "fastapi==0.115.0" \
    "uvicorn[standard]==0.30.6"

# copy your thin app.py (прокси)
COPY app.py /app/app.py

# run as non-root
USER appuser

# Render listens on $PORT; default to 8080
ENV PORT=8080
EXPOSE 8080

# healthcheck (опционально; Render и так проверяет)
# HEALTHCHECK --interval=30s --timeout=3s \
#   CMD python - << 'PY'\nimport urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/").read()\nPY

# start
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
