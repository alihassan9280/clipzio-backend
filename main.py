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

Instagram (and sometimes TikTok) rate-limit datacenter IPs. Three things fight
that here:
  * cookies (set IG_COOKIES_B64 to a base64 cookies.txt from a burner
    account) - free and unlimited, tried first for Instagram,
  * free public Instagram mirrors (no key) next,
  * RapidAPI (RAPIDAPI_KEY) only as a last resort, and
  * retry with backoff on transient failures.

TikTok blocks yt-dlp from datacenter IPs, so TikTok links resolve via tikwm
(free, no key) first, with yt-dlp as the backup.
"""
import base64
import html
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from collections import deque

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
import httpx
import yt_dlp

app = FastAPI(title="Clipzio Resolver", version="1.2.0")

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

# --- which links we accept ---------------------------------------------------
# Only these sites (and their subdomains) are resolved. Anything else is
# rejected so the server can't be used to fetch arbitrary/internal URLs.
# Add more with ALLOWED_HOSTS="instagram.com,tiktok.com,example.com".
ALLOWED_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get(
        "ALLOWED_HOSTS", "instagram.com,instagr.am,tiktok.com,tiktokv.com"
    ).split(",")
    if h.strip()
)
INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")
TIKTOK_HOSTS = ("tiktok.com", "tiktokv.com")


def _host_matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _check_url(url: str) -> tuple[str, str]:
    """Return (normalized link, hostname), or 400 if it isn't a supported site."""
    url = url.strip()
    if "://" not in url:  # "instagram.com/reel/..." -> "https://instagram.com/reel/..."
        url = "https://" + url
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        host = ""
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in ("http", "https")
        or not _host_matches(host, ALLOWED_HOSTS)
    ):
        raise HTTPException(status_code=400, detail="Unsupported link")
    return url, host


# --- simple per-IP rate limit (protects the RapidAPI quota) -----------------
# Off by default (unlimited downloads). Set RATE_LIMIT_PER_MIN (e.g. 60) in
# the environment if someone starts abusing the server.
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "0"))
_hits: dict[str, deque] = {}
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # Render sits behind Cloudflare; prefer the header Cloudflare sets, then
    # the first X-Forwarded-For hop, then the socket peer.
    ip = request.headers.get("cf-connecting-ip") or request.headers.get(
        "true-client-ip"
    )
    if not ip:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            ip = xff.split(",")[0]
    if not ip and request.client:
        ip = request.client.host
    return (ip or "unknown").strip()


def _rate_limit(request: Request) -> None:
    if RATE_LIMIT_PER_MIN <= 0:
        return
    ip = _client_ip(request)
    now = time.monotonic()
    with _hits_lock:
        q = _hits.get(ip)
        if q is None:
            if len(_hits) > 10000:  # drop idle IPs so memory stays bounded
                for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                    del _hits[k]
            q = _hits[ip] = deque()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(
                status_code=429, detail="Too many requests, try again shortly"
            )
        q.append(now)


# --- RapidAPI Instagram resolver (permanent, no cookies) ---------------------
# Set RAPIDAPI_KEY in the environment. When present, Instagram links resolve via
# RapidAPI (its own IPs handle Instagram) instead of cookie-based yt-dlp.
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "instagram-reels-downloader-api.p.rapidapi.com"
)
# Instagram: the methods tried, in order, until one works (IG_ORDER).
#   cookies   yt-dlp with IG_COOKIES_B64 (free; skipped when no cookies)
#   mirrors   free public Instagram mirrors (IG_MIRRORS), no key
#   rapidapi  RapidAPI (RAPIDAPI_KEY; skipped when unset or quota used up)
#   ytdlp     yt-dlp without cookies (Instagram usually blocks server IPs)
# Free methods come first so the RapidAPI free quota is only a last resort.
IG_ORDER = tuple(
    m.strip().lower()
    for m in os.environ.get("IG_ORDER", "cookies,mirrors,rapidapi,ytdlp").split(",")
    if m.strip()
)
# When RapidAPI says the quota is used up (429) or the key is rejected
# (401/403), stop calling it for a while and go straight to yt-dlp.
RAPIDAPI_COOLDOWN_S = int(os.environ.get("RAPIDAPI_COOLDOWN_S", "3600"))
_rapidapi_off_until = 0.0


def _resolve_via_rapidapi(url: str) -> dict:
    """Resolve an Instagram link via the RapidAPI reels downloader."""
    global _rapidapi_off_until
    endpoint = (
        f"https://{RAPIDAPI_HOST}/download?url="
        + urllib.parse.quote(url, safe="")
    )
    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY or "",
    }
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        r = c.get(endpoint, headers=headers)
    if r.status_code in (401, 403, 429):
        _rapidapi_off_until = time.monotonic() + RAPIDAPI_COOLDOWN_S
        print(f"rapidapi: HTTP {r.status_code}, pausing for "
              f"{RAPIDAPI_COOLDOWN_S}s")
    if r.status_code != 200:
        raise HTTPException(
            status_code=422, detail=f"RapidAPI HTTP {r.status_code}"
        )
    j = r.json()
    if not j.get("success"):
        raise HTTPException(
            status_code=422, detail=str(j.get("message") or "resolve failed")
        )
    data = j.get("data") or {}
    medias = data.get("medias") or []

    # Prefer a video (not audio-only) mp4 with the highest resolution.
    vids = [m for m in medias if not m.get("is_audio") and m.get("url")]
    if not vids:
        vids = [m for m in medias if m.get("url")]
    if not vids:
        raise HTTPException(status_code=422, detail="No downloadable media")

    def score(m):
        h = m.get("height") or 0
        is_mp4 = 1 if (m.get("extension") == "mp4"
                       or ".mp4" in (m.get("url") or "")) else 0
        return (is_mp4, h)

    best = max(vids, key=score)
    author = data.get("author") or data.get("username")
    if author and not str(author).startswith("@"):
        author = f"@{author}"
    return {
        "downloadUrl": best["url"],
        "thumbnail": data.get("thumbnail"),
        "author": author,
        "title": data.get("title"),
        "duration": int(data.get("duration") or 0),
        "noWatermark": True,
    }

# --- tikwm TikTok resolver (free, no key) ------------------------------------
# TikTok blocks yt-dlp from datacenter IPs (Render included), so TikTok links
# go to tikwm first and yt-dlp is only the backup. TIKWM=0 turns it off.
TIKWM_ENABLED = os.environ.get("TIKWM", "1") != "0"
TIKWM_BASE = os.environ.get("TIKWM_BASE", "https://www.tikwm.com").rstrip("/")


def _resolve_via_tikwm(url: str) -> dict:
    """Resolve a TikTok link via tikwm's public API (no-watermark video)."""
    with httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": BROWSER_UA}
    ) as c:
        r = c.post(f"{TIKWM_BASE}/api/", data={"url": url, "hd": "1"})
    if r.status_code != 200:
        raise HTTPException(status_code=422, detail=f"tikwm HTTP {r.status_code}")
    j = r.json()
    if j.get("code") != 0:
        raise HTTPException(
            status_code=422, detail=f"tikwm: {j.get('msg') or 'failed'}"
        )
    d = j.get("data") or {}

    def absolute(u):
        return f"{TIKWM_BASE}{u}" if isinstance(u, str) and u.startswith("/") else u

    # "play" is the standard no-watermark mp4 (h264, plays everywhere);
    # "hdplay" is the HD one. "wmplay" (watermarked) is never used.
    play = absolute(d.get("play") or d.get("hdplay"))
    if not play:
        raise HTTPException(status_code=422, detail="tikwm: no video in post")
    handle = (d.get("author") or {}).get("unique_id")
    return {
        "downloadUrl": play,
        "thumbnail": absolute(d.get("cover") or d.get("origin_cover")),
        "author": f"@{handle}" if handle else None,
        "title": d.get("title"),
        "duration": int(d.get("duration") or 0),
        "noWatermark": True,
    }


# --- free Instagram mirrors (no key, no cookies) -----------------------------
# Public "embed fixer" sites that serve an Instagram post's video to link
# previews. Free, but best-effort: they don't have every post, and some return
# only a thumbnail, so every result is checked to really be a video.
IG_MIRRORS = tuple(
    h.strip()
    for h in os.environ.get(
        "IG_MIRRORS", "kkinstagram.com,uuinstagram.com,eeinstagram.com"
    ).split(",")
    if h.strip()
)
_PREVIEW_BOT_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"


def _ig_shortcode(url: str) -> str | None:
    m = re.search(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _meta(page: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"',
        page,
    ) or re.search(
        rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(prop)}"',
        page,
    )
    return html.unescape(m.group(1)) if m else None


def _video_url_ok(c: httpx.Client, url: str) -> str | None:
    """Return the final URL if it serves a video, else None."""
    with c.stream(
        "GET", url, headers={"User-Agent": BROWSER_UA, "Range": "bytes=0-0"}
    ) as r:
        ctype = r.headers.get("content-type", "")
        if r.status_code in (200, 206) and ctype.startswith("video/"):
            return str(r.url)
    return None


def _resolve_via_ig_mirrors(url: str) -> dict:
    sc = _ig_shortcode(url)
    if not sc:
        raise HTTPException(status_code=422, detail="mirrors: no shortcode")
    with httpx.Client(timeout=20, follow_redirects=True) as c:
        for host in IG_MIRRORS:
            try:
                r = c.get(
                    f"https://{host}/reel/{sc}/",
                    headers={"User-Agent": _PREVIEW_BOT_UA},
                )
                page = ""
                if r.headers.get("content-type", "").startswith("video/"):
                    video = str(r.url)
                else:
                    page = r.text
                    video = _meta(page, "og:video") or _meta(
                        page, "og:video:secure_url"
                    ) or _meta(page, "og:video:url")
                if not video:
                    continue
                if video.startswith("/"):
                    video = f"https://{host}{video}"
                final = _video_url_ok(c, video)
                if not final:
                    continue
                title = _meta(page, "og:description") or _meta(page, "og:title")
                who = re.search(r"@([A-Za-z0-9._]+)", _meta(page, "og:title") or "")
                return {
                    "downloadUrl": final,
                    "thumbnail": _meta(page, "og:image"),
                    "author": f"@{who.group(1)}" if who else None,
                    "title": title,
                    "duration": 0,
                    "noWatermark": True,
                }
            except httpx.HTTPError as e:
                print(f"mirror {host} failed: {e}")
    raise HTTPException(status_code=422, detail="mirrors: no video found")


# --- short cache of /resolve answers -----------------------------------------
# The same link resolved again within a few minutes (retries, the app asking
# twice) is answered from memory, so it doesn't spend RapidAPI quota twice.
# CDN links expire after a while, so keep this short. 0 disables it.
RESOLVE_CACHE_TTL = int(os.environ.get("RESOLVE_CACHE_TTL", "900"))
_resolve_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_get(url: str) -> dict | None:
    if RESOLVE_CACHE_TTL <= 0:
        return None
    with _cache_lock:
        hit = _resolve_cache.get(url)
        if hit and time.monotonic() - hit[0] < RESOLVE_CACHE_TTL:
            return hit[1]
        _resolve_cache.pop(url, None)
    return None


def _cache_put(url: str, result: dict) -> None:
    if RESOLVE_CACHE_TTL <= 0:
        return
    now = time.monotonic()
    with _cache_lock:
        if len(_resolve_cache) > 2000:
            for k in [k for k, v in _resolve_cache.items()
                      if now - v[0] >= RESOLVE_CACHE_TTL]:
                del _resolve_cache[k]
            if len(_resolve_cache) > 2000:
                _resolve_cache.clear()
        _resolve_cache[url] = (now, result)

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


def _cookie_copy() -> str | None:
    """Per-request copy of the cookie file.

    yt-dlp writes the cookie jar back to its cookiefile when it closes, so
    concurrent requests sharing one file can truncate it. Each request gets
    its own copy and the original is never written to.
    """
    if not _COOKIEFILE:
        return None
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    shutil.copyfile(_COOKIEFILE, path)
    return path


def _ydl_opts(cookiefile: str | None = None) -> dict:
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
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


# Errors that won't go away by retrying (bad/removed/private links).
_PERMANENT_ERRORS = (
    "unsupported url",
    "is not a valid url",
    "http error 404",
    "video unavailable",
    "this video is private",
    "has been removed",
    "no longer available",
    "does not exist",
)


def _is_permanent(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _PERMANENT_ERRORS)


def _extract(url: str, attempts: int = 3) -> dict:
    """Extract the (first) video's info, retrying transient failures.

    Instagram is flaky on datacenter IPs, so network/rate-limit errors are
    retried; permanent errors (removed, private, unsupported) fail fast.
    """
    last_err = None
    for attempt in range(attempts):
        cookiefile = _cookie_copy()
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(cookiefile)) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    video = _first_video(info)
                    # Keep the jar so /download can send the same cookies the
                    # CDN expects (TikTok ties video URLs to them).
                    video["_cookiejar"] = ydl.cookiejar
                    return video
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _is_permanent(e):
                break
        finally:
            if cookiefile:
                try:
                    os.unlink(cookiefile)
                except OSError:
                    pass
        if attempt < attempts - 1:
            time.sleep(1.2 * (attempt + 1))
    print(f"extract failed for {url}: {last_err}")
    raise HTTPException(
        status_code=422,
        detail="Could not get this video. It may be private, removed, "
        "or temporarily unavailable.",
    )


def _is_watermarked(f: dict) -> bool:
    # yt-dlp marks TikTok's watermarked "download" formats with preference -2
    # and a "watermarked" note.
    note = (f.get("format_note") or "").lower()
    return (f.get("preference") or 0) <= -2 or "watermark" in note


def _is_direct(f: dict) -> bool:
    # A plain file URL the app can download (not an HLS/DASH manifest).
    return f.get("protocol") in (None, "http", "https")


def _pick_progressive(info: dict) -> dict | None:
    """Pick the format to hand to the app: one file with video + audio."""
    # 1) The format yt-dlp itself selected (our `format` option). yt-dlp copies
    #    it into `info`, and its ranking already puts watermarked TikTok
    #    formats last.
    if info.get("url") and _is_direct(info) and not _is_watermarked(info):
        return info

    # 2) Otherwise choose ourselves: no watermark, direct file, mp4, tallest.
    cands = [
        f for f in info.get("formats") or []
        if f.get("url")
        and _is_direct(f)
        and f.get("vcodec") != "none"
        and f.get("acodec") != "none"
    ]
    if cands:
        return max(cands, key=lambda f: (
            not _is_watermarked(f),
            f.get("ext") == "mp4",
            f.get("height") or 0,
            f.get("preference") or 0,
        ))

    # 3) Last resort: whatever yt-dlp selected.
    return info if info.get("url") else None


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


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    # HEAD is included so uptime pingers (UptimeRobot etc.) that send HEAD
    # get a 200 instead of 405 — keeps the free instance warm without false
    # "down" alerts.
    return {
        "ok": True,
        "cookies": bool(_COOKIEFILE),
        "rapidapi": bool(RAPIDAPI_KEY),
        "rapidapi_paused": time.monotonic() < _rapidapi_off_until,
        "ig_order": list(IG_ORDER),
        "ig_mirrors": list(IG_MIRRORS),
        "tikwm": TIKWM_ENABLED,
    }


@app.get("/resolve")
def resolve(request: Request, url: str = Query(..., description="Video URL")):
    url, host = _check_url(url)
    _rate_limit(request)

    cached = _cache_get(url)
    if cached:
        return cached

    if TIKWM_ENABLED and _host_matches(host, TIKTOK_HOSTS):
        try:
            result = _resolve_via_tikwm(url)
            _cache_put(url, result)
            return result
        except Exception as e:  # noqa: BLE001
            detail = e.detail if isinstance(e, HTTPException) else e
            print(f"tikwm failed, falling back to yt-dlp: {detail}")

    if _host_matches(host, INSTAGRAM_HOSTS):
        result = _resolve_instagram(url)
    else:
        result = _resolve_via_ytdlp(url)
    _cache_put(url, result)
    return result


def _resolve_instagram(url: str) -> dict:
    """Try each method in IG_ORDER until one returns a video."""
    tried_ytdlp = False
    for method in IG_ORDER:
        try:
            if method == "cookies":
                if not _COOKIEFILE:
                    continue
                tried_ytdlp = True
                return _resolve_via_ytdlp(url, attempts=1)
            if method == "mirrors":
                if IG_MIRRORS:
                    return _resolve_via_ig_mirrors(url)
                continue
            if method == "rapidapi":
                if RAPIDAPI_KEY and time.monotonic() >= _rapidapi_off_until:
                    return _resolve_via_rapidapi(url)
                continue
            if method == "ytdlp":
                if tried_ytdlp:
                    continue  # already tried (with cookies)
                tried_ytdlp = True
                return _resolve_via_ytdlp(url, attempts=1)
        except Exception as e:  # noqa: BLE001
            detail = e.detail if isinstance(e, HTTPException) else e
            print(f"instagram: {method} failed: {detail}")
    raise HTTPException(
        status_code=422,
        detail="Could not get this video. It may be private, removed, "
        "or temporarily unavailable.",
    )


def _resolve_via_ytdlp(url: str, attempts: int = 3) -> dict:
    info = _extract(url, attempts)
    fmt = _pick_progressive(info)
    if not fmt:
        raise HTTPException(status_code=422, detail="No downloadable stream")
    result = {
        "downloadUrl": fmt["url"],
        "thumbnail": info.get("thumbnail"),
        "author": _author(info),
        "title": info.get("title") or info.get("description"),
        "duration": int(info.get("duration") or 0),
        "noWatermark": not _is_watermarked(fmt),
    }
    return result


@app.get("/download")
async def download(request: Request, url: str = Query(...)):
    url, host = _check_url(url)
    _rate_limit(request)

    target = None
    headers = {"User-Agent": BROWSER_UA}
    if TIKWM_ENABLED and _host_matches(host, TIKTOK_HOSTS):
        try:
            target = (await run_in_threadpool(_resolve_via_tikwm, url))[
                "downloadUrl"
            ]
        except Exception as e:  # noqa: BLE001
            detail = e.detail if isinstance(e, HTTPException) else e
            print(f"download: tikwm failed, falling back to yt-dlp: {detail}")
    elif _host_matches(host, INSTAGRAM_HOSTS):
        # Same free-first chain as /resolve.
        target = (await run_in_threadpool(_resolve_instagram, url))["downloadUrl"]

    if target is None:
        # yt-dlp is blocking; run it off the event loop so other requests
        # (including /health) keep being served.
        info = await run_in_threadpool(_extract, url)
        fmt = _pick_progressive(info)
        if not fmt:
            raise HTTPException(status_code=422, detail="No downloadable stream")
        target = fmt["url"]
        # Send the headers + cookies yt-dlp used, or the CDN often answers 403.
        headers.update(fmt.get("http_headers") or {})
        headers.pop("Cookie", None)
        jar = info.get("_cookiejar")
        if jar is not None:
            cookie = jar.get_cookie_header(target)
            if cookie:
                headers["Cookie"] = cookie

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True
    )
    try:
        upstream = await client.send(
            client.build_request("GET", target, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as e:
        await client.aclose()
        print(f"download: upstream request failed: {e}")
        raise HTTPException(
            status_code=502, detail="Could not reach the video server"
        )
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Video server returned HTTP {upstream.status_code}",
        )

    async def _stream():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    out_headers = {}
    length = upstream.headers.get("content-length")
    if length and not upstream.headers.get("content-encoding"):
        out_headers["Content-Length"] = length
    return StreamingResponse(
        _stream(), media_type="video/mp4", headers=out_headers
    )


PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipzio - Privacy Policy</title>
<style>body{font-family:system-ui,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.6;color:#1a1a1a}h1{font-size:26px}h2{font-size:19px;margin-top:28px}small{color:#666}</style>
</head><body>
<h1>Clipzio - Privacy Policy</h1>
<small>Last updated: 27 September 2026</small>

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
<p>To retrieve videos, requests may be processed through our server, the source
content-delivery networks, and third-party video-lookup services (for example, an
API provider we use for Instagram links), which receive only the video link.
We do not share personal information with advertisers.
This version of the app does not display ads.</p>

<h2>Data retention</h2>
<p>We do not maintain user accounts or long-term personal records. Transient
request data is used only to complete your download. Our hosting provider keeps
short-lived technical server logs (such as IP address and the requested link) for
security and troubleshooting.</p>

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
# Each can also be overridden from Render's Environment tab (no code change):
# APP_LATEST_BUILD, APP_MIN_BUILD, APP_UPDATE_URL, APP_UPDATE_MESSAGE.
APP_LATEST_BUILD = int(os.environ.get("APP_LATEST_BUILD", "2"))  # newest versionCode on Play
APP_MIN_BUILD = int(os.environ.get("APP_MIN_BUILD", "1"))  # older builds are force-updated
APP_UPDATE_URL = os.environ.get(
    "APP_UPDATE_URL",
    "https://play.google.com/store/apps/details?id=com.clipzio.clipzio",
)
APP_UPDATE_MESSAGE = os.environ.get(
    "APP_UPDATE_MESSAGE",
    "A new version of Clipzio is available with improvements.",
)


@app.get("/config")
def config():
    return {
        "latest_build": APP_LATEST_BUILD,
        "min_build": APP_MIN_BUILD,
        "url": APP_UPDATE_URL,
        "message": APP_UPDATE_MESSAGE,
    }
