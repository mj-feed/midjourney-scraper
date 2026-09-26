"""
Build a self-contained frontend HTML file with catalog data inlined.
NO thumbnails are inlined — the browser loads them from CDN URLs with retry logic.

This keeps the standalone HTML small (~5MB for 3000+ items) and scales indefinitely.
Thumbnails load on-demand from cdn.midjourney.com with 3-retry exponential backoff.

Outputs:
  - download/browser_standalone.html (catalog data inlined, thumbnails from CDN)

Usage:
    python3 build_standalone.py
"""
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DOWNLOAD_DIR = SCRIPT_DIR.parent.parent / "download"
TEMPLATE = DOWNLOAD_DIR / "index.html"
STANDALONE = DOWNLOAD_DIR / "browser_standalone.html"
CATALOG_DIR = DOWNLOAD_DIR / "catalog"

def main():
    html = TEMPLATE.read_text()

    # Use the enriched catalog (has derived params, ref summary, etc.)
    # Do NOT use catalog_with_thumbs.json — it's too large and gets sharded separately
    catalog_path = CATALOG_DIR / "catalog_enriched.json"
    if not catalog_path.exists():
        catalog_path = CATALOG_DIR / "catalog.json"

    # Strip thumbnail_data_uri fields from the catalog to keep it small
    # (the browser will use CDN URLs with retry instead)
    catalog_raw = json.loads(catalog_path.read_text())
    for item in catalog_raw:
        item.pop("thumbnail_data_uri", None)
    catalog = json.dumps(catalog_raw, ensure_ascii=False)

    facets = (CATALOG_DIR / "facets.json").read_text()
    stats = (CATALOG_DIR / "stats.json").read_text()
    print(f"Using catalog: {catalog_path.name} (stripped thumbnails, {len(catalog):,} bytes)")

    # Replace the loadData() function to use inlined data instead of fetch
    old_load = """async function loadData() {
  try {
    const [catRes, facetsRes, statsRes] = await Promise.all([
      fetch('catalog/catalog.json'),
      fetch('catalog/facets.json'),
      fetch('catalog/stats.json'),
    ]);
    STATE.items = await catRes.json();
    STATE.facets = await facetsRes.json();
    STATE.stats = await statsRes.json();
    document.getElementById('stats').textContent = `${STATE.items.length} items · ${STATE.stats.unique_users || 0} users`;
    renderFacets();
    applyFilters();
  } catch (e) {
    console.error('Failed to load data', e);
    document.getElementById('grid').innerHTML = `<div class="empty-state"><div class="icon">⚠</div><div>Failed to load data</div><div style="margin-top:8px;font-size:12px">${e.message}</div></div>`;
  }
}"""

    new_load = f"""// Data inlined by build_standalone.py (no thumbnails — loaded from CDN with retry)
const INLINE_CATALOG = {catalog};
const INLINE_FACETS = {facets};
const INLINE_STATS = {stats};

async function loadData() {{
  try {{
    STATE.items = INLINE_CATALOG;
    STATE.facets = INLINE_FACETS;
    STATE.stats = INLINE_STATS;
    document.getElementById('stats').textContent = `${{STATE.items.length}} items · ${{STATE.stats.unique_users || 0}} users`;
    renderFacets();
    applyFilters();
  }} catch (e) {{
    console.error('Failed to load data', e);
    document.getElementById('grid').innerHTML = `<div class="empty-state"><div class="icon">⚠</div><div>Failed to load data</div><div style="margin-top:8px;font-size:12px">${{e.message}}</div></div>`;
  }}
}}"""

    standalone_html = html.replace(old_load, new_load)

    # Update the title
    standalone_html = standalone_html.replace(
        "<title>Midjourney Explore Browser</title>",
        "<title>Midjourney Explore Browser (Standalone)</title>"
    )

    STANDALONE.write_text(standalone_html)
    print(f"Built {STANDALONE}")
    print(f"  Size: {STANDALONE.stat().st_size:,} bytes ({STANDALONE.stat().st_size/1024/1024:.1f} MB)")
    print(f"  Catalog items inlined: {len(catalog_raw)} (thumbnails load from CDN)")

if __name__ == "__main__":
    main()
