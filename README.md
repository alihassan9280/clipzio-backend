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
| `GET /health` | Health check |

## Run locally

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
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
  periodically (Docker rebuild re-pulls the latest `yt-dlp`) so extraction keeps
  working. On Render/Railway, a redeploy is enough.
- Instagram may rate-limit a shared cloud IP. If you hit that, add cookies:
  export your IG cookies to `cookies.txt`, add `COPY cookies.txt .` to the
  Dockerfile, and set `YDL_OPTS["cookiefile"] = "cookies.txt"` in `main.py`.

## Legal

Only resolve content that users are allowed to download. This service is a tool;
respect the source platforms' Terms of Service and local copyright law.
