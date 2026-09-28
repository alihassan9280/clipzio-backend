# Clipzio resolver backend

A tiny FastAPI service that resolves **Instagram** and **TikTok** links into
direct, no-watermark video URLs using [`yt-dlp`](https://github.com/yt-dlp/yt-dlp).

Point the app at it by setting, in `lib/config/app_config.dart`:

```dart
static const String backendBase = 'https://your-service.onrender.com';
```

When `backendBase` is set, the app calls `GET {backendBase}/resolve?url=<link>`
for **both** platforms (more reliable than the built-in client-side resolvers).

## API

| Route | Purpose |
|---|---|
| `GET /resolve?url=<link>` | Returns `{ downloadUrl, thumbnail, author, title, duration, noWatermark }` |
| `GET /download?url=<link>` | Optional streaming proxy (use only if a direct URL 403s) |
| `GET /health` | Health check (GET or HEAD) |
| `GET /config` | App update popup: `{ latest_build, min_build, url, message }` |
| `GET /privacy` | Privacy policy page (linked from the Play Store listing) |

Only Instagram and TikTok links are accepted (`400 Unsupported link` otherwise).
There is no download limit by default (see `RATE_LIMIT_PER_MIN`).

### Instagram: free methods first
Instagram is tried in this order (`IG_ORDER`) until one works:

1. **cookies**: yt-dlp with `IG_COOKIES_B64` (free, unlimited; skipped if not set)
2. **mirrors**: free public Instagram mirrors, no key (`IG_MIRRORS`); best-effort
3. **rapidapi**: only if `RAPIDAPI_KEY` is set (free plan = small monthly quota)
4. **ytdlp**: without cookies (Instagram usually blocks server IPs)

Instagram blocks logged-out requests from server IPs, so no free server-side
method is 100% reliable. The fully free and permanent option is resolving
Instagram on the phone (the user's own IP), like the app does for TikTok.

## Environment variables (Render → service → Environment)

| Variable | Default | Purpose |
|---|---|---|
| `RAPIDAPI_KEY` | — | Instagram via RapidAPI (no cookies needed) |
| `RAPIDAPI_HOST` | `instagram-reels-downloader-api.p.rapidapi.com` | RapidAPI host |
| `RAPIDAPI_COOLDOWN_S` | `3600` | After RapidAPI returns 401/403/429 (quota used up), skip it for this long |
| `RESOLVE_CACHE_TTL` | `900` | Seconds a `/resolve` answer is reused for the same link (saves quota); `0` = off |
| `IG_COOKIES_B64` | — | Base64 Instagram `cookies.txt` for yt-dlp (free, unlimited IG) |
| `IG_ORDER` | `cookies,mirrors,rapidapi,ytdlp` | Instagram methods, in order. Remove `rapidapi` to never use it |
| `IG_MIRRORS` | `kkinstagram.com,uuinstagram.com,eeinstagram.com` | Free Instagram mirror sites |
| `TIKWM` | `1` | TikTok via tikwm first (TikTok blocks yt-dlp on server IPs); `0` = yt-dlp only |
| `ALLOWED_HOSTS` | `instagram.com,instagr.am,tiktok.com,tiktokv.com` | Sites the server will resolve |
| `RATE_LIMIT_PER_MIN` | `0` (off) | Requests per minute per IP, if you ever need to stop abuse |
| `APP_LATEST_BUILD` / `APP_MIN_BUILD` | `2` / `1` | `/config` update popup / force-update |
| `APP_UPDATE_URL` / `APP_UPDATE_MESSAGE` | Play Store link / default text | `/config` popup |

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Test it:

```bash
curl "http://localhost:8000/resolve?url=https://www.tiktok.com/@user/video/123"
```

To use it from a phone on the **same Wi-Fi**, set `backendBase` to your PC's LAN
IP, e.g. `http://192.168.1.50:8000`.

## Deploy (pick one — all have free tiers)

**Render (easiest, Docker):**
1. Push this repo to GitHub.
2. On [render.com](https://render.com) → New → Web Service → pick the repo.
   `render.yaml` is detected automatically (Docker, free plan, `/health`).
3. Deploy → copy the `https://…onrender.com` URL into `backendBase`.

**Railway / Fly.io:** both read the `Dockerfile` directly — create a service
from the repo and deploy. Fly: `fly launch` then `fly deploy`.

**Any VPS:**
```bash
docker build -t clipzio-resolver .
docker run -d -p 8000:8000 --restart unless-stopped clipzio-resolver
```

## Keeping it working

- `yt-dlp` is updated often to keep up with Instagram/TikTok changes. Redeploy
  periodically so extraction keeps working. The Dockerfile always installs the
  newest `yt-dlp` release, so on Render/Railway a redeploy is enough.
- RapidAPI's free plan is only a few requests per month, so it's used as a
  backup. Keep `IG_COOKIES_B64` fresh: when `/health` stays fine but IG
  downloads start failing, refresh the cookies (steps below).

### Instagram cookies (required for reliable IG on a cloud IP)

Instagram blocks logged-out requests from datacenter IPs (Render, etc.), so IG
resolves fail with "empty media response". Fix it with cookies from a **burner**
Instagram account (don't use your main — automated use can get it limited):

1. In Chrome, log into instagram.com with the burner account.
2. Install the extension **"Get cookies.txt LOCALLY"**.
3. On instagram.com, export → save `cookies.txt`.
4. Base64-encode it (keeps it out of logs) and copy to clipboard:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:USERPROFILE\Downloads\instagram.com_cookies.txt")) | Set-Clipboard
   ```
5. Render dashboard → the service → **Environment** → add variable
   `IG_COOKIES_B64` = (paste) → Save. Render redeploys and IG works.

Cookies expire in a few weeks — repeat when IG starts failing again. `/health`
returns `"cookies": true` once they're loaded. Never commit cookies to the repo.
For scale (many users), use a residential proxy instead of one account's cookies.

## Legal

Only resolve content that users are allowed to download. This service is a tool;
respect the source platforms' Terms of Service and local copyright law.
