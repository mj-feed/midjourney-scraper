"""
Test CDN URL permanence and access requirements.

Tests:
1. Direct curl (no headers) — does the CDN serve to anonymous clients?
2. curl with Referer header — does CF allow with proper Referer?
3. curl with browser-like User-Agent — does UA matter?
4. curl with full browser headers (UA + Referer + Accept) — what's the minimum?
5. Old job_ids from our first scrape (days ago) — do they still resolve?
6. curl_cffi with Chrome impersonation — does TLS fingerprint matter?

For each test, we check:
- HTTP status (200 = OK, 403 = blocked, 404 = gone)
- Content-Type (should be image/webp)
- Content-Length (should be > 0)
"""
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

JSONL = Path("/home/z/my-project/download/midjourney_feed.jsonl")

# Sample items: oldest scraped + newest scraped + one of each type
items = []
with open(JSONL) as f:
    for line in f:
        items.append(json.loads(line))

# Sort by scraped_at
items.sort(key=lambda x: x.get("scraped_at", ""))
oldest = items[:3]
newest = items[-3:]
# Get one of each type
images = [x for x in items if x["type"] == "image"][:2]
videos = [x for x in items if x["type"] == "video"][:2]
styles = [x for x in items if x["type"] == "style"][:2]

test_items = oldest + newest + images + videos + styles
print(f"Testing {len(test_items)} items (oldest={oldest[0]['scraped_at']}, newest={newest[-1]['scraped_at']})")

# Collect URLs to test
test_urls = []
for item in test_items:
    if item["type"] == "image" and item.get("image_urls"):
        test_urls.append((item["id"], item["type"], item["scraped_at"][:10], item["image_urls"][0]))
    elif item["type"] == "video":
        if item.get("video_urls"):
            test_urls.append((item["id"], item["type"], item["scraped_at"][:10], item["video_urls"][0]))
        if item.get("video_thumbnail_url"):
            test_urls.append((item["id"], "video_thumb", item["scraped_at"][:10], item["video_thumbnail_url"]))
    elif item["type"] == "style":
        # Styles have no image URLs in our data
        pass

print(f"Total URLs to test: {len(test_urls)}")
print()

def curl_test(url, headers=None, label=""):
    """Test a URL with curl. Returns (status, content_type, content_length)."""
    cmd = [
        "curl", "-s", "-o", "/dev/null", "-w",
        "%{http_code}|%{content_type}|%{size_download}",
        "--max-time", "15",
    ]
    if headers:
        for h in headers:
            cmd.extend(["-H", h])
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        parts = r.stdout.split("|")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        return "?", "?", "?"
    except Exception as e:
        return "ERR", str(e)[:50], "0"

# Test 1: Direct curl, no headers
print("=" * 80)
print("TEST 1: Direct curl, NO headers (anonymous)")
print("=" * 80)
results_1 = []
for job_id, typ, date, url in test_urls[:5]:  # Test first 5
    status, ct, cl = curl_test(url)
    print(f"  [{status}] {typ:10s} {date}  cl={cl:>8}  {url[:80]}")
    results_1.append((status, ct, cl))

# Test 2: With Referer header
print()
print("=" * 80)
print("TEST 2: curl + Referer: https://www.midjourney.com/explore")
print("=" * 80)
for job_id, typ, date, url in test_urls[:5]:
    status, ct, cl = curl_test(url, ["Referer: https://www.midjourney.com/explore"])
    print(f"  [{status}] {typ:10s} {date}  cl={cl:>8}  {url[:80]}")

# Test 3: With browser-like User-Agent
print()
print("=" * 80)
print("TEST 3: curl + browser User-Agent + Referer")
print("=" * 80)
browser_headers = [
    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Referer: https://www.midjourney.com/explore",
    "Accept: image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language: en-US,en;q=0.9",
    "Sec-Fetch-Dest: image",
    "Sec-Fetch-Mode: no-cors",
    "Sec-Fetch-Site: cross-site",
]
for job_id, typ, date, url in test_urls[:5]:
    status, ct, cl = curl_test(url, browser_headers)
    print(f"  [{status}] {typ:10s} {date}  cl={cl:>8}  {url[:80]}")

# Test 4: curl_cffi with Chrome impersonation (TLS fingerprint)
print()
print("=" * 80)
print("TEST 4: curl_cffi with Chrome impersonation (TLS fingerprint spoofing)")
print("=" * 80)
try:
    from curl_cffi import requests as cffi_req
    for job_id, typ, date, url in test_urls[:5]:
        try:
            r = cffi_req.get(url, impersonate="chrome", timeout=15,
                             headers={"Referer": "https://www.midjourney.com/explore"})
            print(f"  [{r.status_code}] {typ:10s} {date}  cl={len(r.content):>8}  ct={r.headers.get('content-type','?')}  {url[:60]}")
        except Exception as e:
            print(f"  [ERR] {typ:10s} {date}  {str(e)[:60]}  {url[:60]}")
except ImportError:
    print("  curl_cffi not available — skipping")

# Test 5: All URLs with the best-working method
print()
print("=" * 80)
print("TEST 5: ALL URLs with browser headers (full permanence check)")
print("=" * 80)
ok_count = 0
fail_count = 0
for job_id, typ, date, url in test_urls:
    status, ct, cl = curl_test(url, browser_headers)
    marker = "✓" if status == "200" else "✗"
    print(f"  {marker} [{status}] {typ:12s} {date}  cl={cl:>8}  {url[:70]}")
    if status == "200":
        ok_count += 1
    else:
        fail_count += 1
    time.sleep(0.3)  # be polite

print()
print("=" * 80)
print(f"SUMMARY: {ok_count} OK, {fail_count} FAILED out of {len(test_urls)} URLs")
print("=" * 80)
