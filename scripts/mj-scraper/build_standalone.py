"""
Build a self-contained frontend HTML file with catalog data inlined.
This allows the browser to be opened directly as a file (file://) without
a web server, OR served via any static host.

Outputs:
  - download/index.html (standalone — fetches catalog/ files, for served use)
  - download/browser_standalone.html (catalog data inlined, for file:// use)

Usage:
    python3 build_standalone.py
"""
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DOWNLOAD_DIR = SCRIPT_DIR.parent.parent / "download"
TEMPLATE = DOWNLOAD_DIR / "index.html"
STANDALONE = DOWNLOAD_DIR / "browser_standalone.html"
CATALOG = DOWNLOAD_DIR / "catalog" / "catalog.json"
FACETS = DOWNLOAD_DIR / "catalog" / "facets.json"
STATS = DOWNLOAD_DIR / "catalog" / "stats.json"

def main():
    html = TEMPLATE.read_text()
    # Prefer catalog_with_thumbs.json (has inline base64 thumbnails) if it exists
    catalog_with_thumbs = DOWNLOAD_DIR / "catalog" / "catalog_with_thumbs.json"
    catalog_path = catalog_with_thumbs if catalog_with_thumbs.exists() else CATALOG
    catalog = catalog_path.read_text()
    facets = FACETS.read_text()
    stats = STATS.read_text()
    print(f"Using catalog: {catalog_path.name} ({catalog_path.stat().st_size:,} bytes)")

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

    new_load = """// Data inlined by build_standalone.py
const INLINE_CATALOG = __CATALOG__;
const INLINE_FACETS = __FACETS__;
const INLINE_STATS = __STATS__;

async function loadData() {
  try {
    STATE.items = INLINE_CATALOG;
    STATE.facets = INLINE_FACETS;
    STATE.stats = INLINE_STATS;
    document.getElementById('stats').textContent = `${STATE.items.length} items · ${STATE.stats.unique_users || 0} users`;
    renderFacets();
    applyFilters();
  } catch (e) {
    console.error('Failed to load data', e);
    document.getElementById('grid').innerHTML = `<div class="empty-state"><div class="icon">⚠</div><div>Failed to load data</div><div style="margin-top:8px;font-size:12px">${e.message}</div></div>`;
  }
}"""

    # Escape backticks and ${ in JSON for safe embedding in JS template literals
    # Actually we're not using template literals — we just assign the JSON directly
    # JSON is safe to embed as a JS expression (it's a subset of JS object literal syntax
    # except for a few unicode line separators — but our data doesn't have those)
    new_load = new_load.replace("__CATALOG__", catalog)
    new_load = new_load.replace("__FACETS__", facets)
    new_load = new_load.replace("__STATS__", stats)

    standalone_html = html.replace(old_load, new_load)

    # Update the title to indicate standalone
    standalone_html = standalone_html.replace(
        "<title>Midjourney Explore Browser</title>",
        "<title>Midjourney Explore Browser (Standalone)</title>"
    )

    STANDALONE.write_text(standalone_html)
    print(f"Built {STANDALONE}")
    print(f"  Size: {STANDALONE.stat().st_size:,} bytes ({STANDALONE.stat().st_size/1024/1024:.1f} MB)")
    print(f"  Catalog items inlined: {len(json.loads(catalog))}")

if __name__ == "__main__":
    main()
