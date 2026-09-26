"""
Shard the catalog with thumbnails into multiple files, each well under GitHub's 100MB limit.

Strategy: split items into chunks of N (default 200) items per shard.
Each shard file is ~12MB (200 items × ~60KB thumbnail).
This scales indefinitely — 10,000 items = 50 shards, each still ~12MB.

Outputs:
  - download/catalog/thumbs_000.json  (items 0-199 with thumbnails)
  - download/catalog/thumbs_001.json  (items 200-399 with thumbnails)
  - ...
  - download/catalog/thumbs_index.json  (manifest: shard count, item ranges)

Usage:
    python3 shard_catalog.py [--shard-size 200]
"""
import argparse
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CATALOG_DIR = PROJECT_ROOT / "download" / "catalog"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-size", type=int, default=200,
                    help="Items per shard (default 200, ~12MB each)")
    args = ap.parse_args()

    # Load the enriched catalog (has derived fields, no thumbnails yet)
    enriched_path = CATALOG_DIR / "catalog_enriched.json"
    thumbs_path = CATALOG_DIR / "catalog_with_thumbs.json"

    # Prefer catalog_with_thumbs.json (has thumbnails), fall back to enriched
    source_path = thumbs_path if thumbs_path.exists() else enriched_path
    print(f"Loading from {source_path.name}...")
    items = json.loads(source_path.read_text())
    print(f"  {len(items)} items loaded")

    # Shard the items
    shard_size = args.shard_size
    total_shards = (len(items) + shard_size - 1) // shard_size
    print(f"\nSharding into {total_shards} files ({shard_size} items each)...")

    manifest = {
        "total_items": len(items),
        "shard_size": shard_size,
        "total_shards": total_shards,
        "shards": [],
    }

    for i in range(total_shards):
        start = i * shard_size
        end = min(start + shard_size, len(items))
        shard_items = items[start:end]

        # Estimate size
        shard_json = json.dumps(shard_items, ensure_ascii=False)
        shard_size_bytes = len(shard_json.encode("utf-8"))

        shard_name = f"thumbs_{i:03d}.json"
        shard_path = CATALOG_DIR / shard_name
        shard_path.write_text(shard_json)

        manifest["shards"].append({
            "file": shard_name,
            "index": i,
            "start": start,
            "end": end,
            "count": len(shard_items),
            "size_bytes": shard_size_bytes,
        })

        print(f"  {shard_name}: items {start}-{end-1} ({len(shard_items)} items, {shard_size_bytes/1024/1024:.1f} MB)")

    # Write manifest
    manifest_path = CATALOG_DIR / "thumbs_index.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest: {manifest_path}")
    print(f"  Total items: {manifest['total_items']}")
    print(f"  Total shards: {manifest['total_shards']}")
    print(f"  Max shard size: {max(s['size_bytes'] for s in manifest['shards'])/1024/1024:.1f} MB")

    # Remove the old monolithic catalog_with_thumbs.json (no longer needed)
    if thumbs_path.exists():
        size_mb = thumbs_path.stat().st_size / 1024 / 1024
        print(f"\nRemoving monolithic {thumbs_path.name} ({size_mb:.1f} MB) — replaced by shards")
        thumbs_path.unlink()

    print(f"\nDone. All shards are under 100MB (max: {max(s['size_bytes'] for s in manifest['shards'])/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
