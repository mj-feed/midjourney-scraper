"""
Cache thumbnails by downloading them via the Supabase proxy (with retry + region rotation)
and embedding as base64 data URIs in the catalog.

This makes the standalone browser work fully offline AND avoids the CF rate-limiting
issue (browser tries to load 1000+ images from one IP → gets 403'd).

Output:
  - download/catalog/catalog_with_thumbs.json — catalog with inline base64 thumbnails
  - download/browser_standalone.html — rebuilt with thumbnails inlined

Usage:
    python3 cache_thumbnails.py [--max-items 200] [--concurrency 4]
"""
import argparse
import base64
import concurrent.futures
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from collections import defaultdict

SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = SCRIPT_DIR.parent.parent
DOWNLOAD_DIR = _PROJECT_ROOT / "download"
CATALOG_DIR = DOWNLOAD_DIR / "catalog"

PROXY = "https://pxrpbzmnwtxqpvqgqdqt.supabase.co/functions/v1/proxy"
TOKEN = "test-token-1"
REGIONS = ["us-east-1", "eu-west-1", "ap-northeast-1", "sa-east-1", "ca-central-1",
           "us-west-2", "eu-central-1", "ap-southeast-1"]
MAX_RETRIES = 5


def fetch_image_via_proxy(url, regions_to_try=None):
    """Fetch an image via the Supabase proxy with region rotation + retry.
    Returns bytes if successful, None if all retries fail.
    Uses POST method (forwards custom headers better) with accept-encoding: identity."""
    regions = regions_to_try or REGIONS
    for attempt in range(MAX_RETRIES):
        region = regions[attempt % len(regions)]
        body = json.dumps({
            "url": url,
            "mode": "raw",
            "headers": {
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
                "accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                "accept-encoding": "identity",
                "referer": "https://www.midjourney.com/explore",
                "sec-fetch-dest": "image",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "cross-site",
            }
        }).encode()
        req = urllib.request.Request(PROXY, data=body, method="POST", headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "x-region": region,
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = r.read()
                # Check if we got an actual image (RIFF header for WEBP, or \xff\xd8 for JPEG, or PNG)
                if r.status == 200 and (resp[:4] == b"RIFF" or resp[:2] == b"\xff\xd8" or resp[:8] == b"\x89PNG\r\n\x1a\n"):
                    return resp
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass
        time.sleep(0.3 + attempt * 0.5)  # increasing backoff
    return None


def fetch_thumbnail_for_item(item):
    """Fetch the thumbnail for an item. Returns base64 data URI or None."""
    # Determine the thumbnail URL
    if item["type"] == "image" and item.get("image_urls"):
        thumb_url = item["image_urls"][0]  # 384px thumbnail
    elif item["type"] == "video" and item.get("video_thumbnail_url"):
        thumb_url = item["video_thumbnail_url"]
    elif item["type"] == "style":
        return None  # Styles don't have CDN URLs we can fetch
    else:
        return None

    img_bytes = fetch_image_via_proxy(thumb_url)
    if img_bytes:
        # Determine content type
        if img_bytes[:4] == b"RIFF":
            ct = "image/webp"
        elif img_bytes[:2] == b"\xff\xd8":
            ct = "image/jpeg"
        elif img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            ct = "image/png"
        else:
            ct = "image/webp"  # default
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f"data:{ct};base64,{b64}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-items", type=int, default=0, help="Max items to cache (0 = all)")
    ap.add_argument("--concurrency", type=int, default=4, help="Parallel fetches")
    args = ap.parse_args()

    # Load catalog
    catalog_path = CATALOG_DIR / "catalog.json"
    print(f"Loading catalog from {catalog_path}...")
    items = json.loads(catalog_path.read_text())
    print(f"  {len(items)} items loaded")

    # Filter to items that have thumbnails to fetch
    fetchable = [it for it in items if
                 (it["type"] == "image" and it.get("image_urls")) or
                 (it["type"] == "video" and it.get("video_thumbnail_url"))]
    print(f"  {len(fetchable)} items have thumbnails to cache ({sum(1 for it in items if it['type']=='style')} styles have no CDN URL)")

    if args.max_items > 0:
        fetchable = fetchable[:args.max_items]
        print(f"  Limiting to {len(fetchable)} items (--max-items)")

    # Fetch thumbnails in parallel
    print(f"\nFetching {len(fetchable)} thumbnails via Supabase proxy (concurrency={args.concurrency})...")
    start = time.time()
    cached = 0
    failed = 0

    # Build a map: item_id -> base64 thumbnail
    thumb_map = {}

    def fetch_one(item):
        return item["id"], fetch_thumbnail_for_item(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(fetch_one, it): it for it in fetchable}
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            item_id, thumb = future.result()
            if thumb:
                thumb_map[item_id] = thumb
                cached += 1
            else:
                failed += 1
            if (i + 1) % 50 == 0:
                elapsed = time.time() - start
                rate = (i + 1) / elapsed
                print(f"  [{i+1}/{len(fetchable)}] cached={cached} failed={failed} ({rate:.1f}/s, {elapsed:.0f}s elapsed)", flush=True)

    elapsed = time.time() - start
    print(f"\nDone: {cached} cached, {failed} failed ({cached*100//(cached+failed)}% success, {elapsed:.0f}s)")

    # Add thumbnails to catalog items
    for it in items:
        if it["id"] in thumb_map:
            it["thumbnail_data_uri"] = thumb_map[it["id"]]

    # Save catalog with thumbnails
    out_path = CATALOG_DIR / "catalog_with_thumbs.json"
    out_path.write_text(json.dumps(items, ensure_ascii=False))
    print(f"\nSaved {out_path} ({out_path.stat().st_size:,} bytes)")

    # Stats
    total_size = sum(len(v) for v in thumb_map.values())
    print(f"  Total thumbnail data: {total_size:,} bytes ({total_size/1024/1024:.1f} MB)")
    print(f"  Avg per thumbnail: {total_size//max(cached,1):,} bytes")


if __name__ == "__main__":
    main()
