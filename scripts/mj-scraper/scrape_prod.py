"""
Midjourney Explore Feed Scraper — Production Version
====================================================
Pipeline:
  1. Refresh Firebase ID token if expiring soon (saves rotated refresh token).
  2. Launch Playwright (full Chromium via Xvfb), solve CF Turnstile on /explore.
  3. Inject auth cookies (Firebase ID token + refresh token) into the context.
  4. Use page.evaluate(fetch()) with `x-csrf-protection: 1` header to call
     GET https://www.midjourney.com/api/explore?page=N&feed=<feed>&_ql=explore
     for N = 0, 1, 2, ... until empty page or --max-pages reached.
  5. For each item, extract:
       - id (job id)
       - type (image | video)
       - prompt (full prompt object incl. decodedPrompt, params, personalize, styleRef)
       - prompt_text (concatenated decodedPrompt contents — easy full-text search)
       - user (username_v2, display_name, user_id)
       - dimensions (width, height)
       - aspect_ratio (from prompt.ar)
       - job_metadata (job_type, event_type, enqueue_time, parent_grid, parent_id)
       - image_urls (CDN URLs for all 4 grid variants at multiple sizes)
       - video_segments (raw, if type=video)
       - published, items (filter flags)
       - raw (full original item)
       - scraped_at, feed, page (provenance)
  6. Append each item as a JSONL line to:
       /home/z/my-project/download/midjourney_feed.jsonl
       /home/z/my-project/upload/midjourney_feed.jsonl
  7. Skip duplicates (by id) — maintain a seen-ids set across the run.

Usage:
    DISPLAY=:99 python3 scrape_prod.py \\
        [--feeds top] \\
        [--max-pages 50] \\
        [--page-delay 1.5] \\
        [--out /home/z/my-project/download/midjourney_feed.jsonl] \\
        [--timeout 600]
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
from playwright.sync_api import sync_playwright

os.environ.setdefault("DISPLAY", ":99")

SCRIPT_DIR = Path(__file__).parent
AUTH_COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
CF_COOKIES_FILE = SCRIPT_DIR / "cf_cookies.json"
FIREBASE_API_KEY_FILE = SCRIPT_DIR / "firebase_api_key.txt"
FIREBASE_TOKENS_FILE = SCRIPT_DIR / "firebase_tokens.json"

# Use paths relative to the script location (works in any environment: local, GHA, etc.)
# Walk up from scripts/mj-scraper/scrape_prod.py -> repo root -> download/
_PROJECT_ROOT = SCRIPT_DIR.parent.parent
DOWNLOAD_DIR = _PROJECT_ROOT / "download"
UPLOAD_DIR   = _PROJECT_ROOT / "upload"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
IP_BOUND_COOKIES = {"cf_clearance", "__cf_bm", "_cfuvid"}

# MJ image CDN URL pattern: https://cdn.midjourney.com/<job_id>/0_<variant>_<size>_<flag>.<ext>
# Common sizes seen: 384 (thumb). Others we'll generate: 1024, 2048.
# Common flags: N (normal). For original (full quality) we'll also try without size suffix.
CDN_SIZES = [384, 1024, 2048]
CDN_FLAGS = ["N"]


# ---- Timeout handler ----
class TimeoutErr(Exception): pass
def _alarm(signum, frame):
    raise TimeoutErr("script timed out")
# Install signal handler only when run as main (not when imported as library)
_signal_installed = False
def install_timeout_handler():
    global _signal_installed
    if not _signal_installed:
        signal.signal(signal.SIGALRM, _alarm)
        _signal_installed = True


# ---- Token helpers ----
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
    except urllib.error.HTTPError as e:
        print(f"[firebase] HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
    except Exception as e:
        print(f"[firebase] err: {e}", file=sys.stderr)
    return None

def ensure_fresh_id_token():
    """Ensure we have a valid (non-expired) ID token. Refreshes if needed.
    Saves the rotated refresh token + new id_token back to cookies.txt immediately.

    CRITICAL: cookies.txt is the single source of truth for the browser's cookie jar.
    Always sync BOTH the id_token AND refresh_token to cookies.txt, even on the
    early-return path (when the cached token is still valid). Otherwise the
    browser will load a stale id_token from cookies.txt and 401 out."""
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
            # ALWAYS sync both tokens to cookies.txt (single source of truth)
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
                print("[token] Synced cached tokens to cookies.txt", flush=True)
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


# ---- CF solver ----
def solve_cf(page, max_wait=60):
    start = time.time()
    while time.time() - start < max_wait:
        title = page.title()
        if title and "Just a moment" not in title and "Please Wait" not in title:
            return True
        try:
            iframe_el = page.query_selector("iframe[src*='challenges.cloudflare.com']")
            if iframe_el:
                box = iframe_el.bounding_box()
                if box:
                    page.mouse.click(box["x"]+35, box["y"]+box["height"]/2)
                    time.sleep(2)
        except Exception:
            pass
        time.sleep(1)
    return False


# ---- Auth cookies ----
def load_auth_cookies():
    out = []
    for name, value in parse_cookies_txt().items():
        if name in IP_BOUND_COOKIES: continue
        out.append({
            "name": name, "value": value,
            "secure": True, "httpOnly": True, "sameSite": "Lax",
            "url": "https://www.midjourney.com/",
        })
    return out


# ---- Item extraction ----
def build_image_urls(job_id, num_variants=4):
    """Build CDN URLs for all variants of a job at multiple sizes.
    Pattern: https://cdn.midjourney.com/<job_id>/0_<variant>_<size>_<flag>.<ext>
    The SPA loads .webp at size 384 for thumbnails; we include common sizes."""
    urls = []
    for variant in range(num_variants):
        for size in CDN_SIZES:
            for flag in CDN_FLAGS:
                urls.append(f"https://cdn.midjourney.com/{job_id}/0_{variant}_{size}_{flag}.webp")
    return urls


def build_image_urls_primary(job_id, num_variants=4):
    """Just the primary thumbnail URL per variant (matches what SPA loads)."""
    return [f"https://cdn.midjourney.com/{job_id}/0_{v}_384_N.webp" for v in range(num_variants)]


def build_video_url(job_id):
    """Build the video CDN URL.
    Pattern discovered: https://cdn.midjourney.com/video/<job_id>/0.mp4
    (Single file per video job — no variants/sizes.)"""
    return f"https://cdn.midjourney.com/video/{job_id}/0.mp4"


def extract_prompt_text(prompt_obj):
    """Concatenate all decodedPrompt contents into a single string."""
    if not prompt_obj: return ""
    parts = prompt_obj.get("decodedPrompt") or []
    return " ".join(p.get("content", "") for p in parts if p.get("content"))


def extract_aspect_ratio(prompt_obj):
    if not prompt_obj: return None
    ar = prompt_obj.get("ar")
    if ar and isinstance(ar, dict) and ar.get("w") and ar.get("h"):
        return f"{ar['w']}:{ar['h']}"
    return None


def extract_tags(item):
    """Extract any tag-like fields. MJ doesn't have explicit 'tags' but
    personalize codes, styleRef, and depthRef serve a similar purpose.
    Preserves the discriminator `t` field on styleRef/depthRef items."""
    tags = []
    prompt = item.get("prompt") or {}
    for p in (prompt.get("personalize") or []):
        if isinstance(p, dict) and p.get("content"):
            tags.append({"type": "personalize", "content": p["content"], "weight": p.get("weight")})
    for s in (prompt.get("styleRef") or []):
        if isinstance(s, dict) and s.get("content"):
            tags.append({"type": "styleRef", "t": s.get("t"), "content": s["content"], "weight": s.get("weight")})
    for d in (prompt.get("depthRef") or []):
        if isinstance(d, dict) and d.get("content"):
            tags.append({"type": "depthRef", "t": d.get("t"), "content": d["content"], "weight": d.get("weight")})
    return tags


def gcd(a, b):
    while b: a, b = b, a % b
    return a


def normalize_style_item(raw_item, feed, page):
    """Normalize a style item from /api/explore-srefs.
    Style items have a different schema:
    - id: "0_<sref_number>" (not a UUID)
    - formatted_sref: the sref code (e.g. "3925209156") — what users pass to --sref
    - type: "style"
    - prompt: empty string (styles are pure visual references)
    - items: array with 1 entry (not 4 like image grids)
    - display_name: always "Midjourney" (curated)

    NOTE: Style CDN image URLs use a DIFFERENT pattern from regular images.
    The SPA resolves sref → image UUID via an internal mechanism we haven't
    cracked (the UUIDs appear in background-image CSS but not in the API
    response). We save the sref code (the primary identifier) and dimensions
    but leave image_urls empty. To get the actual image, use the Midjourney
    web UI with --sref <code>, or decompile the SPA's JS bundle to find the
    sref→UUID resolution endpoint.
    """
    job_id = raw_item.get("id", "")
    # Extract sref number from id (format: "0_<number>")
    sref = raw_item.get("formatted_sref") or ""
    if not sref and "_" in job_id:
        sref = job_id.split("_", 1)[1]

    # Dimensions
    w = raw_item.get("width")
    h = raw_item.get("height")
    aspect_ratio = None
    if w and h:
        g = gcd(w, h)
        aspect_ratio = f"{w//g}:{h//g}"

    return {
        "id": job_id,
        "type": "style",
        "sref": sref,
        "formatted_sref": sref,
        "prompt": raw_item.get("prompt", ""),
        "prompt_text": "",
        "aspect_ratio": aspect_ratio,
        "tags": [],
        "user": {
            "username": raw_item.get("username_v2"),
            "display_name": raw_item.get("display_name"),
            "user_id": raw_item.get("user_id"),
        },
        "dimensions": {"width": w, "height": h},
        "job_metadata": {
            "job_type": raw_item.get("job_type"),
            "event_type": raw_item.get("event_type"),
            "enqueue_time": raw_item.get("enqueue_time"),
            "enqueue_time_iso": None,
            "parent_grid": raw_item.get("parent_grid"),
            "parent_id": raw_item.get("parent_id"),
            "published": raw_item.get("published"),
            "isStyleJob": raw_item.get("isStyleJob"),
            "liked_by_user": raw_item.get("liked_by_user"),
            "selected": raw_item.get("selected"),
        },
        "video_metadata": None,
        # Style image URLs are UNKNOWN — the SPA resolves sref→UUID via an
        # internal mechanism not visible in the API response. Left empty.
        "image_urls": [],
        "image_urls_all_sizes": [],
        "image_urls_original": [],
        "video_urls": [],
        "video_thumbnail_url": None,
        "items": raw_item.get("items") or [],
        "owner_profile": raw_item.get("owner_profile"),
        "raw": raw_item,
        "feed": feed,
        "page": page,
        "scraped_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    }


def normalize_item(raw_item, feed, page):
    """Convert raw API item into our normalized schema."""
    prompt = raw_item.get("prompt") or {}
    job_id = raw_item.get("id")
    items_arr = raw_item.get("items") or []
    num_variants = len(items_arr) if items_arr else 4
    item_type = raw_item.get("type")  # "image" or "video"

    # Build CDN URLs based on type. Verified URL patterns (see verify_urls_dom.py):
    #   Image:  https://cdn.midjourney.com/<job_id>/0_<variant>_<size>_N.webp  (sizes 384/1024/2048)
    #           Also: <job_id>/0_0.png and <job_id>/0_0.webp (original-resolution)
    #   Video:  https://cdn.midjourney.com/video/<job_id>/0.mp4  (single file, ~5.2s, 24fps)
    #           Video thumbnails come from the PARENT image (use parent_id), NOT the video job_id.
    image_urls = []
    image_urls_all_sizes = []
    image_urls_original = []
    video_urls = []
    video_thumbnail_url = None
    if job_id:
        if item_type == "video":
            # Videos: single .mp4 file per job.
            # Both 0.mp4 and 0_0.mp4 work (verified); we use the canonical 0.mp4.
            video_urls = [build_video_url(job_id)]
            # Video thumbnail = parent image's variant thumbnail (if parent_id is known)
            parent_id = raw_item.get("parent_id")
            parent_grid = raw_item.get("parent_grid")
            if parent_id:
                # parent_grid is the variant index of the parent image (0-3 typically)
                variant = parent_grid if isinstance(parent_grid, int) else 0
                video_thumbnail_url = f"https://cdn.midjourney.com/{parent_id}/0_{variant}_384_N.webp"
                image_urls = [video_thumbnail_url]
                image_urls_all_sizes = [f"https://cdn.midjourney.com/{parent_id}/0_{variant}_{s}_N.webp" for s in CDN_SIZES]
                image_urls_original = [
                    f"https://cdn.midjourney.com/{parent_id}/0_{variant}.png",
                    f"https://cdn.midjourney.com/{parent_id}/0_{variant}.webp",
                ]
        else:
            # Images: 4 variants × multiple sizes + original-resolution png/webp
            image_urls = build_image_urls_primary(job_id, num_variants)
            image_urls_all_sizes = build_image_urls(job_id, num_variants)
            image_urls_original = []
            for v in range(num_variants):
                image_urls_original.append(f"https://cdn.midjourney.com/{job_id}/0_{v}.png")
                image_urls_original.append(f"https://cdn.midjourney.com/{job_id}/0_{v}.webp")

    # video_segments is a list of frame counts at 24fps (verified: [125] = 5.2s)
    video_segments = raw_item.get("video_segments")
    video_duration_seconds = None
    if isinstance(video_segments, list) and video_segments:
        try:
            total_frames = sum(int(v) for v in video_segments if isinstance(v, (int, float)))
            video_duration_seconds = round(total_frames / 24.0, 3)
        except Exception:
            pass

    # Aspect ratio: prefer prompt.ar; fall back to gcd-reduced width/height
    aspect_ratio = extract_aspect_ratio(prompt)
    if not aspect_ratio:
        w = raw_item.get("width") or 0
        h = raw_item.get("height") or 0
        if w and h:
            g = gcd(w, h)
            aspect_ratio = f"{w//g}:{h//g}"

    # Safe enqueue_time conversion
    enqueue_time = raw_item.get("enqueue_time")
    enqueue_time_iso = None
    if isinstance(enqueue_time, (int, float)):
        try:
            enqueue_time_iso = datetime.datetime.fromtimestamp(enqueue_time/1000, tz=datetime.timezone.utc).isoformat()
        except Exception:
            pass

    return {
        "id": job_id,
        "type": item_type,  # "image" or "video"
        "prompt": prompt,  # full prompt object
        "prompt_text": extract_prompt_text(prompt),
        "aspect_ratio": aspect_ratio,
        "tags": extract_tags(raw_item),
        "user": {
            "username": raw_item.get("username_v2"),
            "display_name": raw_item.get("display_name"),
            "user_id": raw_item.get("user_id"),
        },
        "dimensions": {
            "width": raw_item.get("width"),
            "height": raw_item.get("height"),
        },
        "job_metadata": {
            "job_type": raw_item.get("job_type"),
            "event_type": raw_item.get("event_type"),
            "enqueue_time": enqueue_time,
            "enqueue_time_iso": enqueue_time_iso,
            "parent_grid": raw_item.get("parent_grid"),
            "parent_id": raw_item.get("parent_id"),
            "published": raw_item.get("published"),
        },
        "video_metadata": {
            "motion": prompt.get("motion") if item_type == "video" else None,
            "length": prompt.get("length") if item_type == "video" else None,
            "end": prompt.get("end") if item_type == "video" else None,
            "video_segments": video_segments,
            "duration_seconds": video_duration_seconds,
            "fps": 24,
        } if item_type == "video" else None,
        "image_urls": image_urls,                  # primary thumbnails (one per variant) OR video poster
        "image_urls_all_sizes": image_urls_all_sizes,  # all sizes × variants × flags (384/1024/2048 × N)
        "image_urls_original": image_urls_original,    # original-resolution .png + .webp per variant
        "video_urls": video_urls,                   # video .mp4 URLs (empty for image items)
        "video_thumbnail_url": video_thumbnail_url,  # parent image's thumbnail for video items
        "items": items_arr,                          # raw per-variant filter flags
        "owner_profile": raw_item.get("owner_profile"),
        "raw": raw_item,                             # full original
        "feed": feed,
        "page": page,
        "scraped_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    }


# ---- Feed endpoint mapping ----
# Most feeds use /api/explore, but styles use /api/explore-srefs.
# Map feed name -> (endpoint_path, item_kind).
FEED_ENDPOINTS = {
    # Image feeds (all use /api/explore)
    "top":           ("/api/explore", "image"),
    "top_week":      ("/api/explore", "image"),
    "top_month":     ("/api/explore", "image"),
    "top_all":       ("/api/explore", "image"),
    # Video feeds (use /api/explore)
    "video_top":     ("/api/explore", "video"),
    "video_top_week":("/api/explore", "video"),
    "video_top_month":("/api/explore", "video"),
    "video_top_all": ("/api/explore", "video"),
    # Style feeds (use /api/explore-srefs — DIFFERENT endpoint + schema)
    "styles_top":    ("/api/explore-srefs", "style"),
    "styles_top_week":("/api/explore-srefs", "style"),
    "styles_top_month":("/api/explore-srefs", "style"),
    "styles_top_all":("/api/explore-srefs", "style"),
}


def get_feed_endpoint(feed):
    """Return (endpoint_path, item_kind) for a feed name.
    Defaults to /api/explore for unknown feeds."""
    return FEED_ENDPOINTS.get(feed, ("/api/explore", "image"))


# ---- Main scrape ----
def fetch_feed_page(page, feed, page_num):
    """Fetch one page of the feed via the browser's fetch(). Returns parsed JSON or None.
    Distinguishes between CF challenges (403 + html body) and other failures."""
    endpoint, _item_kind = get_feed_endpoint(feed)
    url = f"https://www.midjourney.com{endpoint}?page={page_num}&feed={feed}&_ql=explore"
    js = """
        async (params) => {
            const opts = { method: 'GET', credentials: 'include', headers: params.headers };
            try {
                const r = await fetch(params.url, opts);
                const text = await r.text();
                return { status: r.status, ok: r.ok, body: text };
            } catch (e) {
                return { status: 0, ok: false, body: '', error: e.message };
            }
        }
    """
    result = page.evaluate(js, {"url": url, "headers": {"x-csrf-protection": "1"}})
    status = result.get("status")
    body = result.get("body", "")
    if result.get("ok"):
        try:
            return json.loads(body)
        except Exception as e:
            print(f"  [fetch] JSON parse err: {e}", flush=True)
            return None
    # Categorize the failure
    if status == 401:
        print(f"  [fetch] 401 Unauthorized — JWT may have expired", flush=True)
        return {"_error": "unauthorized"}
    if status == 403 and "Just a moment" in body[:500]:
        print(f"  [fetch] 403 CF challenge — needs re-solve", flush=True)
        return {"_error": "cf_challenge"}
    if status == 429:
        print(f"  [fetch] 429 Too Many Requests — rate-limited", flush=True)
        return {"_error": "rate_limited"}
    print(f"  [fetch] FAIL status={status} err={result.get('error','')} body[:200]={body[:200]!r}", flush=True)
    return None


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
    auth_cookies = load_auth_cookies()
    print(f"[main] {len(auth_cookies)} auth cookies loaded", flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Mirror to upload/ as well (project rules: keep upload tracked)
    upload_path = UPLOAD_DIR / out_path.name

    seen_ids = set()
    # Load existing IDs from output file (to allow resume)
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

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled","--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=UA, viewport={"width":1440,"height":900}, locale="en-US",
            timezone_id="America/Los_Angeles",
            extra_http_headers={"accept-language":"en-US,en;q=0.9"},
        )
        page = ctx.new_page()

        # Attach request/response handlers (matches the working test_headers.py pattern).
        # This may help Playwright route requests the same way as a real browser.
        captured_requests = []
        captured_responses = []
        def on_request(req):
            if req.resource_type in ("xhr", "fetch") and "midjourney.com" in req.url and "/cdn-cgi/" not in req.url:
                captured_requests.append({"url": req.url, "method": req.method, "headers": dict(req.headers)})
        def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch") and "midjourney.com" in resp.url and "/cdn-cgi/" not in resp.url:
                try:
                    body = resp.text()
                except Exception:
                    body = None
                captured_responses.append({"url": resp.url, "status": resp.status, "body_len": len(body) if body else 0})
        page.on("request", on_request)
        page.on("response", on_response)

        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{}}")

        # STEP 1: Visit /explore without auth (CF clears, SPA loads)
        print("\n[main] STEP 1: Visit /explore (no auth) to clear CF...", flush=True)
        try:
            page.goto("https://www.midjourney.com/explore?tab=top", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[main] goto warn: {e}", flush=True)
        if not solve_cf(page, 90):
            print("[main] CF did not clear", file=sys.stderr)
            page.screenshot(path=str(SCRIPT_DIR / "fail_cf.png"))
            browser.close()
            return 1
        # Wait long enough for the SPA to fully render and make its initial XHR
        # calls. The SPA's XHR to /api/explore "primes" the CF session for XHR.
        print("[main] Waiting 12s for SPA to fully load + prime CF for XHR...", flush=True)
        time.sleep(12)
        cf_cookies = ctx.cookies()
        CF_COOKIES_FILE.write_text(json.dumps(cf_cookies, indent=2))
        print(f"[main] CF cleared. {len(cf_cookies)} cookies in jar.", flush=True)
        print(f"[main] Captured {len(captured_requests)} XHR requests during page load", flush=True)
        for cr in captured_requests:
            print(f"  - {cr['method']} {cr['url'][:120]}", flush=True)

        # STEP 2: Warmup fetch — call /api/explore?page=0 WITHOUT auth cookies.
        # Use the EXACT same fetch pattern as test_headers.py (which works).
        print("\n[main] STEP 2: Warmup fetch to /api/explore?page=0 (no auth)...", flush=True)
        warmup = page.evaluate("""
            async (params) => {
                const opts = { method: 'GET', credentials: 'include', headers: params.headers };
                try {
                    const r = await fetch(params.url, opts);
                    const text = await r.text();
                    return { status: r.status, ok: r.ok, bodyLen: text.length, bodyStart: text.substring(0,200) };
                } catch (e) { return { error: e.message }; }
            }
        """, {"url": "https://www.midjourney.com/api/explore?page=0&feed=top&_ql=explore",
              "headers": {"x-csrf-protection": "1"}})
        print(f"[main] Warmup result: {warmup}", flush=True)
        if not warmup.get("ok"):
            print("[main] Warmup failed — CF may still be challenging XHR. Continuing anyway...", file=sys.stderr)
        time.sleep(2)

        # STEP 3: NOW inject auth cookies. Subsequent fetches will include them.
        print("\n[main] STEP 3: Injecting auth cookies...", flush=True)
        ctx.add_cookies(auth_cookies)
        time.sleep(2)

        # STEP 3: For each feed, paginate
        feeds = [f.strip() for f in args.feeds.split(",") if f.strip()]
        print(f"\n[main] STEP 3: Scraping feeds={feeds} max_pages={args.max_pages}", flush=True)

        # Open output files in append mode
        with open(out_path, "a") as fout, open(upload_path, "a") as fup:
            for feed in feeds:
                print(f"\n[main] === Feed: {feed} ===", flush=True)
                consecutive_failures = 0
                page_num = 0
                while page_num < args.max_pages:
                    print(f"[main] {feed} page={page_num}...", flush=True)
                    # Retry each page up to 3 times with exponential backoff
                    data = None
                    for attempt in range(3):
                        data = fetch_feed_page(page, feed, page_num)
                        # Success or non-retryable: break
                        if isinstance(data, list):
                            break
                        if isinstance(data, dict) and data.get("_error"):
                            err = data["_error"]
                            if err == "unauthorized":
                                # JWT expired mid-scrape — refresh + re-inject cookies
                                print(f"  [retry {attempt+1}/3] Refreshing JWT and re-injecting cookies...", flush=True)
                                tokens = ensure_fresh_id_token()
                                # Re-load auth cookies (now with new id_token) and add to context
                                new_auth = load_auth_cookies()
                                for c in new_auth:
                                    c.pop("url", None)
                                # Clear existing auth cookies and re-add (overwrite by name)
                                ctx.clear_cookies()
                                # Re-add CF cookies (still valid)
                                cf_cookies = json.loads(CF_COOKIES_FILE.read_text()) if CF_COOKIES_FILE.exists() else []
                                cf_keep = [c for c in cf_cookies if c.get("name") in IP_BOUND_COOKIES]
                                ctx.add_cookies(cf_keep + new_auth)
                                time.sleep(2)
                            elif err == "cf_challenge":
                                # CF re-challenged — re-solve + warmup
                                print(f"  [retry {attempt+1}/3] Re-solving CF challenge...", flush=True)
                                try:
                                    page.goto("https://www.midjourney.com/explore?tab=top", wait_until="domcontentloaded", timeout=60000)
                                except Exception:
                                    pass
                                if solve_cf(page, 60):
                                    time.sleep(8)
                                    # Warmup fetch
                                    page.evaluate("""
                                        async () => {
                                            const r = await fetch('https://www.midjourney.com/api/explore?page=0&feed=top&_ql=explore', {
                                                method: 'GET', credentials: 'include', headers: { 'x-csrf-protection': '1' }
                                            });
                                            return r.status;
                                        }
                                    """)
                                    time.sleep(2)
                            elif err == "rate_limited":
                                print(f"  [retry {attempt+1}/3] Rate-limited — sleeping 30s...", flush=True)
                                time.sleep(30)
                            else:
                                break
                        else:
                            # data is None — unknown failure
                            pass
                        # Backoff before next attempt
                        time.sleep(args.page_delay * (attempt + 1) * 2)

                    if not isinstance(data, list):
                        consecutive_failures += 1
                        if consecutive_failures >= 2:
                            print(f"[main] Two consecutive page failures — stopping feed={feed}", flush=True)
                            break
                        page_num += 1  # skip to next page
                        continue

                    if len(data) == 0:
                        print(f"[main] Empty page — stopping feed={feed}", flush=True)
                        break

                    consecutive_failures = 0
                    new_in_page = 0
                    # Pick the right normalizer based on feed type
                    _endpoint, item_kind = get_feed_endpoint(feed)
                    normalizer = normalize_style_item if item_kind == "style" else normalize_item
                    for raw_item in data:
                        item_id = raw_item.get("id")
                        if not item_id:
                            continue
                        if item_id in seen_ids:
                            total_skipped += 1
                            continue
                        seen_ids.add(item_id)
                        normalized = normalizer(raw_item, feed=feed, page=page_num)
                        line = json.dumps(normalized, ensure_ascii=False)
                        fout.write(line + "\n")
                        fup.write(line + "\n")
                        fout.flush()
                        fup.flush()
                        new_in_page += 1
                        total_new += 1

                    print(f"  +{new_in_page} new items (skipped {len(data)-new_in_page} dupes); total_new={total_new}", flush=True)
                    page_num += 1
                    time.sleep(args.page_delay)

        # Final screenshot
        page.screenshot(path=str(SCRIPT_DIR / "scrape_prod_final.png"))
        browser.close()

    print(f"\n[main] DONE. total_new={total_new} total_skipped={total_skipped}", flush=True)
    print(f"[main] Output: {out_path}", flush=True)
    print(f"[main] Mirror: {upload_path}", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="top,top_week,top_month,top_all,video_top,styles_top,styles_top_week,styles_top_month,styles_top_all",
                    help="Comma-separated feed names. Image feeds: top, top_week, top_month, top_all. Video: video_top. Styles: styles_top, styles_top_week, styles_top_month, styles_top_all.")
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--page-delay", type=float, default=1.5, help="Delay between pages (seconds)")
    ap.add_argument("--out", default=str(DOWNLOAD_DIR / "midjourney_feed.jsonl"))
    ap.add_argument("--timeout", type=int, default=600, help="Hard timeout (seconds)")
    args = ap.parse_args()
    return run(args)

if __name__ == "__main__":
    sys.exit(main())
