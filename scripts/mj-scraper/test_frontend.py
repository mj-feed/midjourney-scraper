"""E2E test of the frontend browser using Playwright.
Starts a local HTTP server, loads the page, and verifies:
  1. Data loads (items + facets + stats)
  2. Grid renders cards
  3. Sidebar facets render
  4. Search filters items
  5. Facet click filters items
  6. Keyboard navigation works (J/K, Enter, Esc)
  7. Command palette opens (Cmd+K)
  8. Drawer opens and shows details
"""
import os, sys, time, json, subprocess, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

os.environ.setdefault("DISPLAY", ":99")

DOWNLOAD_DIR = Path("/home/z/my-project/download")
PORT = 8765
FAILS = []

def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILS.append(name)

def main():
    # Start HTTP server
    server = subprocess.Popen(
        ["python3", "-m", "http.server", str(PORT)],
        cwd=str(DOWNLOAD_DIR),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    print(f"HTTP server started on port {PORT} (PID {server.pid})")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()

            print("\n=== Test 1: Page loads ===")
            page.goto(f"http://localhost:{PORT}/index.html", wait_until="networkidle", timeout=30000)
            time.sleep(2)
            title = page.title()
            check("Page title", "Midjourney" in title, f"title={title!r}")

            print("\n=== Test 2: Data loads ===")
            # Wait for stats to appear
            try:
                page.wait_for_selector("#stats", timeout=10000)
                stats_text = page.text_content("#stats")
                check("Stats shows item count", "items" in stats_text, f"stats={stats_text!r}")
                check("Stats shows user count", "users" in stats_text, f"stats={stats_text!r}")
            except Exception as e:
                check("Stats loads", False, str(e))

            print("\n=== Test 3: Grid renders cards ===")
            try:
                page.wait_for_selector(".card", timeout=10000)
                card_count = page.locator(".card").count()
                check("Cards render", card_count > 0, f"card_count={card_count}")
                check("At least 50 cards", card_count >= 50, f"card_count={card_count}")
            except Exception as e:
                check("Cards render", False, str(e))

            print("\n=== Test 4: Sidebar facets render ===")
            facet_groups = page.locator(".facet-group").count()
            check("Multiple facet groups", facet_groups >= 5, f"groups={facet_groups}")
            facet_items = page.locator(".facet-item").count()
            check("Many facet items", facet_items >= 20, f"items={facet_items}")
            # Check specific facets exist
            type_facets = page.locator('.facet-item[data-group="type"]').count()
            check("Type facets exist", type_facets >= 3, f"type_facets={type_facets}")
            feed_facets = page.locator('.facet-item[data-group="feed"]').count()
            check("Feed facets exist", feed_facets >= 5, f"feed_facets={feed_facets}")

            print("\n=== Test 5: Search filters items ===")
            initial_count = page.locator(".card").count()
            search = page.locator("#search")
            search.click()
            search.fill("portrait")
            time.sleep(1)  # debounce
            filtered_count = page.locator(".card").count()
            check("Search reduces card count", filtered_count < initial_count, f"initial={initial_count} filtered={filtered_count}")
            check("Search returns some results", filtered_count > 0, f"filtered={filtered_count}")
            # Clear search
            search.fill("")
            time.sleep(1)

            print("\n=== Test 6: Facet click filters items ===")
            initial_count = page.locator(".card").count()
            # Click the "video" type facet
            video_facet = page.locator('.facet-item[data-group="type"][data-value="video"]')
            if video_facet.count() > 0:
                video_facet.first.click()
                time.sleep(1)
                video_count = page.locator(".card").count()
                check("Video facet filters", video_count < initial_count, f"initial={initial_count} video={video_count}")
                check("Video facet shows some cards", video_count > 0, f"video={video_count}")
                # Verify all visible cards are videos
                video_tags = page.locator('.type-tag.video').count()
                check("All visible cards are videos", video_tags == video_count, f"video_tags={video_tags} cards={video_count}")
                # Clear filter
                video_facet.first.click()
                time.sleep(1)
            else:
                check("Video facet exists", False, "no video facet found")

            print("\n=== Test 7: Keyboard navigation (J/K) ===")
            # Focus the page body (click on empty area)
            page.locator("main").click()
            time.sleep(0.5)
            # Press J to select first card
            page.keyboard.press("j")
            time.sleep(0.5)
            selected = page.locator(".card.selected").count()
            check("J selects a card", selected == 1, f"selected={selected}")
            # Press J again to move to second card
            page.keyboard.press("j")
            time.sleep(0.5)
            selected = page.locator(".card.selected").count()
            check("J moves selection", selected == 1, f"selected={selected}")

            print("\n=== Test 8: Enter opens drawer ===")
            page.keyboard.press("Enter")
            time.sleep(1)
            drawer_open = page.locator("#drawer.open").count()
            check("Enter opens drawer", drawer_open == 1, f"drawer_open={drawer_open}")
            # Check drawer has content
            drawer_body = page.locator("#drawer-body").text_content()
            check("Drawer has prompt text", "Prompt" in drawer_body, f"drawer_body[:100]={drawer_body[:100]!r}")
            check("Drawer has details", "Details" in drawer_body, f"drawer_body[:100]={drawer_body[:100]!r}")

            print("\n=== Test 9: L/H navigates in drawer ===")
            page.keyboard.press("l")
            time.sleep(1)
            check("L works (drawer stays open)", page.locator("#drawer.open").count() == 1)
            page.keyboard.press("h")
            time.sleep(1)
            check("H works (drawer stays open)", page.locator("#drawer.open").count() == 1)

            print("\n=== Test 10: Esc closes drawer ===")
            page.keyboard.press("Escape")
            time.sleep(1)
            check("Esc closes drawer", page.locator("#drawer.open").count() == 0)

            print("\n=== Test 11: Cmd+K opens command palette ===")
            page.keyboard.press("Control+k")
            time.sleep(1)
            cmdk_open = page.locator("#cmdk-backdrop.open").count()
            check("Cmd+K opens palette", cmdk_open == 1, f"cmdk_open={cmdk_open}")
            # Type a search
            cmdk_input = page.locator("#cmdk-input")
            check("CmdK input focused", cmdk_input.evaluate("el => el === document.activeElement"))
            cmdk_input.fill("top")
            time.sleep(0.5)
            results = page.locator(".cmdk-item").count()
            check("CmdK shows results", results > 0, f"results={results}")
            # Close
            page.keyboard.press("Escape")
            time.sleep(0.5)
            check("Esc closes palette", page.locator("#cmdk-backdrop.open").count() == 0)

            print("\n=== Test 12: ? opens shortcuts help ===")
            page.locator("main").click()
            time.sleep(0.5)
            page.keyboard.press("?")
            time.sleep(1)
            shortcuts_open = page.locator("#shortcuts-backdrop.open").count()
            check("? opens shortcuts", shortcuts_open == 1, f"open={shortcuts_open}")
            # Verify shortcuts content
            shortcuts_text = page.locator("#shortcuts-content").text_content()
            check("Shortcuts shows navigation", "Navigation" in shortcuts_text)
            check("Shortcuts shows J/K", "J" in shortcuts_text and "K" in shortcuts_text)
            page.keyboard.press("Escape")
            time.sleep(0.5)

            print("\n=== Test 13: Density cycling (D) ===")
            initial_class = page.locator("#grid").get_attribute("class")
            page.locator("main").click()
            time.sleep(0.3)
            page.keyboard.press("d")
            time.sleep(0.5)
            new_class = page.locator("#grid").get_attribute("class")
            check("D changes grid density", initial_class != new_class, f"initial={initial_class} new={new_class}")

            print("\n=== Test 14: Type filter shortcuts (1/2/3) ===")
            # Reload page to get clean state
            page.reload(wait_until="networkidle", timeout=30000)
            time.sleep(3)
            # Click on grid to focus
            page.locator("#grid").click()
            time.sleep(0.5)
            # Press 2 — videos only
            page.keyboard.press("2")
            time.sleep(2)
            video_count = page.locator(".card").count()
            video_tags = page.locator('.type-tag.video').count()
            active_type_filters = page.locator('.facet-item[data-group="type"].active').count()
            check("2 filters to videos", video_count > 0 and video_count < 100, f"video_count={video_count} video_tags={video_tags} active_filters={active_type_filters}")
            check("All cards are videos after 2", video_tags == video_count and video_count > 0, f"video_tags={video_tags} cards={video_count}")

            print("\n=== Test 15: Standalone HTML works (file:// protocol) ===")
            page.goto(f"file://{DOWNLOAD_DIR}/browser_standalone.html", wait_until="networkidle", timeout=30000)
            time.sleep(3)
            stats_text = page.text_content("#stats")
            check("Standalone loads data", "items" in stats_text, f"stats={stats_text!r}")
            card_count = page.locator(".card").count()
            check("Standalone renders cards", card_count > 0, f"cards={card_count}")

            browser.close()

    finally:
        server.terminate()
        server.wait()
        print(f"\nHTTP server stopped")

    print(f"\n{'='*50}")
    if FAILS:
        print(f"FAILED: {len(FAILS)} checks failed")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    else:
        print(f"ALL CHECKS PASSED")
        return 0

if __name__ == "__main__":
    sys.exit(main())
