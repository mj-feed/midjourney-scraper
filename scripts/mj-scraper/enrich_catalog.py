"""
Enrich the catalog with derived semantic metadata that the MJ API doesn't provide directly.

The MJ API returns opaque identifiers:
- personalize codes (e.g. 'ptsoywo') — 7-char random strings, NO semantic name
- sref codes (e.g. '2468219636') — 10-digit numbers, NO description
- style items — prompt is always '' (empty), NO text description

This script DERIVES useful metadata from the fields we DO have:
1. Prompt parameters: extract --ar, --chaos, --stylize, --weird, --v, --hd, etc. as a
   human-readable "params" string (e.g. "--ar 3:4 --chaos 100 --v 8.2")
2. Style reference types: classify styleRef as "seed" (numeric sref) vs "url" (s.mj.run link)
3. Prompt complexity score: based on prompt length, tag count, parameter count
4. Has-personalize / has-sref / has-image-ref boolean flags for easy filtering
5. Derived "prompt_summary": first 80 chars of prompt + param string for quick scanning

Usage:
    python3 enrich_catalog.py
"""
import json
from pathlib import Path
from collections import Counter

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DOWNLOAD_DIR = PROJECT_ROOT / "download"
CATALOG_DIR = DOWNLOAD_DIR / "catalog"


def derive_params_string(prompt):
    """Extract MJ parameters from the prompt object into a human-readable string."""
    if not prompt or not isinstance(prompt, dict):
        return ""
    parts = []
    # Aspect ratio
    ar = prompt.get("ar")
    if ar and isinstance(ar, dict) and ar.get("w") and ar.get("h"):
        parts.append(f"--ar {ar['w']}:{ar['h']}")
    # Version
    v = prompt.get("version")
    if v:
        parts.append(f"--v {v}")
    # Chaos
    chaos = prompt.get("chaos")
    if chaos is not None and chaos != 0:
        parts.append(f"--chaos {chaos}")
    # Stylize
    stylize = prompt.get("stylize")
    if stylize is not None and stylize != 0:
        parts.append(f"--stylize {stylize}")
    # Weird
    weird = prompt.get("weird")
    if weird is not None and weird != 0:
        parts.append(f"--weird {weird}")
    # HD
    if prompt.get("hd"):
        parts.append("--hd")
    # Draft
    if prompt.get("draft"):
        parts.append("--draft")
    # Tile
    if prompt.get("tile"):
        parts.append("--tile")
    # StyleRaw
    if prompt.get("styleRaw"):
        parts.append("--style raw")
    # Video
    if prompt.get("video"):
        parts.append("--video")
    # Motion (videos)
    motion = prompt.get("motion")
    if motion:
        parts.append(f"--motion {motion}")
    return " ".join(parts)


def derive_ref_summary(tags):
    """Summarize the reference tags in a human-readable way."""
    if not tags:
        return ""
    parts = []
    sref_seeds = [t for t in tags if t["type"] == "styleRef" and t.get("t") == "seed"]
    sref_urls = [t for t in tags if t["type"] == "styleRef" and t.get("t") == "url"]
    personalize = [t for t in tags if t["type"] == "personalize"]
    if sref_seeds:
        parts.append(f"{len(sref_seeds)} sref seed" + ("s" if len(sref_seeds) > 1 else ""))
    if sref_urls:
        parts.append(f"{len(sref_urls)} image ref" + ("s" if len(sref_urls) > 1 else ""))
    if personalize:
        parts.append(f"{len(personalize)} personalize profile" + ("s" if len(personalize) > 1 else ""))
    return ", ".join(parts)


def derive_prompt_summary(item):
    """Create a short summary for quick scanning in the browser."""
    prompt_text = item.get("prompt_text") or ""
    params = item.get("derived_params") or ""
    if not prompt_text and item.get("type") == "style":
        return f"--sref {item.get('sref','?')}"
    summary = prompt_text[:80]
    if params:
        summary += f" {params}"
    return summary


def enrich_item(item):
    """Add derived fields to a catalog item."""
    prompt = item.get("prompt") or {}
    tags = item.get("tags") or []

    # Derive params string
    item["derived_params"] = derive_params_string(prompt)

    # Derive reference summary
    item["derived_ref_summary"] = derive_ref_summary(tags)

    # Boolean flags for easy filtering
    item["has_personalize"] = any(t["type"] == "personalize" for t in tags)
    item["has_sref_seed"] = any(t["type"] == "styleRef" and t.get("t") == "seed" for t in tags)
    item["has_sref_url"] = any(t["type"] == "styleRef" and t.get("t") == "url" for t in tags)
    item["has_depth_ref"] = any(t["type"] == "depthRef" for t in tags)

    # Prompt complexity (0-10 scale)
    complexity = 0
    if prompt.get("decodedPrompt"):
        complexity += min(len(prompt["decodedPrompt"]), 3)
    complexity += min(len(tags), 4)
    if item["has_sref_seed"]:
        complexity += 1
    if item["has_sref_url"]:
        complexity += 1
    if prompt.get("chaos") and prompt["chaos"] != 0:
        complexity += 1
    item["derived_complexity"] = min(complexity, 10)

    # Prompt summary
    item["derived_prompt_summary"] = derive_prompt_summary(item)

    return item


def main():
    # Load from the FULL JSONL (has the complete prompt object with all params)
    # Fall back to catalog if JSONL not available
    jsonl_path = DOWNLOAD_DIR / "midjourney_feed.jsonl"
    if jsonl_path.exists():
        print(f"Loading from {jsonl_path.name} (has full prompt objects)...")
        items = []
        with open(jsonl_path) as f:
            for line in f:
                items.append(json.loads(line))
    else:
        catalog_path = CATALOG_DIR / "catalog_with_thumbs.json"
        if not catalog_path.exists():
            catalog_path = CATALOG_DIR / "catalog.json"
        print(f"Loading catalog from {catalog_path.name}...")
        items = json.loads(catalog_path.read_text())
    print(f"  {len(items)} items loaded")

    # Enrich all items
    print("Enriching items with derived metadata...")
    for item in items:
        enrich_item(item)

    # Compute enrichment stats
    print("\n=== ENRICHMENT STATS ===")
    print(f"  Items with --ar param: {sum(1 for it in items if '--ar' in it.get('derived_params',''))}")
    print(f"  Items with personalize: {sum(1 for it in items if it['has_personalize'])}")
    print(f"  Items with sref seed: {sum(1 for it in items if it['has_sref_seed'])}")
    print(f"  Items with sref URL: {sum(1 for it in items if it['has_sref_url'])}")
    print(f"  Items with depth ref: {sum(1 for it in items if it['has_depth_ref'])}")

    # Sample derived fields
    print("\n=== SAMPLE ENRICHED ITEMS ===")
    for it in items[:3]:
        print(f"\n  [{it['type']}] {it.get('prompt_text','')[:60]}...")
        print(f"    derived_params: {it['derived_params']}")
        print(f"    derived_ref_summary: {it['derived_ref_summary']}")
        print(f"    derived_complexity: {it['derived_complexity']}/10")
        print(f"    derived_prompt_summary: {it['derived_prompt_summary']}")

    # Save enriched catalog
    out_path = CATALOG_DIR / "catalog_enriched.json"
    out_path.write_text(json.dumps(items, ensure_ascii=False))
    print(f"\nSaved {out_path} ({out_path.stat().st_size:,} bytes)")

    # Also update facets with new derived fields
    print("\nComputing enriched facets...")
    facets = {}
    # Param-based facets
    ar_counts = Counter()
    for it in items:
        params = it.get("derived_params", "")
        if "--ar" in params:
            # Extract --ar value
            import re
            m = re.search(r"--ar (\d+:\d+)", params)
            if m:
                ar_counts[m.group(1)] += 1
    facets["aspect_ratio_param"] = [{"value": v, "count": c} for v, c in ar_counts.most_common()]

    version_counts = Counter()
    for it in items:
        params = it.get("derived_params", "")
        import re
        m = re.search(r"--v (\S+)", params)
        if m:
            version_counts[m.group(1)] += 1
    facets["version"] = [{"value": v, "count": c} for v, c in version_counts.most_common()]

    # Boolean flag facets
    facets["has_personalize"] = [
        {"value": "yes", "count": sum(1 for it in items if it["has_personalize"])},
        {"value": "no", "count": sum(1 for it in items if not it["has_personalize"])},
    ]
    facets["has_sref"] = [
        {"value": "yes", "count": sum(1 for it in items if it["has_sref_seed"] or it["has_sref_url"])},
        {"value": "no", "count": sum(1 for it in items if not (it["has_sref_seed"] or it["has_sref_url"]))},
    ]

    # Load existing facets and merge
    facets_path = CATALOG_DIR / "facets.json"
    if facets_path.exists():
        existing = json.loads(facets_path.read_text())
        existing.update(facets)
        facets = existing
    facets_path.write_text(json.dumps(facets, ensure_ascii=False, indent=2))
    print(f"Updated {facets_path} with enriched facets")
    print(f"  New facets: aspect_ratio_param ({len(facets['aspect_ratio_param'])} values), "
          f"version ({len(facets['version'])} values), has_personalize, has_sref")


if __name__ == "__main__":
    main()
