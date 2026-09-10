# Midjourney Explore Feed Scraper

Daily scraper for the Midjourney Explore feed (images, videos, styles). Runs via GitHub Actions on a public repo (unlimited minutes) and pushes scraped data to the private [`midjourney-data`](https://github.com/mj-feed/midjourney-data) repo.

## How it works

1. **GHA workflow** (`.github/workflows/daily-scrape.yml`) runs daily at 09:00 UTC
2. Launches headless Chromium via Playwright + Xvfb to bypass Cloudflare Turnstile
3. Refreshes the Firebase JWT using the stored refresh token (auto-rotates)
4. Calls `GET /api/explore?page=N&feed=<feed>&_ql=explore` with `x-csrf-protection: 1` header
5. Scrapes all feeds: top, top_week, top_month, top_all, video_top, styles_top, styles_top_week, styles_top_month
6. Builds catalog files (catalog.json, facets.json, stats.json) + standalone browser HTML
7. Pushes everything to the `midjourney-data` repo

## Frontend browser

The standalone browser is built daily and pushed to `midjourney-data/download/browser_standalone.html`. It's a single-file HTML with all data inlined — open it directly in any browser, no server needed.

## Secrets (set in this repo)

- `MJ_FIREBASE_REFRESH_TOKEN` — Firebase refresh token (long-lived, rotates on each use)
- `MJ_FIREBASE_API_KEY` — Firebase Web API key (`AIzaSy...`)
- `DATA_REPO_PAT` — PAT with `repo` scope on the `mj-feed/midjourney-data` repo

## Manual trigger

Go to Actions → Daily Midjourney Scrape → Run workflow.
