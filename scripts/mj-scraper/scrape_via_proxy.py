"""
Midjourney Explore Feed Scraper — Proxy Edition
================================================
Uses the Supabase edge proxy (from agent-fetch-kit) to call the MJ API directly.
NO browser needed — no Xvfb, no Playwright, no CF Turnstile solving.

The proxy forwards custom headers (including auth cookies) to the target,
with IP rotation + region selection + JA3 randomization.

Usage:
    python3 scrape_via_proxy.py [--max-pages 30] [--regions us-east-1,eu-west-1]
"""
import argparse
import base64
import datetime
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = SCRIPT_DIR.parent.parent
DOWNLOAD_DIR = _PROJECT_ROOT / "download"
UPLOAD_DIR = _PROJECT_ROOT / "upload"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

AUTH_COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
FIREBASE_API_KEY_FILE = SCRIPT_DIR / "firebase_api_key.txt"
FIREBASE_TOKENS_FILE = SCRIPT_DIR / "firebase_tokens.json"

# Supabase edge proxy (from agent-fetch-kit)
PROXY_URL = "https://pxrpbzmnwtxqpvqgqdqt.supabase.co/functions/v1/proxy"
PROXY_TOKEN = "test-token-1"

IP_BOUND_COOKIES = {"cf_clearance", "__cf_bm", "_cfuvid"}


# ---- Timeout handler ----
class TimeoutErr(Exception): pass
def _alarm(signum, frame):
    raise TimeoutErr("script timed out")
_signal_installed = False
def install_timeout_handler():
    global _signal_installed
    if not _signal_installed:
        signal.signal(signal.SIGALRM, _alarm)
        _signal_installed = True


# ---- Token helpers (same as scrape_prod.py) ----
def parse_cookies_txt():
    out = {}
    if not AUTH_COOKIES_FILE.exists(): return out
    for line in AUTH_COOKIES_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        n, _, v = line.partition("=")
        out[n.strip()] = v.strip()
    return out

def save_cookies_txt(d):
    AUTH_COOKIES_FILE.write_text("\n".join(f"{n}={v}" for n, v in d.items()) + "\n")

def decode_jwt_exp(token):
    parts = token.split(".")
    if len(parts) < 2: return 0
    p = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0

def refresh_firebase_id_token(refresh_token, api_key):
    url = f"https://securetoken.googleapis.com/v1/token?key={api_key}"
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[firebase] err: {e}", file=sys.stderr)
    return None

def ensure_fresh_id_token():
    cookies = parse_cookies_txt()
    rt = cookies.get("__Host-Midjourney.AuthUserTokenV3_r")
    api_key = FIREBASE_API_KEY_FILE.read_text().strip() if FIREBASE_API_KEY_FILE.exists() else None
    if not rt or not api_key:
        print("[token] FATAL: missing refresh token or API key", file=sys.stderr)
        sys.exit(1)
    if FIREBASE_TOKENS_FILE.exists():
        tokens = json.loads(FIREBASE_TOKENS_FILE.read_text())
        id_token = tokens.get("id_token")
        if id_token and decode_jwt_exp(id_token) - time.time() > 300:
            mins_left = int((decode_jwt_exp(id_token) - time.time()) / 60)
            print(f"[token] Existing ID token valid for {mins_left} more min", flush=True)
            new_rt = tokens.get("refresh_token")
            changed = False
            if new_rt and new_rt != rt:
                cookies["__Host-Midjourney.AuthUserTokenV3_r"] = new_rt
                changed = True
            if cookies.get("__Host-Midjourney.AuthUserTokenV3_i") != id_token:
                cookies["__Host-Midjourney.AuthUserTokenV3_i"] = id_token
                changed = True
            if changed:
                save_cookies_txt(cookies)
            return tokens
    print("[token] Refreshing ID token...", flush=True)
    tokens = refresh_firebase_id_token(rt, api_key)
    if not tokens:
        sys.exit(1)
    FIREBASE_TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    new_rt = tokens.get("refresh_token")
    if new_rt and new_rt != rt:
        cookies["__Host-Midjourney.AuthUserTokenV3_r"] = new_rt
    cookies["__Host-Midjourney.AuthUserTokenV3_i"] = tokens["id_token"]
    save_cookies_txt(cookies)
    print("[token] Saved refreshed tokens", flush=True)
    return tokens


# ---- Proxy fetch ----
def proxy_fetch(url, region="us-east-1", extra_headers=None, timeout=30):
    """Fetch a URL via the Supabase edge proxy with custom headers forwarded."""
    body = json.dumps({
        "url": url,
        "mode": "raw",
        "headers": {
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "accept": "application/json",
            "accept-encoding": "identity",  # prevent compression
            **(extra_headers or {}),
        }
    }).encode()
    req = urllib.request.Request(PROXY_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {PROXY_TOKEN}",
        "Content-Type": "application/json",
        "x-region": region,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = r.read()
            text = resp.decode("utf-8", "ignore")
            status = r.status
            edge = r.headers.get("x-sb-edge-region", "?")
            return {"status": status, "body": text, "edge": edge, "ok": status == 200}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        return {"status": e.code, "body": body, "edge": "?", "ok": False}
    except Exception as e:
        return {"status": 0, "body": str(e), "edge": "?", "ok": False}


# ---- Feed endpoint mapping (same as scrape_prod.py) ----
FEED_ENDPOINTS = {
    "top":           ("/api/explore", "image"),
    "top_week":      ("/api/explore", "image"),
    "top_month":     ("/api/explore", "image"),
    "top_all":       ("/api/explore", "image"),
    "video_top":     ("/api/explore", "video"),
    "styles_top":    ("/api/explore-srefs", "style"),
    "styles_top_week":("/api/explore-srefs", "style"),
    "styles_top_month":("/api/explore-srefs", "style"),
}

def get_feed_endpoint(feed):
    return FEED_ENDPOINTS.get(feed, ("/api/explore", "image"))


# ---- Normalize items (from playwright-free normalize module) ----
from normalize import normalize_item, normalize_style_item


# ---- Main scrape ----
def run(args):
    install_timeout_handler()
    signal.alarm(args.timeout)
    try:
        return _run(args)
    except TimeoutErr as e:
        print(f"\n[main] TIMEOUT: {e}", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)

def _run(args):
    tokens = ensure_fresh_id_token()
    id_token = tokens["id_token"]

    # Build auth cookie header
    cookies_dict = parse_cookies_txt()
    auth_cookie_header = "; ".join(
        f"{n}={v}" for n, v in cookies_dict.items() if n not in IP_BOUND_COOKIES
    )
    print(f"[main] Auth cookie header: {len(auth_cookie_header)} chars", flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path = UPLOAD_DIR / out_path.name

    seen_ids = set()
    if out_path.exists():
        with open(out_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("id"):
                        seen_ids.add(rec["id"])
                except Exception:
                    pass
    print(f"[main] {len(seen_ids)} existing IDs loaded from {out_path}", flush=True)

    total_new = 0
    total_skipped = 0
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    feeds = [f.strip() for f in args.feeds.split(",") if f.strip()]

    extra_headers = {
        "x-csrf-protection": "1",
        "cookie": auth_cookie_header,
    }

    with open(out_path, "a") as fout, open(upload_path, "a") as fup:
        for feed in feeds:
            endpoint, item_kind = get_feed_endpoint(feed)
            print(f"\n[main] === Feed: {feed} (endpoint={endpoint}, kind={item_kind}) ===", flush=True)
            normalizer = normalize_style_item if item_kind == "style" else normalize_item
            consecutive_failures = 0
            page_num = 0
            while page_num < args.max_pages:
                # Rotate region per page for diversity (even though feed is globally uniform)
                region = regions[page_num % len(regions)]
                url = f"https://www.midjourney.com{endpoint}?page={page_num}&feed={feed}&_ql=explore"
                print(f"[main] {feed} page={page_num} region={region}...", flush=True)

                result = proxy_fetch(url, region=region, extra_headers=extra_headers)
                if not result["ok"]:
                    print(f"  [fetch] FAIL status={result['status']} body[:200]={result['body'][:200]!r}", flush=True)
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        print(f"[main] Two consecutive failures — stopping feed={feed}", flush=True)
                        break
                    time.sleep(args.page_delay * 2)
                    page_num += 1
                    continue

                try:
                    data = json.loads(result["body"])
                except Exception as e:
                    print(f"  [fetch] JSON parse err: {e}", flush=True)
                    consecutive_failures += 1
                    page_num += 1
                    continue

                if not isinstance(data, list):
                    print(f"[main] Unexpected response type: {type(data).__name__}", flush=True)
                    break
                if len(data) == 0:
                    print(f"[main] Empty page — stopping feed={feed}", flush=True)
                    break

                consecutive_failures = 0
                new_in_page = 0
                for raw_item in data:
                    item_id = raw_item.get("id")
                    if not item_id:
                        continue
                    if item_id in seen_ids:
                        total_skipped += 1
                        continue
                    seen_ids.add(item_id)
                    normalized = normalizer(raw_item, feed=feed, page=page_num)
                    normalized["scrape_region"] = region
                    normalized["scrape_edge"] = result.get("edge", "?")
                    line = json.dumps(normalized, ensure_ascii=False)
                    fout.write(line + "\n")
                    fup.write(line + "\n")
                    fout.flush()
                    fup.flush()
                    new_in_page += 1
                    total_new += 1

                print(f"  +{new_in_page} new items (skipped {len(data)-new_in_page} dupes); total_new={total_new} (edge={result.get('edge')})", flush=True)
                page_num += 1
                time.sleep(args.page_delay)

    print(f"\n[main] DONE. total_new={total_new} total_skipped={total_skipped}", flush=True)
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="top,top_week,top_month,top_all,video_top,styles_top,styles_top_week,styles_top_month")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--page-delay", type=float, default=1.0)
    ap.add_argument("--regions", default="us-east-1,eu-west-1,ap-northeast-1,sa-east-1",
                    help="Comma-separated Supabase proxy regions (rotated per page)")
    ap.add_argument("--out", default=str(DOWNLOAD_DIR / "midjourney_feed.jsonl"))
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    return run(args)

if __name__ == "__main__":
    sys.exit(main())
