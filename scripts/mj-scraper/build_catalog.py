"""
Build catalog files for the frontend browser from the scraped JSONL data.

Outputs (to <repo_root>/download/catalog/):
  - catalog.json     — lightweight item list (id, type, feed, prompt_text, user, dims, urls, tags, sref)
  - catalog_full.json — full items including raw API response (lazy-loaded by frontend)
  - facets.json      — precomputed facet counts for sidebar filters
  - stats.json       — aggregate statistics for the header

Usage:
    python3 build_catalog.py [--input ../../download/midjourney_feed.jsonl]
"""
import argparse
import json
from pathlib import Path
from collections import Counter, defaultdict

# Use paths relative to the script location (works in any environment)
SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = SCRIPT_DIR.parent.parent
DOWNLOAD_DIR = _PROJECT_ROOT / "download"
CATALOG_DIR = DOWNLOAD_DIR / "catalog"
CATALOG_DIR.mkdir(parents=True, exist_ok=True)

# Fields to include in the lightweight catalog (everything needed for browsing)
LIGHT_FIELDS = {
    "id", "type", "feed", "page", "scraped_at",
    "prompt_text", "aspect_ratio", "tags",
    "user", "dimensions", "job_metadata",
    "image_urls", "image_urls_all_sizes", "image_urls_original",
    "video_urls", "video_thumbnail_url", "video_metadata",
    "sref", "formatted_sref",
}


def load_items(jsonl_path):
    items = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def build_light_item(item):
    """Extract only the fields needed for browsing (drop the heavy `raw` field)."""
    return {k: v for k, v in item.items() if k in LIGHT_FIELDS}


def compute_facets(items):
    """Compute facet counts for the sidebar."""
    facets = {}

    # By type
    facets["type"] = Counter(it["type"] for it in items)

    # By feed
    facets["feed"] = Counter(it["feed"] for it in items)

    # By aspect ratio
    facets["aspect_ratio"] = Counter(it.get("aspect_ratio") or "unknown" for it in items)

    # By user (display_name)
    facets["user"] = Counter(
        (it.get("user") or {}).get("display_name") or "unknown"
        for it in items
    )

    # By job_type
    facets["job_type"] = Counter(
        (it.get("job_metadata") or {}).get("job_type") or "unknown"
        for it in items
    )

    # By event_type
    facets["event_type"] = Counter(
        (it.get("job_metadata") or {}).get("event_type") or "unknown"
        for it in items
    )

    # By tag (personalize codes, styleRef seeds, depthRef)
    tag_counts = Counter()
    for it in items:
        for tag in (it.get("tags") or []):
            tag_key = f"{tag['type']}:{tag.get('content','')}"
            tag_counts[tag_key] += 1
    facets["tag"] = tag_counts

    # By motion (videos only)
    motion_counts = Counter()
    for it in items:
        vm = it.get("video_metadata")
        if vm and vm.get("motion"):
            motion_counts[vm["motion"]] += 1
    facets["motion"] = motion_counts

    # By has_sref (styles)
    facets["has_sref"] = Counter(
        "yes" if it.get("sref") else "no"
        for it in items
    )

    # Convert Counters to sorted lists of {value, count}
    out = {}
    for key, counter in facets.items():
        out[key] = [
            {"value": v, "count": c}
            for v, c in counter.most_common()
        ]
    return out


def compute_stats(items):
    """Compute aggregate statistics."""
    stats = {
        "total_items": len(items),
        "by_type": dict(Counter(it["type"] for it in items)),
        "by_feed": dict(Counter(it["feed"] for it in items)),
        "unique_users": len(set(
            (it.get("user") or {}).get("display_name")
            for it in items
            if (it.get("user") or {}).get("display_name")
        )),
        "unique_tags": len(set(
            f"{t['type']}:{t.get('content','')}"
            for it in items
            for t in (it.get("tags") or [])
        )),
        "videos_with_urls": sum(1 for it in items if it.get("video_urls")),
        "styles_with_sref": sum(1 for it in items if it.get("sref")),
        "scrape_date_range": {
            "earliest": min(it["scraped_at"] for it in items) if items else None,
            "latest": max(it["scraped_at"] for it in items) if items else None,
        },
    }
    # Prompt length stats
    prompt_lens = [len(it.get("prompt_text") or "") for it in items if it.get("prompt_text")]
    if prompt_lens:
        stats["prompt_length"] = {
            "min": min(prompt_lens),
            "max": max(prompt_lens),
            "avg": round(sum(prompt_lens) / len(prompt_lens), 1),
        }
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(DOWNLOAD_DIR / "midjourney_feed.jsonl"))
    args = ap.parse_args()

    print(f"Loading items from {args.input}...")
    items = load_items(args.input)
    print(f"  {len(items)} items loaded")

    print("Building lightweight catalog...")
    light_items = [build_light_item(it) for it in items]
    catalog_path = CATALOG_DIR / "catalog.json"
    catalog_path.write_text(json.dumps(light_items, ensure_ascii=False))
    print(f"  Saved {catalog_path} ({catalog_path.stat().st_size:,} bytes)")

    print("Building full catalog (with raw)...")
    full_path = CATALOG_DIR / "catalog_full.json"
    full_path.write_text(json.dumps(items, ensure_ascii=False))
    print(f"  Saved {full_path} ({full_path.stat().st_size:,} bytes)")

    print("Computing facets...")
    facets = compute_facets(items)
    facets_path = CATALOG_DIR / "facets.json"
    facets_path.write_text(json.dumps(facets, ensure_ascii=False, indent=2))
    print(f"  Saved {facets_path} ({facets_path.stat().st_size:,} bytes)")
    print(f"  Facet keys: {list(facets.keys())}")

    print("Computing stats...")
    stats = compute_stats(items)
    stats_path = CATALOG_DIR / "stats.json"
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"  Saved {stats_path}")
    print(f"  Stats: {stats['total_items']} items, {stats['unique_users']} users, {stats['unique_tags']} tags")

    print("\nDone.")


if __name__ == "__main__":
    main()
