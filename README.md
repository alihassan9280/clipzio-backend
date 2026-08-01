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
