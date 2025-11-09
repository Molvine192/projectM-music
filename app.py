# gateway.py — тонкий шлюз Render → Cloudflare Tunnel → твоя локалка
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

UPSTREAM = os.environ.get("UPSTREAM_CONVERTER")  # напр.: https://abc.trycloudflare.com
if not UPSTREAM:
    raise RuntimeError("Set UPSTREAM_CONVERTER env var to your Cloudflare Tunnel URL")

app = FastAPI(title="ProjectM Music Gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

TIMEOUT = httpx.Timeout(60.0, connect=10.0)
client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)

def u(path: str) -> str:
    return f"{UPSTREAM.rstrip('/')}{path}"

@app.get("/")
async def root():
    # простая сводка
    return {"service": "Gateway", "upstream": UPSTREAM}

@app.get("/ping")
async def ping():
    try:
        r = await client.get(u("/ping"))
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(502, f"upstream_error: {e}")

@app.get("/status")
async def status():
    try:
        r = await client.get(u("/status"))
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(502, f"upstream_error: {e}")

@app.get("/search")
async def search(request: Request):
    try:
        r = await client.get(u("/search"), params=dict(request.query_params))
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(502, f"upstream_error: {e}")

# /convert: поддержим и GET, и POST — как у тебя на локалке
@app.get("/convert")
async def convert_get(request: Request):
    try:
        r = await client.get(u("/convert"), params=dict(request.query_params))
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(502, f"upstream_error: {e}")

@app.post("/convert")
async def convert_post(request: Request):
    try:
        # прокси с сохранением query и тела
        qp = dict(request.query_params)
        body = await request.body()
        headers = {"Content-Type": request.headers.get("content-type", "application/json")}
        r = await client.post(u("/convert"), params=qp, content=body, headers=headers)
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPStatusError as he:
        # если upstream вернул 4xx/5xx с JSON — пробросим как есть
        try:
            return JSONResponse(he.response.json(), status_code=he.response.status_code)
        except Exception:
            raise HTTPException(he.response.status_code, he.response.text)
    except Exception as e:
        raise HTTPException(502, f"upstream_error: {e}")

# /media — стримим байты (mp3)
@app.get("/media/{filename}")
async def media(filename: str):
    try:
        r = await client.get(u(f"/media/{filename}"))
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text)
        return StreamingResponse(iter([r.content]), media_type=r.headers.get("content-type", "audio/mpeg"))
    except Exception as e:
        raise HTTPException(502, f"upstream_error: {e}")
