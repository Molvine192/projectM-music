from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import os, json, time, io
from urllib.parse import urlencode, urljoin
import urllib.request, urllib.error

# --------------------------- Config ---------------------------
PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_CONVERTER = (os.environ.get("UPSTREAM_CONVERTER", "") or "").strip().rstrip("/")
UPSTREAM_TOKEN = (os.environ.get("UPSTREAM_TOKEN", "") or "").strip()
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))

app = FastAPI(title="MTA Music Gateway (Render proxy)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

def upstream_url(path: str, qs: dict = None) -> str:
    if not UPSTREAM_CONVERTER:
        return ""
    base = UPSTREAM_CONVERTER + "/"
    full = urljoin(base, path.lstrip("/"))
    if qs:
        sep = "&" if ("?" in full) else "?"
        full = f"{full}{sep}{urlencode(qs, doseq=True)}"
    return full

def fetch_json(method: str, url: str, body: dict | None) -> dict:
    data_bytes = None
    headers = {"User-Agent": "MTA-Render-Gateway/1.0", "Accept": "application/json"}
    if UPSTREAM_TOKEN:
        headers["X-Shared-Token"] = UPSTREAM_TOKEN
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "ignore")
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": "upstream_not_json", "raw": raw}

# --------------------------- Endpoints ---------------------------
@app.get("/")
def root():
    return {
        "service": "MTA Music Gateway (Render proxy)",
        "has_upstream": bool(UPSTREAM_CONVERTER),
        "upstream": UPSTREAM_CONVERTER or None,
        "endpoints": ["/search?q=", "/convert", "/media/<file>", "/ping", "/status"],
        "ts": int(time.time()),
    }

@app.get("/ping")
def ping(source: str = "mta"):
    return {"ok": True, "source": source, "ts": int(time.time())}

@app.get("/status")
def status():
    return {"ok": True, "upstream": UPSTREAM_CONVERTER or None}

@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = 30):
    if not UPSTREAM_CONVERTER:
        return JSONResponse(status_code=503, content={"ok": False, "error": "no_upstream_configured"})
    url = upstream_url("/search", {"q": q, "limit": max(1, min(limit, 50))})
    try:
        return fetch_json("GET", url, None)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": "upstream_failed", "msg": str(e)})

@app.post("/convert")
async def convert(request: Request):
    if not UPSTREAM_CONVERTER:
        return JSONResponse(status_code=503, content={"ok": False, "error": "no_upstream_configured"})
    qp = dict(request.query_params)
    ctype = (request.headers.get("content-type") or "").lower()
    body_to_send = None

    # пробуем JSON
    try:
        incoming = await request.json()
        body_to_send = incoming if isinstance(incoming, dict) else {"data": incoming}
    except Exception:
        raw = await request.body()
        if raw:
            try:
                if "application/x-www-form-urlencoded" in ctype and b"&" in raw:
                    from urllib.parse import parse_qs
                    form = parse_qs(raw.decode("utf-8", "ignore"))
                    body_to_send = {k: v[0] if isinstance(v, list) else v for k, v in form.items()}
                else:
                    body_to_send = {"raw": raw.decode("utf-8", "ignore")}
            except Exception:
                body_to_send = {"raw": ""}

    url = upstream_url("/convert", qp)
    try:
        upstream_resp = fetch_json("POST", url, body_to_send)
        # если успех и mp3 относительный/абсолютный — перепишем на наш /media/<file>
        if isinstance(upstream_resp, dict) and upstream_resp.get("ok") and upstream_resp.get("mp3"):
            # ожидаем, что upstream mp3 — это /media/<video_id>.mp3 или полный URL на туннель
            # извлечём имя файла
            mp3_url = upstream_resp["mp3"]
            filename = mp3_url.split("/")[-1] if "/" in mp3_url else mp3_url
            upstream_resp["mp3"] = f"/media/{filename}"  # клиент может подставить домен Render сам
        return upstream_resp
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": "upstream_failed", "msg": str(e)})

# ---- /media proxy: тянем байты с туннеля и отдаём клиенту ----
@app.get("/media/{filename}")
def media(filename: str, request: Request):
    if not UPSTREAM_CONVERTER:
        raise HTTPException(status_code=503, detail="no_upstream_configured")
    # простая валидация имени
    if not filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    # формируем URL апстрима
    upstream_media = upstream_url(f"/media/{filename}")
    headers = {"User-Agent": "MTA-Render-Gateway/1.0"}
    if UPSTREAM_TOKEN:
        headers["X-Shared-Token"] = UPSTREAM_TOKEN

    # поддержка простого Range
    range_hdr = request.headers.get("Range")
    if range_hdr:
        headers["Range"] = range_hdr

    req = urllib.request.Request(upstream_media, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(status_code=404, detail="not found")
        raise HTTPException(status_code=502, detail=f"upstream_http_error_{e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream_error: {e}")

    # Обернём поток в StreamingResponse
    status = 206 if resp.getcode() == 206 else 200
    length = resp.headers.get("Content-Length")
    content_range = resp.headers.get("Content-Range")

    def gen():
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

    headers_out = {"Content-Type": "audio/mpeg"}
    if length:
        headers_out["Content-Length"] = length
    if content_range:
        headers_out["Content-Range"] = content_range
    # кэшировать смысла нет: это прокси кртковременных файлов
    return StreamingResponse(gen(), status_code=status, headers=headers_out)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
