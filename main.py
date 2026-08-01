"""
Clipzio resolver backend.

A tiny FastAPI service that turns a video link into a direct, no-watermark
video URL using yt-dlp (the most reliable extractor there is).

The Flutter app calls:  GET  {backendBase}/resolve?url=<link>
and expects JSON:
    {
      "downloadUrl": "...",     # direct mp4 the app downloads
      "thumbnail":   "...",
      "author":      "@handle",
      "title":       "...",
      "duration":    12,          # seconds
      "noWatermark": true
    }

Instagram (and sometimes TikTok) rate-limit datacenter IPs. Two things fight
that here:
  * retry with backoff on transient failures, and
  * optional cookies (set IG_COOKIES_B64 to a base64 cookies.txt from a burner
    account) which make Instagram requests reliable and fast.
"""
import base64
import os
import tempfile
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import yt_dlp

app = FastAPI(title="Clipzio Resolver", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)

# --- optional cookies (helps Instagram a lot) --------------------------------
_COOKIEFILE = None


def _setup_cookies():
    global _COOKIEFILE
    b64 = os.environ.get("IG_COOKIES_B64")
    if b64:
        try:
            data = base64.b64decode(b64)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tmp.write(data)
            tmp.close()
            _COOKIEFILE = tmp.name
            print("cookies: loaded from IG_COOKIES_B64")
            return
        except Exception as e:  # noqa: BLE001
            print("cookies: failed to load IG_COOKIES_B64:", e)
    if os.path.exists("cookies.txt"):
        _COOKIEFILE = "cookies.txt"
        print("cookies: using cookies.txt")


_setup_cookies()


def _ydl_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": (
            "b[ext=mp4][acodec!=none][vcodec!=none]/"
            "b[acodec!=none][vcodec!=none]/b"
        ),
        "http_headers": {"User-Agent": BROWSER_UA},
        "socket_timeout": 20,
    }
    if _COOKIEFILE:
        opts["cookiefile"] = _COOKIEFILE
    return opts


def _extract(url: str) -> dict:
    """Extract info with a few retries (Instagram is flaky on datacenter IPs)."""
    last_err = None
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                return info
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.2 * (attempt + 1))
    raise HTTPException(
        status_code=422,
        detail=f"Could not resolve after retries: {last_err}",
    )


def _pick_progressive(info: dict) -> str | None:
    best = None
    for f in info.get("formats") or []:
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        if has_v and has_a and f.get("url"):
            h = f.get("height") or 0
            if best is None or h > (best.get("height") or 0):
                best = f
    if best:
        return best["url"]
    return info.get("url")


def _author(info: dict) -> str | None:
    # Prefer the human handle over Instagram's numeric uploader_id.
    handle = (
        info.get("uploader")
        or info.get("channel")
        or info.get("uploader_id")
    )
    if not handle:
        return None
    return handle if str(handle).startswith("@") else f"@{handle}"


def _first_video(info: dict) -> dict:
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise HTTPException(status_code=422, detail="No video in that post")
        return entries[0]
    return info


@app.get("/health")
def health():
    return {"ok": True, "cookies": bool(_COOKIEFILE)}


@app.get("/resolve")
def resolve(url: str = Query(..., description="Video URL")):
    info = _first_video(_extract(url))
    download_url = _pick_progressive(info)
    if not download_url:
        raise HTTPException(status_code=422, detail="No downloadable stream")
    return {
        "downloadUrl": download_url,
        "thumbnail": info.get("thumbnail"),
        "author": _author(info),
        "title": info.get("title") or info.get("description"),
        "duration": int(info.get("duration") or 0),
        "noWatermark": True,
    }


@app.get("/download")
async def download(url: str = Query(...)):
    info = _first_video(_extract(url))
    direct = _pick_progressive(info)
    if not direct:
        raise HTTPException(status_code=422, detail="No downloadable stream")

    async def _stream():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream(
                "GET", direct, headers={"User-Agent": BROWSER_UA}
            ) as r:
                async for chunk in r.aiter_bytes(64 * 1024):
                    yield chunk

    return StreamingResponse(_stream(), media_type="video/mp4")
