FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx
COPY gateway.py /app/gateway.py
ENV PORT=8080
EXPOSE 8080
CMD ["uvicorn","gateway:app","--host","0.0.0.0","--port","8080","--loop","asyncio","--http","h11"]
