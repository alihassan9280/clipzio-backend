"""
Live smoke test: resolves real TikTok/Instagram links against running servers
and checks that the file the app would download is a real, playable video.

    python tests/smoke_live.py NAME=URL [NAME=URL ...]
    e.g. python tests/smoke_live.py old=http://127.0.0.1:8001 new=http://127.0.0.1:8002

Environment:
    OLD_SRC / NEW_SRC   paths to main.py of each version; used to report which
                        yt-dlp format each version picks (watermark check).
    TIKTOK_LINKS        extra comma-separated TikTok links to test.
    SITES               "tiktok", "instagram" or both (default). Use "tiktok"
                        against production so the RapidAPI quota isn't spent.

Exits non-zero only on a regression: a link that works on "old" but not on
"new", or "new" picking a watermarked TikTok format when a clean one exists.
Sites blocking the CI runner's IP is reported, not failed.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

import httpx

TIKTOK = [
    "https://www.tiktok.com/@leenabhushan/video/6748451240264420610",
    "https://www.tiktok.com/@patroxofficial/video/6742501081818877190",
    "https://www.tiktok.com/@hankgreen1/video/7047596209028074758",
    "https://www.tiktok.com/@tatemcrae/video/7107337212743830830",
]
INSTAGRAM = [
    "https://www.instagram.com/reel/Chunk8-jurw/",
    "https://www.instagram.com/reel/CDUMkliABpa/",
    "https://www.instagram.com/p/BQ0eAlwhDrw/",
]
# What a Flutter app's plain HTTP client sends.
APP_UA = "Dart/3.5 (dart:io)"

report = []
regressions = []


def probe(path: str) -> dict:
    """ffprobe a downloaded file: video/audio streams, size, duration."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return {"valid": False, "why": out.stderr.strip()[:120] or "not media"}
    j = json.loads(out.stdout)
    streams = j.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = j.get("format") or {}
    return {
        "valid": bool(v),
        "video": f"{v.get('codec_name')} {v.get('width')}x{v.get('height')}"
        if v else None,
        "audio": a.get("codec_name") if a else None,
        "duration": round(float(fmt.get("duration") or 0), 1),
        "size_kb": int(int(fmt.get("size") or 0) / 1024),
    }


def fetch_to_file(client: httpx.Client, url: str, **kw) -> tuple[int, str]:
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    with client.stream("GET", url, **kw) as r:
        with open(path, "wb") as f:
            if r.status_code == 200:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
        return r.status_code, path


def load(name: str, src: str):
    spec = importlib.util.spec_from_file_location(f"clipzio_{name}", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def picked_format(mod, url: str) -> str:
    """Which yt-dlp format this version of main.py hands to the app."""
    try:
        info = mod._extract(url)
        info = mod._first_video(info) if "entries" in info else info
        pick = mod._pick_progressive(info)
        if isinstance(pick, dict):
            f = pick
        else:
            f = next((f for f in info.get("formats") or []
                      if f.get("url") == pick), info)
        note = f.get("format_note") or ""
        wm = (f.get("preference") or 0) <= -2 or "watermark" in note.lower()
        has_clean = any(
            (x.get("preference") or 0) > -2
            and "watermark" not in (x.get("format_note") or "").lower()
            and x.get("vcodec") != "none" and x.get("acodec") != "none"
            for x in info.get("formats") or []
        )
        return (f"{f.get('format_id')}"
                f"{' WATERMARKED' if wm else ''}"
                f"{' (clean one existed)' if wm and has_clean else ''}")
    except Exception as e:  # noqa: BLE001
        return f"error: {getattr(e, 'detail', e)}"[:90]


def main() -> int:
    servers = dict(a.split("=", 1) for a in sys.argv[1:])
    mods = {}
    for name in servers:
        src = os.environ.get(f"{name.upper()}_SRC")
        if src:
            mods[name] = load(name, src)

    tiktok = [u.strip() for u in os.environ.get("TIKTOK_LINKS", "").split(",")
              if u.strip()] + TIKTOK
    sites = os.environ.get("SITES", "tiktok,instagram").lower()
    plan = []
    if "tiktok" in sites:
        plan.append(("TikTok", tiktok))
    if "instagram" in sites:
        plan.append(("Instagram", INSTAGRAM))

    client = httpx.Client(timeout=120, follow_redirects=True)
    for site, links in plan:
        for link in links:
            ok_by_server = {}
            for name, base in servers.items():
                row = {"site": site, "link": link, "server": name}
                t = time.time()
                try:
                    r = client.get(f"{base}/resolve", params={"url": link})
                    row["resolve"] = r.status_code
                    row["secs"] = round(time.time() - t, 1)
                    body = r.json()
                except Exception as e:  # noqa: BLE001
                    row["resolve"] = f"error {e}"[:60]
                    body = {}
                if row["resolve"] == 200:
                    row["noWatermark"] = body.get("noWatermark")
                    row["author"] = body.get("author")
                    # Download exactly like the app: the direct URL.
                    try:
                        code, path = fetch_to_file(
                            client, body["downloadUrl"],
                            headers={"User-Agent": APP_UA},
                        )
                        row["direct_http"] = code
                        row["direct_file"] = (
                            probe(path) if code == 200 else None
                        )
                        os.unlink(path)
                    except Exception as e:  # noqa: BLE001
                        row["direct_http"] = f"error {e}"[:60]
                    # And through the server's /download proxy.
                    try:
                        code, path = fetch_to_file(
                            client, f"{base}/download", params={"url": link}
                        )
                        row["proxy_http"] = code
                        row["proxy_file"] = (
                            probe(path) if code == 200 else None
                        )
                        os.unlink(path)
                    except Exception as e:  # noqa: BLE001
                        row["proxy_http"] = f"error {e}"[:60]
                else:
                    row["detail"] = str(body.get("detail"))[:90]
                if name in mods and site == "TikTok":
                    row["format"] = picked_format(mods[name], link)
                ok_by_server[name] = bool(
                    (row.get("direct_file") or {}).get("valid")
                    or (row.get("proxy_file") or {}).get("valid")
                )
                if "clean one existed" in row.get("format", "") and name == "new":
                    regressions.append(f"new picked watermark: {link}")
                report.append(row)
                print(json.dumps(row), flush=True)
            if ok_by_server.get("old") and not ok_by_server.get("new"):
                regressions.append(f"works on old, fails on new: {link}")

    lines = ["| site | link | server | /resolve | direct download (app) "
             "| /download proxy | format picked |", "|---|---|---|---|---|---|---|"]
    for r in report:
        def fmt(http, f):
            if http is None:
                return "-"
            if http != 200:
                return f"HTTP {http}"
            if not f or not f.get("valid"):
                return f"200 but not a video ({(f or {}).get('why', '')})"
            return (f"✅ {f['video']}, audio={f['audio']}, "
                    f"{f['duration']}s, {f['size_kb']} KB")
        res = (f"{r['resolve']} ({r.get('secs')}s)" if r["resolve"] == 200
               else f"{r['resolve']} {r.get('detail', '')}")
        lines.append(
            f"| {r['site']} | {r['link'].split('/')[-1] or r['link'].split('/')[-2]} "
            f"| {r['server']} | {res} "
            f"| {fmt(r.get('direct_http'), r.get('direct_file'))} "
            f"| {fmt(r.get('proxy_http'), r.get('proxy_file'))} "
            f"| {r.get('format', '-')} |"
        )
    table = "\n".join(lines)
    print("\n" + table)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Live smoke test\n\n" + table + "\n")
            if regressions:
                f.write("\n**Regressions:**\n" + "\n".join(
                    f"- {x}" for x in regressions) + "\n")
    if regressions:
        print("\nREGRESSIONS:\n" + "\n".join(regressions))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
