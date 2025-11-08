from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os, re, time, json, sqlite3, threading, base64, subprocess, urllib.request, logging, traceback
from typing import Optional, Union, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs
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

# Piped fallback instances
PIPED_INSTANCES: List[str] = [
    *(os.environ.get("PIPED_INSTANCE", "https://pipedapi.kavin.rocks,https://piped.mha.fi,https://piped.video,https://piped.projectsegfau.lt,https://piped.phoenixthrush.com").split(",")),
]
PIPED_INSTANCES = [u.strip() for u in PIPED_INSTANCES if u.strip()]
PIPED_TIMEOUT = int(os.environ.get("PIPED_TIMEOUT", "25"))

DB_PATH = os.path.join(MEDIA_ROOT, "history.sqlite3")

# ---------------- App ----------------
app = FastAPI(title="YouTube MP3 Bridge for MTA")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
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
            video_id TEXT, title TEXT, nick TEXT, ip TEXT, serial TEXT
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, source TEXT
        );
    """)
    conn.commit(); conn.close()
db_init()

def db_add_play(video_id: str, title: str, nick: str, ip: str, serial: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO plays(ts, video_id, title, nick, ip, serial) VALUES(?,?,?,?,?,?)",
                 (int(time.time()), video_id, title, nick, ip, serial))
    conn.commit(); conn.close()

def db_add_ping(source: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO pings(ts, source) VALUES(?,?)", (int(time.time()), source))
    conn.commit(); conn.close()

def db_recent(limit=50):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ts, video_id, title, nick, ip, serial FROM plays ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [{"ts": r[0], "video_id": r[1], "title": r[2], "nick": r[3], "ip": r[4], "serial": r[5]} for r in rows]

# ---------------- Cookies support (optional) ----------------
COOKIES_PATH = None
if os.environ.get("YTDLP_COOKIES_B64"):
    try:
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "wb") as f:
            f.write(base64.b64decode(os.environ["YTDLP_COOKIES_B64"]))
    except Exception as e:
        COOKIES_PATH = None; logger.warning("Failed to load YTDLP_COOKIES_B64: %s", e)
elif os.environ.get("YTDLP_COOKIES"):
    try:
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            f.write(os.environ["YTDLP_COOKIES"])
    except Exception as e:
        COOKIES_PATH = None; logger.warning("Failed to load YTDLP_COOKIES: %s", e)

# ---------------- Utilities ----------------
UA = os.environ.get("YTDLP_UA", "Mozilla/5.0")
GEO_BYPASS_COUNTRY = os.environ.get("YTDLP_GEO_BYPASS_COUNTRY", "US")

# порядок перебора клиентов YouTube API
PLAYER_CLIENTS_WITH_COOKIES = ["web", "web_embedded", "android", "ios", "tv_embedded"]
PLAYER_CLIENTS_NO_COOKIES   = ["android", "web", "web_embedded", "ios", "tv_embedded"]

def ydl_base_opts(player_client: str) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": os.path.join(MEDIA_ROOT, ".cache"),
        "retries": 3,
        "fragment_retries": 3,
        "http_headers": {"User-Agent": UA},
        "force_ipv4": True,
        "ignoreconfig": True,     # игнор любых внешних конфигов yt-dlp
        "geo_bypass_country": GEO_BYPASS_COUNTRY,
        "extractor_args": {"youtube": {"player_client": [player_client]}},
        # доп. мягкие совместимости (необязательно, но иногда помогает)
        "compat_opts": [],
    }
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    return opts

SEARCH_OPTS = {**ydl_base_opts("android"), "extract_flat": "in_playlist"}  # поиск надёжнее с android
YDL_INFO_DEFAULT = ydl_base_opts("web")

executor = ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "2")))

def mp3_path_for(video_id: str) -> str:
    return os.path.join(MEDIA_ROOT, f"{video_id}.mp3")

def is_fresh(path: str) -> bool:
    return os.path.exists(path) and (time.time() - os.path.getmtime(path) < CACHE_TTL_SECONDS)

def cleanup_old_files():
    now = time.time()
    for name in os.listdir(MEDIA_ROOT):
        if name.endswith(".mp3"):
            p = os.path.join(MEDIA_ROOT, name)
            if (now - os.path.getmtime(p)) > CACHE_TTL_SECONDS:
                try: os.remove(p)
                except Exception: pass

def schedule_cleanup():
    def loop():
        while True:
            cleanup_old_files(); time.sleep(CLEANUP_INTERVAL_SECONDS)
    threading.Thread(target=loop, daemon=True).start()
schedule_cleanup()

# ---------- YouTube helpers ----------
YOUTUBE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{5,20}$")
YOUTUBE_URL_RE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/))([0-9A-Za-z_-]{5,20})")

def extract_video_id(candidate: str) -> Optional[str]:
    candidate = (candidate or "").strip()
    if YOUTUBE_ID_RE.fullmatch(candidate): return candidate
    m = YOUTUBE_URL_RE.search(candidate);  return m.group(1) if m else None

def pick_best_audio_from_formats(formats) -> Optional[str]:
    best_url, best_abr = None, -1
    for f in formats or []:
        vcodec = f.get("vcodec"); acodec = f.get("acodec"); url = f.get("url")
        if url and (vcodec in (None, "none")) and (acodec not in (None, "none")):
            abr = f.get("abr") or 0
            try: abr = int(abr)
            except: abr = 0
            if abr > best_abr:
                best_abr, best_url = abr, url
    return best_url

def ffmpeg_transcode_to_mp3(input_url: str, target_path: str) -> bool:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    cmd = ["ffmpeg","-y","-i",input_url,"-vn","-acodec","libmp3lame","-b:a","192k", target_path]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        if proc.returncode != 0:
            logger.warning("ffmpeg stderr: %s", proc.stderr.decode("utf-8","ignore")[-400:])
        return proc.returncode == 0
    except Exception as e:
        logger.warning("ffmpeg failed: %s", e); return False

def piped_best_audio_url(video_id: str) -> Optional[str]:
    for base in PIPED_INSTANCES:
        try:
            url = f"{base.rstrip('/')}/api/v1/streams/{video_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=PIPED_TIMEOUT) as resp:
                if resp.status != 200:
                    continue
                data = json.loads(resp.read().decode("utf-8","ignore"))
                streams = data.get("audioStreams") or []
                best = None; best_rate = -1
                for s in streams:
                    try: rate = int(s.get("bitrate") or s.get("bitrateKbps") or 0)
                    except: rate = 0
                    if s.get("url") and rate > best_rate:
                        best_rate, best = rate, s
                if best and best.get("url"):
                    return best["url"]
        except Exception as e:
            logger.warning("piped fail on %s: %s", base, e)
    return None

def env_ytdl_vars() -> Dict[str, str]:
    out = {}
    for k, v in os.environ.items():
        if k.startswith("YT_DLP") or k.startswith("YTDL"):
            out[k] = v if len(v) < 200 else (v[:200] + "...(truncated)")
    return out

def try_extract_info_with_clients(video_id: str) -> Optional[str]:
    """
    Пытаемся получить URL лучшей аудио-дорожки, перебирая разные player_client.
    Возвращает прямой URL потока или None.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    has_cookies = bool(COOKIES_PATH)
    order = PLAYER_CLIENTS_WITH_COOKIES if has_cookies else PLAYER_CLIENTS_NO_COOKIES

    for client in order:
        opts = ydl_base_opts(client)
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = (info or {}).get("formats") or []
            logger.info("extract with client=%s: formats_total=%d", client, len(fmts))
            stream_url = pick_best_audio_from_formats(fmts)
            if stream_url:
                logger.info("extract with client=%s: picked audio", client)
                return stream_url
        except Exception as e:
            logger.warning("extract failed client=%s: %s", client, str(e).splitlines()[-1])
            continue
    return None

# ---------------- Endpoints ----------------
@app.get("/ping")
def ping(source: str = "mta"):
    db_add_ping(source); return {"ok": True, "ts": int(time.time()), "source": source}

@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = MAX_RESULTS):
    query = f"ytsearch{min(limit, MAX_RESULTS)}:{q}"
    with YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = info.get("entries", []) if info else []
    results = []
    for e in entries:
        vid = e.get("id"); title = e.get("title")
        if not (vid and title): continue
        dur = e.get("duration"); ch = e.get("channel") or e.get("uploader")
        url = f"https://www.youtube.com/watch?v={vid}"
        results.append({"id": vid, "title": title, "duration": dur, "channel": ch, "url": url})
    return {"query": q, "count": len(results), "items": results}

@app.post("/convert")
async def convert(request: Request):
    # ---- собрать входные данные
    vid = title = nick = ip = serial = ""
    qp = request.query_params
    ctype = (request.headers.get("content-type") or "").lower()
    data: Union[dict, list, str] = {}
    try:
        data = await request.json()
        if isinstance(data, str):
            data = json.loads(data)
    except Exception:
        pass
    if isinstance(data, dict):
        def pick(d,*ks):
            for k in ks:
                if d.get(k): return str(d[k])
            for nest in ("data","payload","body"):
                if isinstance(d.get(nest), dict):
                    v = pick(d[nest], *ks)
                    if v: return v
            return ""
        vid = pick(data,"video_id","videoId","id","url") or ""
        title = pick(data,"title") or ""
        nick = pick(data,"nick","nickname","user") or ""
        ip = pick(data,"ip") or ""
        serial = pick(data,"serial","serialNumber") or ""
    # form/raw
    if not vid:
        raw = await request.body()
        if b"&" in raw or "application/x-www-form-urlencoded" in ctype:
            try:
                form = parse_qs(raw.decode("utf-8","ignore"))
                vid = (form.get("video_id",[""])[0] or form.get("videoId",[""])[0] or form.get("id",[""])[0] or form.get("url",[""])[0])
                title = title or form.get("title",[""])[0]
                nick = nick or form.get("nick",[""])[0]
                ip = ip or form.get("ip",[""])[0]
                serial = serial or form.get("serial",[""])[0]
            except Exception:
                pass
        elif raw:
            vid = raw.decode("utf-8","ignore").strip()
    # query fallback
    if not vid:
        vid = qp.get("video_id") or qp.get("id") or qp.get("url") or ""
        title = title or qp.get("title") or ""
        nick = nick or qp.get("nick") or ""
        ip = ip or qp.get("ip") or ""
        serial = serial or qp.get("serial") or ""

    logger.info(f"/convert ctype={ctype} vid_raw={repr(vid)} qp={dict(qp)}")
    vid = extract_video_id(vid or "")
    if not vid:
        return JSONResponse(status_code=200, content={"ok": False, "error": "video_id_missing_or_invalid"})

    target = mp3_path_for(vid)
    if not is_fresh(target):
        # 1) yt-dlp: перебор клиентов
        stream_url = try_extract_info_with_clients(vid)
        if stream_url:
            if not ffmpeg_transcode_to_mp3(stream_url, target):
                return JSONResponse(status_code=200, content={"ok": False, "error": "ffmpeg_failed"})
        else:
            # 2) Piped → ffmpeg
            audio_url = piped_best_audio_url(vid)
            if audio_url:
                if not ffmpeg_transcode_to_mp3(audio_url, target):
                    return JSONResponse(status_code=200, content={"ok": False, "error": "ffmpeg_failed"})
            else:
                return JSONResponse(status_code=200, content={
                    "ok": False,
                    "error": "youtube_requires_cookies_or_piped_failed",
                    "cookies_loaded": bool(COOKIES_PATH),
                })

    db_add_play(vid, title or "", nick or "", ip or "", serial or "")
    rel = os.path.basename(target)
    return {"ok": True, "video_id": vid, "mp3": f"/media/{rel}"}

# ---------- Diagnostics ----------
@app.get("/diag")
def diag(video_id: str):
    vid = extract_video_id(video_id)
    if not vid:
        return {"ok": False, "where": "input", "msg": "bad video_id"}
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        with YoutubeDL(YDL_INFO_DEFAULT) as ydl:
            params = dict(ydl.params)
            info = ydl.extract_info(url, download=False)
        fmts = info.get("formats") or []
        audio_only = [f for f in fmts if (f.get("vcodec") in (None,"none")) and (f.get("acodec") not in (None,"none")) and f.get("url")]
        sample = []
        for f in audio_only[:5]:
            sample.append({
                "ext": f.get("ext"),
                "acodec": f.get("acodec"),
                "abr": f.get("abr"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "url_len": len(f.get("url") or ""),
            })
        return {
            "ok": True,
            "cookies_loaded": bool(COOKIES_PATH),
            "ua": UA,
            "player_client": YDL_INFO_DEFAULT.get("extractor_args",{}).get("youtube",{}).get("player_client"),
            "ydl_params": {k: v for k, v in params.items() if k in ("format","ignoreconfig","cookiefile","extractor_args","http_headers","geo_bypass_country")},
            "env_ytdl": env_ytdl_vars(),
            "formats_total": len(fmts),
            "audio_only": len(audio_only),
            "audio_sample": sample,
        }
    except Exception as e:
        return {
            "ok": False, "where": "yt_dlp", "msg": str(e),
            "trace": traceback.format_exc(limit=2),
            "env_ytdl": env_ytdl_vars(),
            "params": YDL_INFO_DEFAULT,
        }

@app.get("/diag_clients")
def diag_clients(video_id: str):
    vid = extract_video_id(video_id)
    if not vid:
        return {"ok": False, "msg": "bad video_id"}
    url = f"https://www.youtube.com/watch?v={vid}"
    has_cookies = bool(COOKIES_PATH)
    order = PLAYER_CLIENTS_WITH_COOKIES if has_cookies else PLAYER_CLIENTS_NO_COOKIES
    out = []
    for client in order:
        opts = ydl_base_opts(client)
        item = {"client": client}
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = (info or {}).get("formats") or []
            audio_only = [f for f in fmts if (f.get("vcodec") in (None,"none")) and (f.get("acodec") not in (None,"none")) and f.get("url")]
            item.update({
                "ok": True,
                "formats_total": len(fmts),
                "audio_only": len(audio_only),
                "picked": bool(pick_best_audio_from_formats(fmts)),
            })
        except Exception as e:
            item.update({"ok": False, "error": str(e)})
        out.append(item)
    return {"ok": True, "cookies_loaded": has_cookies, "ua": UA, "results": out}

@app.get("/diag_piped")
def diag_piped(video_id: str):
    vid = extract_video_id(video_id)
    if not vid:
        return {"ok": False, "msg": "bad video_id"}
    results = []
    for base in PIPED_INSTANCES:
        try:
            url = f"{base.rstrip('/')}/api/v1/streams/{vid}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=PIPED_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8","ignore"))
                results.append({"instance": base, "status": resp.status, "have_audio": bool(body.get("audioStreams"))})
        except Exception as e:
            results.append({"instance": base, "status": "error", "error": str(e)})
    return {"ok": True, "results": results}

# ---------- Static / status ----------
@app.get("/media/{filename}")
def media(filename: str):
    if not re.fullmatch(r"[0-9A-Za-z_-]+\.mp3", filename): raise HTTPException(status_code=404, detail="not found")
    path = os.path.join(MEDIA_ROOT, filename)
    if not os.path.exists(path): raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="audio/mpeg")

@app.get("/status")
def status():
    files = []
    for name in os.listdir(MEDIA_ROOT):
        if name.endswith(".mp3"):
            p = os.path.join(MEDIA_ROOT, name)
            files.append({"file": name, "size": os.path.getsize(p), "age_seconds": int(time.time()-os.path.getmtime(p))})
    return {"now": int(time.time()), "cache_ttl_sec": CACHE_TTL_SECONDS, "files": sorted(files, key=lambda x: x["age_seconds"]), "recent_plays": db_recent(50)}

@app.get("/")
def root():
    return {
        "service": "YouTube MP3 Bridge for MTA",
        "endpoints": ["/search?q=", "/convert", "/media/<file>", "/status", "/ping", "/diag?video_id=", "/diag_clients?video_id=", "/diag_piped?video_id="],
        "cache_ttl_sec": CACHE_TTL_SECONDS,
        "cookies_loaded": bool(COOKIES_PATH),
        "ua": UA,
        "piped_instances": PIPED_INSTANCES,
    }

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
