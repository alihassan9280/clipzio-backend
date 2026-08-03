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
from fastapi.responses import StreamingResponse, HTMLResponse
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


PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipzio - Privacy Policy</title>
<style>body{font-family:system-ui,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.6;color:#1a1a1a}h1{font-size:26px}h2{font-size:19px;margin-top:28px}small{color:#666}</style>
</head><body>
<h1>Clipzio - Privacy Policy</h1>
<small>Last updated: 1 August 2026</small>

<p>This Privacy Policy explains how the Clipzio app ("Clipzio", "we", "us")
handles information. By using Clipzio you agree to this policy.</p>

<h2>Information we collect</h2>
<p>Clipzio does <strong>not</strong> require an account and does not ask for your
name, email, or other personal identifiers. We do not sell your data.</p>
<ul>
<li><strong>Clipboard:</strong> When you open the app it checks your clipboard for
a video link so it can offer a one-tap action. This check happens on your device;
the clipboard content is not stored or transmitted unless you start a download.</li>
<li><strong>Links you submit:</strong> When you start a download, the video link
you provide is sent to our processing server only to retrieve the corresponding
video file. Links are used to fulfil your request and are not used to profile you.</li>
<li><strong>Saved videos:</strong> Downloaded videos are stored in your device's
gallery. They stay on your device; we do not receive copies.</li>
</ul>

<h2>Permissions</h2>
<p>Clipzio requests storage/media permission solely to save videos to your gallery,
and internet access to fetch videos.</p>

<h2>Third parties</h2>
<p>To retrieve videos, requests may be processed through our server and the source
content-delivery networks. We do not share personal information with advertisers.
This version of the app does not display ads.</p>

<h2>Data retention</h2>
<p>We do not maintain user accounts or long-term personal records. Transient
request data is used only to complete your download.</p>

<h2>Children</h2>
<p>Clipzio is not directed to children under 13, and we do not knowingly collect
information from them.</p>

<h2>Your responsibility</h2>
<p>Clipzio is a tool. You are responsible for only downloading content you own or
have permission to use, and for complying with the terms of the sites you use and
applicable copyright law.</p>

<h2>Changes</h2>
<p>We may update this policy; the "Last updated" date will change accordingly.</p>

<h2>Contact</h2>
<p>Questions: <a href="mailto:ah457003@gmail.com">ah457003@gmail.com</a></p>
</body></html>"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return PRIVACY_HTML


# ---------------------------------------------------------------------------
# App update config. Bump APP_LATEST_BUILD when you publish a new version so
# older apps show the "Update now" popup. Set APP_MIN_BUILD to force-update
# (block) builds older than it.
# ---------------------------------------------------------------------------
APP_LATEST_BUILD = 2      # newest versionCode published on Play
APP_MIN_BUILD = 1         # builds below this are force-updated (blocked)
APP_UPDATE_URL = "https://play.google.com/store/apps/details?id=com.clipzio.clipzio"
APP_UPDATE_MESSAGE = "A new version of Clipzio is available with improvements."


@app.get("/config")
def config():
    return {
        "latest_build": APP_LATEST_BUILD,
        "min_build": APP_MIN_BUILD,
        "url": APP_UPDATE_URL,
        "message": APP_UPDATE_MESSAGE,
    }
