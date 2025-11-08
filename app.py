from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, json, time, re
from urllib.parse import urlencode, urljoin
import urllib.request

# --------------------------------------------------------------------------------------
# Конфиг
# --------------------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8080"))

# КУДА проксируем (твой локальный туннель Cloudflare, например https://xxxx.trycloudflare.com)
UPSTREAM_CONVERTER = os.environ.get("UPSTREAM_CONVERTER", "").strip().rstrip("/")
# (опционально) общий секрет, если ты его проверяешь на воркере
UPSTREAM_TOKEN = os.environ.get("UPSTREAM_TOKEN", "").strip()

# Поведение:
#   ALWAYS  – всегда проксировать /convert и /search на апстрим (рекомендуется для Render)
#   FALLBACK – сначала попытаться локально (здесь локальной логики нет, так что равно ALWAYS)
PROXY_MODE = os.environ.get("PROXY_MODE", "ALWAYS").upper()

# Таймауты запросов к апстриму (сек)
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))

# --------------------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------------------
app = FastAPI(title="MTA Music Gateway (Render proxy)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# --------------------------------------------------------------------------------------
# Утилиты проксирования
# --------------------------------------------------------------------------------------
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
            # не JSON — вернём как текст
            return {"ok": False, "error": "upstream_not_json", "raw": raw}

def absolutize_mp3_link(mp3: str) -> str:
    if not mp3:
        return mp3
    # если путь относительный — превратим в абсолютный к апстриму
    if mp3.startswith("/"):
        return urljoin(UPSTREAM_CONVERTER + "/", mp3.lstrip("/"))
    # если уже абсолютный — оставим как есть
    return mp3

# --------------------------------------------------------------------------------------
# Эндпойнты
# --------------------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "MTA Music Gateway (Render proxy)",
        "proxy_mode": PROXY_MODE,
        "has_upstream": bool(UPSTREAM_CONVERTER),
        "upstream": UPSTREAM_CONVERTER or None,
        "endpoints": ["/search?q=", "/convert", "/ping", "/status"],
        "ts": int(time.time()),
    }

@app.get("/ping")
def ping(source: str = "mta"):
    return {"ok": True, "source": source, "ts": int(time.time())}

@app.get("/status")
def status():
    return {
        "ok": True,
        "proxy_mode": PROXY_MODE,
        "upstream": UPSTREAM_CONVERTER or None,
        "note": "Этот сервис на Render только проксирует запросы на локальный воркер.",
    }

# -------- /search  ---------------------------------------------------------------
@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = 30):
    if not UPSTREAM_CONVERTER:
        # Можно сделать локальный поиск через Data API/yt-dlp, но для простоты — требуем апстрим
        return JSONResponse(status_code=503, content={
            "ok": False,
            "error": "no_upstream_configured",
            "msg": "Set UPSTREAM_CONVERTER to your cloudflared URL",
        })
    url = upstream_url("/search", {"q": q, "limit": limit})
    try:
        data = fetch_json("GET", url, None)
        return data
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": "upstream_failed", "msg": str(e)})

# -------- /convert  --------------------------------------------------------------
@app.post("/convert")
async def convert(request: Request):
    """
    Проксируем запрос на апстрим. Поддерживаются:
    - JSON-тело (video_id/title/nick/ip/serial)
    - query-параметры вида ?video_id=...
    - сырое текстовое тело с video_id/URL
    """
    if not UPSTREAM_CONVERTER:
        return JSONResponse(status_code=503, content={
            "ok": False,
            "error": "no_upstream_configured",
            "msg": "Set UPSTREAM_CONVERTER to your cloudflared URL",
        })

    # Соберём вход как есть, чтобы переслать наверх
    qp = dict(request.query_params)
    ctype = (request.headers.get("content-type") or "").lower()
    body_to_send = None

    # Попробуем JSON
    try:
        incoming = await request.json()
        # если клиент прислал строку — обернём
        body_to_send = incoming if isinstance(incoming, dict) else {"data": incoming}
    except Exception:
        # Попробуем сырое тело как текст → положим в {"raw": "..."}
        raw = await request.body()
        if raw:
            try:
                # может быть form-urlencoded
                if "application/x-www-form-urlencoded" in ctype and b"&" in raw:
                    from urllib.parse import parse_qs
                    form = parse_qs(raw.decode("utf-8", "ignore"))
                    body_to_send = {k: v[0] if isinstance(v, list) else v for k, v in form.items()}
                else:
                    body_to_send = {"raw": raw.decode("utf-8", "ignore")}
            except Exception:
                body_to_send = {"raw": ""}

    # Соберём URL апстрима
    url = upstream_url("/convert", qp)

    try:
        upstream_resp = fetch_json("POST", url, body_to_send)
        # Подправим mp3, если нужно
        if isinstance(upstream_resp, dict) and upstream_resp.get("ok") and upstream_resp.get("mp3"):
            upstream_resp["mp3"] = absolutize_mp3_link(upstream_resp["mp3"])
        return upstream_resp
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": "upstream_failed", "msg": str(e)})

# --------------------------------------------------------------------------------------
# Старт
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
