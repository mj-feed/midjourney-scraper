"""
Cache thumbnails by downloading them via the Supabase proxy (with retry + region rotation)
and embedding as base64 data URIs in the catalog.

INCREMENTAL: Only fetches thumbnails for items that don't already have one.
Loads existing catalog_with_thumbs.json (or shards) and skips already-cached items.

Output:
  - download/catalog/catalog_with_thumbs.json — catalog with inline base64 thumbnails

Usage:
    python3 cache_thumbnails.py [--max-items 0] [--concurrency 10]
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
    """Fetch an image via the Supabase proxy with region rotation + retry."""
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
                if r.status == 200 and (resp[:4] == b"RIFF" or resp[:2] == b"\xff\xd8" or resp[:8] == b"\x89PNG\r\n\x1a\n"):
                    return resp
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass
        time.sleep(0.3 + attempt * 0.5)
    return None


def fetch_thumbnail_for_item(item):
    """Fetch the thumbnail for an item. Returns base64 data URI or None."""
    if item["type"] == "image" and item.get("image_urls"):
        thumb_url = item["image_urls"][0]
    elif item["type"] == "video" and item.get("video_thumbnail_url"):
        thumb_url = item["video_thumbnail_url"]
    elif item["type"] == "style":
        return None
    else:
        return None

    img_bytes = fetch_image_via_proxy(thumb_url)
    if img_bytes:
        if img_bytes[:4] == b"RIFF":
            ct = "image/webp"
        elif img_bytes[:2] == b"\xff\xd8":
            ct = "image/jpeg"
        elif img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            ct = "image/png"
        else:
            ct = "image/webp"
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f"data:{ct};base64,{b64}"
    return None


def load_existing_thumbnails():
    """Load existing thumbnails from catalog_with_thumbs.json or sharded files.
    Returns dict: item_id -> thumbnail_data_uri"""
    existing = {}

    # Try sharded files first
    manifest_path = CATALOG_DIR / "thumbs_index.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for shard_info in manifest.get("shards", []):
            shard_path = CATALOG_DIR / shard_info["file"]
            if shard_path.exists():
                items = json.loads(shard_path.read_text())
                for it in items:
                    if it.get("thumbnail_data_uri"):
                        existing[it["id"]] = it["thumbnail_data_uri"]
        print(f"  Loaded {len(existing)} existing thumbnails from {len(manifest.get('shards',[]))} shards")

    # Also try monolithic file
    mono_path = CATALOG_DIR / "catalog_with_thumbs.json"
    if mono_path.exists():
        items = json.loads(mono_path.read_text())
        for it in items:
            if it.get("thumbnail_data_uri") and it["id"] not in existing:
                existing[it["id"]] = it["thumbnail_data_uri"]
        print(f"  Loaded {len(existing)} total existing thumbnails (after monolithic)")

    return existing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-items", type=int, default=0, help="Max items to cache (0 = all)")
    ap.add_argument("--concurrency", type=int, default=10, help="Parallel fetches")
    args = ap.parse_args()

    # Load enriched catalog (source of truth for items)
    catalog_path = CATALOG_DIR / "catalog_enriched.json"
    if not catalog_path.exists():
        catalog_path = CATALOG_DIR / "catalog.json"
    print(f"Loading catalog from {catalog_path.name}...")
    items = json.loads(catalog_path.read_text())
    print(f"  {len(items)} items loaded")

    # Load existing thumbnails (INCREMENTAL — skip already-cached items)
    print(f"\nLoading existing thumbnails (incremental mode)...")
    existing_thumbs = load_existing_thumbnails()

    # Apply existing thumbnails to items
    for it in items:
        if it["id"] in existing_thumbs:
            it["thumbnail_data_uri"] = existing_thumbs[it["id"]]

    # Find items that need thumbnails fetched
    fetchable = [it for it in items if
                 (it["type"] == "image" and it.get("image_urls")) or
                 (it["type"] == "video" and it.get("video_thumbnail_url"))]
    need_fetch = [it for it in fetchable if not it.get("thumbnail_data_uri")]
    already_cached = len(fetchable) - len(need_fetch)

    print(f"  {len(fetchable)} items have thumbnails to cache")
    print(f"  {already_cached} already cached (skipping)")
    print(f"  {len(need_fetch)} need fetching")

    if args.max_items > 0:
        need_fetch = need_fetch[:args.max_items]
        print(f"  Limiting to {len(need_fetch)} items (--max-items)")

    if not need_fetch:
        print(f"\nAll thumbnails already cached — nothing to do!")

    # Fetch thumbnails in parallel
    if need_fetch:
        print(f"\nFetching {len(need_fetch)} thumbnails via Supabase proxy (concurrency={args.concurrency})...")
        start = time.time()
        cached = 0
        failed = 0

        thumb_map = {}

        def fetch_one(item):
            return item["id"], fetch_thumbnail_for_item(item)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {executor.submit(fetch_one, it): it for it in need_fetch}
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
                    eta = (len(need_fetch) - i - 1) / rate if rate > 0 else 0
                    print(f"  [{i+1}/{len(need_fetch)}] cached={cached} failed={failed} ({rate:.1f}/s, ETA {eta:.0f}s)", flush=True)

        elapsed = time.time() - start
        print(f"\nDone: {cached} cached, {failed} failed ({cached*100//(cached+failed) if cached+failed else 0}% success, {elapsed:.0f}s)")

        # Apply newly fetched thumbnails
        for it in items:
            if it["id"] in thumb_map:
                it["thumbnail_data_uri"] = thumb_map[it["id"]]

    # Save catalog with thumbnails
    out_path = CATALOG_DIR / "catalog_with_thumbs.json"
    out_path.write_text(json.dumps(items, ensure_ascii=False))
    print(f"\nSaved {out_path} ({out_path.stat().st_size:,} bytes)")

    # Stats
    total_cached = sum(1 for it in items if it.get("thumbnail_data_uri"))
    total_size = sum(len(it.get("thumbnail_data_uri","")) for it in items)
    print(f"  Total thumbnails: {total_cached}/{len(fetchable)} ({total_cached*100//len(fetchable) if fetchable else 0}%)")
    print(f"  Total thumbnail data: {total_size:,} bytes ({total_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
