from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import re
import time
import json
import sqlite3
import threading
from typing import Optional, Union
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs
import logging
import base64
import subprocess
import urllib.request
import urllib.error

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
import uvicorn

# ---------------- Config ----------------
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/data")
os.makedirs(MEDIA_ROOT, exist_ok=True)

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", str(24 * 3600)))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "900"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "30"))
PORT = int(os.environ.get("PORT", "8080"))

# Fallback через Piped (работает без куков)
PIPED_FALLBACK = os.environ.get("PIPED_FALLBACK", "1") not in ("0", "false", "False", "")
PIPED_INSTANCE = os.environ.get("PIPED_INSTANCE", "https://piped.video")
PIPED_TIMEOUT = int(os.environ.get("PIPED_TIMEOUT", "15"))

DB_PATH = os.path.join(MEDIA_ROOT, "history.sqlite3")

# ---------------- App ----------------
app = FastAPI(title="YouTube MP3 Bridge for MTA")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Logging ----------------
logger = logging.getLogger("convert")
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO)

# ---------------- Database ----------------
def db_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS plays(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            video_id TEXT,
            title TEXT,
            nick TEXT,
            ip TEXT,
            serial TEXT
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            source TEXT
        );
    """)
    conn.commit()
    conn.close()

db_init()

def db_add_play(video_id: str, title: str, nick: str, ip: str, serial: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO plays(ts, video_id, title, nick, ip, serial) VALUES(?,?,?,?,?,?)",
              (int(time.time()), video_id, title, nick, ip, serial))
    conn.commit()
    conn.close()

def db_add_ping(source: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO pings(ts, source) VALUES(?,?)", (int(time.time()), source))
    conn.commit()
    conn.close()

def db_recent(limit=50):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT ts, video_id, title, nick, ip, serial FROM plays ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [
        {"ts": r[0], "video_id": r[1], "title": r[2], "nick": r[3], "ip": r[4], "serial": r[5]}
        for r in rows
    ]

# ---------------- Cookies support (optional) ----------------
COOKIES_PATH = None
if os.environ.get("YTDLP_COOKIES_B64"):
    try:
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "wb") as f:
            f.write(base64.b64decode(os.environ["YTDLP_COOKIES_B64"]))
    except Exception as e:
        COOKIES_PATH = None
        logger.warning("Failed to load YTDLP_COOKIES_B64: %s", e)
elif os.environ.get("YTDLP_COOKIES"):
    try:
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            f.write(os.environ["YTDLP_COOKIES"])
    except Exception as e:
        COOKIES_PATH = None
        logger.warning("Failed to load YTDLP_COOKIES: %s", e)

# ---------------- Utilities ----------------
YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "noplaylist": True,
    "cachedir": os.path.join(MEDIA_ROOT, ".cache"),
    "retries": 3,
    "fragment_retries": 3,
    "http_headers": {"User-Agent": "Mozilla/5.0"},
    "force_ipv4": True,
    "extractor_args": {"youtube": {"player_client": ["android"]}},
}
if COOKIES_PATH:
    YDL_BASE_OPTS["cookiefile"] = COOKIES_PATH

AUDIO_OPTS = {
    **YDL_BASE_OPTS,
    "format": "bestaudio/best",
    "outtmpl": os.path.join(MEDIA_ROOT, "%(id)s.%(ext)s"),
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
}

SEARCH_OPTS = {**YDL_BASE_OPTS, "extract_flat": "in_playlist"}

executor = ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "2")))

def mp3_path_for(video_id: str) -> str:
    return os.path.join(MEDIA_ROOT, f"{video_id}.mp3")

def is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < CACHE_TTL_SECONDS

def cleanup_old_files():
    now = time.time()
    for name in os.listdir(MEDIA_ROOT):
        if not name.endswith(".mp3"):
            continue
        p = os.path.join(MEDIA_ROOT, name)
        if (now - os.path.getmtime(p)) > CACHE_TTL_SECONDS:
            try:
                os.remove(p)
            except Exception:
                pass

def schedule_cleanup():
    def loop():
        while True:
            cleanup_old_files()
            time.sleep(CLEANUP_INTERVAL_SECONDS)
    threading.Thread(target=loop, daemon=True).start()

schedule_cleanup()

# ---------------- Piped fallback ----------------
def piped_stream_info(video_id: str) -> Optional[dict]:
    url = f"{PIPED_INSTANCE.rstrip('/')}/api/v1/streams/{video_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=PIPED_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        data = resp.read()
    try:
        return json.loads(data.decode("utf-8", "ignore"))
    except Exception:
        return None

def piped_best_audio_url(video_id: str) -> Optional[str]:
    info = piped_stream_info(video_id)
    if not info:
        return None
    streams = info.get("audioStreams") or []
    best = None
    best_rate = -1
    for s in streams:
        try:
            abr = int(s.get("bitrate") or s.get("bitrateKbps") or 0)
        except Exception:
            abr = 0
        if abr > best_rate and s.get("url"):
            best = s
            best_rate = abr
    return best.get("url") if best else None

def ffmpeg_transcode_to_mp3(input_url: str, target_path: str) -> bool:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_url,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        target_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        return proc.returncode == 0
    except Exception as e:
        logger.warning("ffmpeg failed: %s", e)
        return False

# ---------------- Endpoints ----------------
@app.get("/ping")
def ping(source: str = "mta"):
    db_add_ping(source)
    return {"ok": True, "ts": int(time.time()), "source": source}

@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = MAX_RESULTS):
    query = f"ytsearch{min(limit, MAX_RESULTS)}:{q}"
    with YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = info.get("entries", [])
    results = []
    for e in entries or []:
        vid = e.get("id")
        title = e.get("title")
        dur = e.get("duration")
        ch = e.get("channel") or e.get("uploader")
        url = f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url")
        if vid and title:
            results.append({"id": vid, "title": title, "duration": dur, "channel": ch, "url": url})
    return {"query": q, "count": len(results), "items": results}

# ---------- /convert ----------
YOUTUBE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{5,20}$")
YOUTUBE_URL_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/))([0-9A-Za-z_-]{5,20})")

def extract_video_id(candidate: str) -> Optional[str]:
    candidate = (candidate or "").strip()
    if YOUTUBE_ID_RE.fullmatch(candidate):
        return candidate
    m = YOUTUBE_URL_RE.search(candidate)
    if m:
        return m.group(1)
    return None

def _from_any_dict(d: dict, *keys: str) -> Optional[str]:
    for k in keys:
        if k in d and d[k]:
            return str(d[k])
    for nested_key in ("data", "payload", "body"):
        sub = d.get(nested_key)
        if isinstance(sub, dict):
            val = _from_any_dict(sub, *keys)
            if val:
                return val
    return None

@app.post("/convert")
async def convert(request: Request):
    vid = title = nick = ip = serial = ""
    qp = request.query_params
    raw = b""
    ctype = (request.headers.get("content-type") or "").lower()

    vid_qp   = qp.get("video_id") or qp.get("id") or qp.get("url") or ""
    title_qp = qp.get("title") or ""
    nick_qp  = qp.get("nick") or ""
    ip_qp    = qp.get("ip") or ""
    serial_qp= qp.get("serial") or ""

    data: Union[dict, list, str] = {}
    try:
        data = await request.json()
        if isinstance(data, str):
            data = json.loads(data)
    except Exception:
        data = {}

    if isinstance(data, dict):
        vid   = _from_any_dict(data, "video_id", "videoId", "id", "url") or ""
        title = _from_any_dict(data, "title") or ""
        nick  = _from_any_dict(data, "nick", "nickname", "user") or ""
        ip    = _from_any_dict(data, "ip") or ""
        serial= _from_any_dict(data, "serial", "serialNumber") or ""
    elif isinstance(data, list) and data:
        if isinstance(data[0], dict):
            vid   = _from_any_dict(data[0], "video_id", "videoId", "id", "url") or ""
            title = _from_any_dict(data[0], "title") or ""

    if not vid:
        raw = await request.body()
        if b"&" in raw or "application/x-www-form-urlencoded" in ctype:
            try:
                form = parse_qs(raw.decode("utf-8", "ignore"))
                vid   = (form.get("video_id", [""])[0] or form.get("videoId", [""])[0]
                        or form.get("id", [""])[0] or form.get("url", [""])[0])
                title = title or form.get("title", [""])[0]
                nick  = nick  or form.get("nick",  [""])[0]
                ip    = ip    or form.get("ip",    [""])[0]
                serial= serial or form.get("serial",[""])[0]
            except Exception:
                pass
        elif raw:
            vid = raw.decode("utf-8", "ignore").strip()

    if not vid:
        vid   = vid_qp
        title = title or title_qp
        nick  = nick or nick_qp
        ip    = ip or ip_qp
        serial= serial or serial_qp

    logger.info(f"/convert ctype={ctype} vid_raw={repr(vid)} qp={dict(qp)} raw_len={len(raw)}")
    vid = extract_video_id(vid or "")
    if not vid:
        return JSONResponse(status_code=200, content={"ok": False, "error": "video_id_missing_or_invalid"})

    target = mp3_path_for(vid)
    if not is_fresh(target):
        url = f"https://www.youtube.com/watch?v={vid}"
        # 1) yt-dlp
        try:
            with YoutubeDL(AUDIO_OPTS) as ydl:
                ydl.download([url])
        except DownloadError as e:
            logger.warning("yt-dlp failed: %s", str(e).splitlines()[-1])
            # 2) Piped → ffmpeg
            if PIPED_FALLBACK:
                audio_url = None
                try:
                    audio_url = piped_best_audio_url(vid)
                except Exception as pe:
                    logger.warning("piped fetch failed: %s", pe)
                if audio_url:
                    ok = ffmpeg_transcode_to_mp3(audio_url, target)
                    if not ok:
                        return JSONResponse(status_code=200, content={"ok": False, "error": "ffmpeg_failed"})
                else:
                    return JSONResponse(status_code=200, content={
                        "ok": False,
                        "error": "youtube_requires_cookies_or_piped_failed",
                        "cookies_loaded": bool(COOKIES_PATH),
                    })
            else:
                return JSONResponse(status_code=200, content={
                    "ok": False,
                    "error": "youtube_requires_cookies",
                    "cookies_loaded": bool(COOKIES_PATH),
                })

    db_add_play(vid, title or "", nick or "", ip or "", serial or "")
    rel = os.path.basename(target)
    return {"ok": True, "video_id": vid, "mp3": f"/media/{rel}"}

# ---------- Static ----------
@app.get("/media/{filename}")
def media(filename: str):
    if not re.fullmatch(r"[0-9A-Za-z_-]+\.mp3", filename):
        raise HTTPException(status_code=404, detail="not found")
    path = os.path.join(MEDIA_ROOT, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="audio/mpeg")

@app.get("/status")
def status():
    files = []
    for name in os.listdir(MEDIA_ROOT):
        if name.endswith(".mp3"):
            p = os.path.join(MEDIA_ROOT, name)
            files.append({
                "file": name,
                "size": os.path.getsize(p),
                "age_seconds": int(time.time() - os.path.getmtime(p))
            })
    return {
        "now": int(time.time()),
        "cache_ttl_sec": CACHE_TTL_SECONDS,
        "files": sorted(files, key=lambda x: x["age_seconds"]),
        "recent_plays": db_recent(50)
    }

@app.get("/")
def root():
    return {
        "service": "YouTube MP3 Bridge for MTA",
        "endpoints": ["/search?q=", "/convert", "/media/<file>", "/status", "/ping"],
        "cache_ttl_sec": CACHE_TTL_SECONDS,
        "cookies_loaded": bool(COOKIES_PATH),
        "piped_fallback": PIPED_FALLBACK,
        "piped_instance": PIPED_INSTANCE,
    }

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
