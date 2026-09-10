#!/usr/bin/env bash
# Local daily scrape runner — use with cron on any machine.
#
# This is an alternative to the GitHub Actions workflow for cases where
# GHA doesn't work (e.g., Free plan private repos).
#
# Setup:
#   1. Install: Xvfb, Python 3.12+, playwright (pip install playwright; python -m playwright install chromium)
#   2. Set env vars (or put in ~/.bashrc):
#        export MJ_FIREBASE_REFRESH_TOKEN="<your_refresh_token>"
#        export MJ_FIREBASE_API_KEY="AIzaSyAjizp68NsH3JGUS0EyLXsChW4fN0A92tM"
#   3. Set REPO_PATH to your local clone of this repo:
#        export REPO_PATH="/path/to/midjourney-feed-scraper"
#   4. Add to crontab (daily at 9am):
#        0 9 * * * /path/to/midjourney-feed-scraper/scripts/mj-scraper/run_daily.sh >> /tmp/mj-scrape.log 2>&1
#
# Or run manually:
#   ./run_daily.sh

set -euo pipefail

REPO_PATH="${REPO_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRIPT_DIR="$REPO_PATH/scripts/mj-scraper"

echo "=== $(date -u '+%Y-%m-%d %H:%M UTC') — Daily scrape starting ==="
echo "Repo: $REPO_PATH"

# Check env vars
if [ -z "${MJ_FIREBASE_REFRESH_TOKEN:-}" ] || [ -z "${MJ_FIREBASE_API_KEY:-}" ]; then
  echo "ERROR: MJ_FIREBASE_REFRESH_TOKEN and MJ_FIREBASE_API_KEY must be set"
  exit 1
fi

# Write credentials from env
mkdir -p "$SCRIPT_DIR"
cat > "$SCRIPT_DIR/cookies.txt" <<EOF
__Host-Midjourney.AuthUserTokenV3_r=${MJ_FIREBASE_REFRESH_TOKEN}
__Host-Midjourney.AuthUserTokenV3_i=placeholder
EOF
echo "${MJ_FIREBASE_API_KEY}" > "$SCRIPT_DIR/firebase_api_key.txt"

# Start Xvfb (if not already running)
if ! pgrep -f "Xvfb :99" > /dev/null; then
  echo "Starting Xvfb..."
  Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp -nolisten inet6 &
  XVFB_PID=$!
  sleep 2
  trap "kill $XVFB_PID 2>/dev/null || true" EXIT
fi

# Run scraper
echo "Running scraper..."
DISPLAY=:99 python3 "$SCRIPT_DIR/scrape_prod.py" \
  --max-pages 30 \
  --page-delay 1.5 \
  --timeout 1200 \
  --out "$REPO_PATH/download/midjourney_feed.jsonl" \
  2>&1 | tee /tmp/mj-scrape-run.log

# Build catalog + standalone browser
echo "Building catalog..."
python3 "$SCRIPT_DIR/build_catalog.py" --input "$REPO_PATH/download/midjourney_feed.jsonl"
python3 "$SCRIPT_DIR/build_standalone.py"

# Mirror to upload/
mkdir -p "$REPO_PATH/upload"
cp -f "$REPO_PATH/download/midjourney_feed.jsonl" "$REPO_PATH/upload/"

# Git commit + push (if in a git repo)
cd "$REPO_PATH"
if git rev-parse --git-dir > /dev/null 2>&1; then
  git add -A
  if git diff --staged --quiet; then
    echo "No changes to commit"
  else
    COMMIT_MSG="Daily scrape: $(date -u '+%Y-%m-%d %H:%M UTC')"
    git commit -m "$COMMIT_MSG"
    git push
    echo "Pushed: $(git rev-parse HEAD)"
  fi
fi

echo "=== $(date -u '+%Y-%m-%d %H:%M UTC') — Done ==="
