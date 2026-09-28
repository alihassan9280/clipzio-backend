"""
Probe which free (no key, no cookies) Instagram methods work from this IP.

    python tests/ig_probe.py SHORTCODE [SHORTCODE ...]

For each method it reports whether a video URL was found and whether that URL
downloads as a real video (ffprobe).
"""
import html
import json
import re
import subprocess
import sys
import tempfile
import os

import httpx

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
IG_APP_ID = "936619743392459"


def _unescape(s: str) -> str:
    for _ in range(3):
        try:
            s = json.loads(f'"{s}"')
        except Exception:  # noqa: BLE001
            break
    return html.unescape(s.replace("\\/", "/"))


def m_embed(c, sc):
    r = c.get(f"https://www.instagram.com/p/{sc}/embed/captioned/",
              headers={"User-Agent": UA})
    m = re.search(r'video_url\\*"\s*:\s*\\*"(.+?)\\*"', r.text)
    if m:
        return r.status_code, _unescape(m.group(1))
    m = re.search(r'<video[^>]+src="([^"]+)"', r.text)
    return r.status_code, html.unescape(m.group(1)) if m else None


def m_graphql(c, sc):
    lsd = "AVqbxe3J_YA"
    r = c.post(
        "https://www.instagram.com/api/graphql",
        headers={
            "User-Agent": UA, "X-IG-App-ID": IG_APP_ID, "X-FB-LSD": lsd,
            "X-ASBD-ID": "129477", "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "av": "0", "lsd": lsd, "doc_id": "8845758582119845",
            "variables": json.dumps({
                "shortcode": sc, "fetch_tagged_user_count": None,
                "hoisted_comment_id": None, "hoisted_reply_id": None,
            }),
        },
    )
    try:
        media = r.json()["data"]["xdt_shortcode_media"]
        return r.status_code, media.get("video_url")
    except Exception:  # noqa: BLE001
        return r.status_code, None


def m_a1(c, sc):
    r = c.get(f"https://www.instagram.com/p/{sc}/?__a=1&__d=dis",
              headers={"User-Agent": UA, "X-IG-App-ID": IG_APP_ID})
    m = re.search(r'"video_url"\s*:\s*"([^"]+)"', r.text) or re.search(
        r'"video_versions"\s*:\s*\[\s*\{[^}]*"url"\s*:\s*"([^"]+)"', r.text)
    return r.status_code, _unescape(m.group(1)) if m else None


def m_crawler(c, sc):
    r = c.get(f"https://www.instagram.com/reel/{sc}/",
              headers={"User-Agent": "facebookexternalhit/1.1"})
    m = re.search(r'property="og:video(?::secure_url)?" content="([^"]+)"', r.text)
    return r.status_code, html.unescape(m.group(1)) if m else None


def make_fixer(host):
    def m_fixer(c, sc):
        r = c.get(f"https://{host}/reel/{sc}/",
                  headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"})
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("video/"):
            return r.status_code, str(r.url)
        m = re.search(
            r'property="og:video(?::secure_url|:url)?"\s+content="([^"]+)"', r.text
        ) or re.search(r'content="([^"]+)"\s+property="og:video', r.text)
        u = html.unescape(m.group(1)) if m else None
        if u and u.startswith("/"):
            u = f"https://{host}{u}"
        return r.status_code, u
    m_fixer.__name__ = f"fixer:{host}"
    return m_fixer


METHODS = [m_embed, m_graphql, m_a1, m_crawler] + [
    make_fixer(h) for h in (
        "kkinstagram.com", "vxinstagram.com", "instagramez.com",
        "ddinstagram.com", "uuinstagram.com", "eeinstagram.com",
    )
]


def probe_file(c, url):
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        with c.stream("GET", url, headers={"User-Agent": "Dart/3.5 (dart:io)"}) as r:
            if r.status_code != 200:
                return f"HTTP {r.status_code}"
            with open(path, "wb") as f:
                for ch in r.iter_bytes(65536):
                    f.write(ch)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", path],
            capture_output=True, text=True)
        streams = json.loads(out.stdout or "{}").get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not v:
            return "not a video"
        return (f"OK {v.get('codec_name')} {v.get('width')}x{v.get('height')} "
                f"audio={a.get('codec_name') if a else None} "
                f"{os.path.getsize(path)//1024}KB")
    except Exception as e:  # noqa: BLE001
        return f"error {e}"[:60]
    finally:
        os.unlink(path)


def main():
    rows = ["| method | " + " | ".join(sys.argv[1:]) + " |",
            "|---|" + "---|" * len(sys.argv[1:])]
    with httpx.Client(timeout=30, follow_redirects=True) as c:
        for m in METHODS:
            cells = []
            for sc in sys.argv[1:]:
                try:
                    code, url = m(c, sc)
                    cells.append(f"HTTP {code}, " + (probe_file(c, url) if url else "no video url"))
                except Exception as e:  # noqa: BLE001
                    cells.append(f"error {type(e).__name__}: {e}"[:70])
            row = f"| {m.__name__} | " + " | ".join(cells) + " |"
            print(row, flush=True)
            rows.append(row)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Free Instagram methods\n\n" + "\n".join(rows) + "\n")


if __name__ == "__main__":
    main()
