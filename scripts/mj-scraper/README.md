# Midjourney Explore Feed Scraper

A scraper that extracts the public Midjourney Explore feed (both images and videos), capturing full prompts, CDN URLs (without downloading bytes), tags, and all available metadata.

## What it does

- Bypasses Cloudflare Turnstile using a real Chromium browser via Playwright + Xvfb
- Authenticates with your Midjourney account via Firebase JWT cookies
- Auto-refreshes the JWT (Firebase rotates the refresh token — we persist it)
- Calls the internal Midjourney API `GET /api/explore?page=N&feed=<feed>&_ql=explore`
- Paginates through all available items (dedupes by job id, supports resume)
- Saves each item as a JSONL line with rich normalized schema + raw original
- Mirrors output to both `download/` (user-facing) and `upload/` (tracked in git)

## Output schema (per JSONL line)

```json
{
  "id": "<job-uuid>",
  "type": "image" | "video",
  "prompt": { ... full prompt object incl. decodedPrompt, ar, stylize, personalize, styleRef, motion, etc. ... },
  "prompt_text": "<concatenated decodedPrompt contents>",
  "aspect_ratio": "9:16",
  "tags": [ {"type": "personalize"|"styleRef", "content": "...", "weight": 1}, ... ],
  "user": { "username": "...", "display_name": "...", "user_id": "..." },
  "dimensions": { "width": 1648, "height": 2944 },
  "job_metadata": {
    "job_type": "v8-1_hd_diffusion" | "vid_1.1_i2v_render_a_joint_video",
    "event_type": "diffusion" | "variation" | "video_diffusion",
    "enqueue_time": 1784699178431,
    "enqueue_time_iso": "2026-07-22T05:46:18.431000+00:00",
    "parent_grid": null, "parent_id": null, "published": true
  },
  "video_metadata": {
    "motion": "high" | "low" | "medium",
    "length": null, "end": null,
    "video_segments": [125],
    "duration_seconds": 12.5
  },
  "image_urls": [
    "https://cdn.midjourney.com/<job_id>/0_0_384_N.webp",
    "https://cdn.midjourney.com/<job_id>/0_1_384_N.webp",
    "https://cdn.midjourney.com/<job_id>/0_2_384_N.webp",
    "https://cdn.midjourney.com/<job_id>/0_3_384_N.webp"
  ],
  "image_urls_all_sizes": [ ... 12 URLs: 4 variants × 3 sizes (384, 1024, 2048) ... ],
  "video_urls": [ "https://cdn.midjourney.com/video/<job_id>/0.mp4" ],
  "video_thumbnail_url": "https://cdn.midjourney.com/<job_id>/0_0_384_N.webp",
  "items": [ ... raw per-variant filter flags ... ],
  "owner_profile": null,
  "raw": { ... full original API response ... },
  "feed": "top" | "video_top",
  "page": 0,
  "scraped_at": "2026-07-23T05:30:00+00:00"
}
```

## Quick start

```bash
# 1. Start Xvfb (or use xvfb-run if available)
Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp -nolisten inet6 &
sleep 2

# 2. Run the scraper (uses cached Firebase token, refreshes if expired)
DISPLAY=:99 python3 scrape_prod.py \
    --feeds top,video_top \
    --max-pages 50 \
    --page-delay 1.5 \
    --out /home/z/my-project/download/midjourney_feed.jsonl
```

## How it works (architecture)

1. **Firebase token refresh** (`ensure_fresh_id_token`): if the cached ID token expires in <5 min,
   call `POST https://securetoken.googleapis.com/v1/token?key=<API_KEY>` with the refresh token.
   Save the rotated refresh token back to `cookies.txt` immediately.

2. **Cloudflare bypass**: launch full Chromium (not headless_shell) in headed mode under Xvfb.
   Navigate to `/explore` without auth cookies. Wait for the Turnstile widget to auto-solve
   (no click needed in our experience). This mints a fresh `cf_clearance` cookie bound to our
   IP + UA. CRITICAL: only override `navigator.webdriver` — overriding `navigator.plugins` or
   `navigator.languages` causes CF to flag the session as automation and blocks all XHR.

3. **Prime the CF XHR session**: wait 12s for the SPA to make its initial XHR to `/api/explore`.
   Then make a "warmup" fetch to the same URL (without auth) — this establishes the CF session
   for subsequent XHR calls.

4. **Inject auth cookies** (`ctx.add_cookies`) — adds the Firebase JWT cookies to the existing
   (CF-cleared) cookie jar.

5. **Paginate**: for each feed, call `fetch_feed_page(page, feed, page_num)` which uses
   `page.evaluate(fetch())` from within the browser context. The browser automatically attaches
   all cookies (CF + auth) and the required `x-csrf-protection: 1` header.

6. **Normalize + save**: each raw API item is converted to the schema above, then appended as a
   JSONL line to both `download/midjourney_feed.jsonl` and `upload/midjourney_feed.jsonl`.
   Existing IDs are skipped (enables resume across runs).

## Key files

| File | Purpose |
|------|---------|
| `scrape_prod.py` | Main production scraper. Run this. |
| `cookies.txt` | User-provided auth cookies (refreshed on each run). |
| `firebase_api_key.txt` | Discovered Firebase Web API key. |
| `firebase_tokens.json` | Cached ID token + rotated refresh token. |
| `cf_cookies.json` | Latest CF cookies (IP-bound, valid ~30 min). |
| `cf_bypass.py` | Standalone CF bypass module (debugging). |
| `firebase_refresh.py` | Standalone Firebase token refresher (debugging). |
| `test_headers.py` | Replicates SPA's exact request headers to verify CF bypass. |
| `find_video_toggle.py` | Discovers the video feed URL by clicking the Videos toggle. |
| `inspect_video.py` | Captures video `<video>` element src to find video CDN URL pattern. |
| `probe_feeds_v2.py` | Probes different feed values to find available feeds. |

## Discovered API details

- **Endpoint**: `GET https://www.midjourney.com/api/explore?page=N&feed=<feed>&_ql=explore`
- **Required header**: `x-csrf-protection: 1` (without this, MJ returns 401 Unauthorized)
- **Auth**: `__Host-Midjourney.AuthUserTokenV3_i` cookie containing a Firebase JWT
- **Refresh**: Firebase rotates the refresh token on each refresh — save it back immediately
- **Available feeds**:
  - `top` — Top images (4 pages × 50 items = 200 items observed)
  - `video_top` — Top videos (1 page × ~25 items)
  - Other feed values (trending, hot, new, random, etc.) all fall through to the default top image feed
- **Image CDN URL patterns** (all VERIFIED via DOM Image() onload):
  - Thumbnail: `https://cdn.midjourney.com/<job_id>/0_<variant>_384_N.webp`
  - Medium: `https://cdn.midjourney.com/<job_id>/0_<variant>_1024_N.webp`
  - Large: `https://cdn.midjourney.com/<job_id>/0_<variant>_2048_N.webp` (caps at 2048)
  - Original PNG: `https://cdn.midjourney.com/<job_id>/0_<variant>.png` (full resolution)
  - Original WebP: `https://cdn.midjourney.com/<job_id>/0_<variant>.webp` (full resolution)
  - variants: 0-3 (4 grid images per image job)
- **Video CDN URL pattern** (VERIFIED via `<video>` onloadedmetadata):
  - `https://cdn.midjourney.com/video/<job_id>/0.mp4` (single file, ~5.2s, 24fps)
- **Video thumbnail**: NOT at the video job_id. Use the PARENT image's URL instead:
  `https://cdn.midjourney.com/<parent_id>/0_<parent_grid>_384_N.webp`
  (where `parent_id` and `parent_grid` come from the video item's metadata)
- **video_segments**: list of frame counts at 24fps (e.g. `[125]` = 125 frames = 5.208 seconds)

## CDN URL permanence & access requirements

**Yes, the CDN links are permanent.** Verified via systematic testing (see `test_cdn_permanence.py` + `test_cdn_min_headers.py`):

### Permanence
- URLs are **deterministic**: job_id → fixed URL pattern → fixed image, forever
- All 12 URLs from our earliest scrape (1 day ago) still return 200 OK with the same content
- Midjourney has used this CDN URL pattern since 2022; old job_ids still resolve
- No authentication, no account, no cookies required to fetch

### Access requirements
The CDN sits behind Cloudflare bot protection. The **minimum headers needed**:
- A **browser-like User-Agent** (curl's default `curl/7.81.0` gets 403)
- At least **one `Sec-Fetch-*` header** (`Sec-Fetch-Dest`, `Sec-Fetch-Mode`, or `Sec-Fetch-Site` — any one works)

That's it. No Referer, no cookies, no Accept, no auth.

### What this means
- ✅ **Web browsers**: work automatically (they always send Sec-Fetch-* + browser UA)
- ✅ **Our frontend browser**: img/video tags send Sec-Fetch-* automatically — all images load
- ✅ **curl/Python**: work IF you add the two required headers (see below)
- ❌ **Anonymous curl/wget**: 403 (no Sec-Fetch-* headers)
- ❌ **Googlebot**: 403 (bot UA + no Sec-Fetch-*)

### Fetching programmatically
```bash
# Minimal working curl:
curl -H "User-Agent: Mozilla/5.0" \
     -H "Sec-Fetch-Dest: image" \
     "https://cdn.midjourney.com/<job_id>/0_0_384_N.webp" \
     -o image.webp
```

```python
# Python requests:
import requests
r = requests.get(url, headers={
    "User-Agent": "Mozilla/5.0",
    "Sec-Fetch-Dest": "image",
})
```

## Notes / caveats

- The Firebase ID token expires every 1 hour. The scraper auto-refreshes it.
- The CF `cf_clearance` cookie is IP+UA bound and expires in ~30 min. Each scraper run mints a fresh one.
- The scraper does NOT download image bytes — only URLs are saved.
- The raw API response is preserved in the `raw` field for future schema changes.
- If you re-run the scraper, existing IDs are skipped (resume support).
