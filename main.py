"""
Clipzio resolver backend.

A tiny FastAPI service that turns an Instagram/TikTok link into a direct,
no-watermark video URL using yt-dlp (the most reliable extractor there is).

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

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000
Then set AppConfig.backendBase = "http://<your-ip>:8000" in the app.
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import yt_dlp

app = FastAPI(title="Clipzio Resolver", version="1.0.0")

# The app talks server-to-server, but CORS is handy if you ever call from web.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    # Prefer a single progressive mp4 (audio+video together) so the app can
    # save it straight to the gallery without any merging.
    "format": "b[ext=mp4][acodec!=none][vcodec!=none]/b[acodec!=none][vcodec!=none]/b",
}

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)


def _pick_progressive(info: dict) -> str | None:
    """Choose the best mp4 that already has both audio and video."""
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
    handle = info.get("uploader_id") or info.get("uploader") or info.get("channel")
    if not handle:
        return None
    return handle if str(handle).startswith("@") else f"@{handle}"


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/resolve")
def resolve(url: str = Query(..., description="TikTok or Instagram video URL")):
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Could not resolve: {e}")

    if not info:
        raise HTTPException(status_code=422, detail="Nothing found at that link")

    # A carousel/playlist may nest entries — take the first video.
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise HTTPException(status_code=422, detail="No video in that post")
        info = entries[0]

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


# ---------------------------------------------------------------------------
# Optional streaming proxy.
#
# Some CDN URLs (occasionally Instagram) only serve when the exact request
# headers are replayed. If a resolved downloadUrl ever 403s from the app,
# point the app at this instead by returning:
#     downloadUrl = f"{backendBase}/download?url=<original link>"
# and it will stream the bytes through the backend with the right headers.
# Costs backend bandwidth, so it's opt-in.
# ---------------------------------------------------------------------------
@app.get("/download")
async def download(url: str = Query(...)):
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("entries"):
            info = [e for e in info["entries"] if e][0]
        direct = _pick_progressive(info) if info else None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Could not resolve: {e}")
    if not direct:
        raise HTTPException(status_code=422, detail="No downloadable stream")

    async def _stream():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", direct, headers={"User-Agent": BROWSER_UA}) as r:
                async for chunk in r.aiter_bytes(64 * 1024):
                    yield chunk

    return StreamingResponse(_stream(), media_type="video/mp4")
