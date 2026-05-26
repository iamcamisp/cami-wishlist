#!/bin/bash
# Weekly wishlist price refresh + push to GitHub Pages.
set -euo pipefail

cd "$(dirname "$0")"

LOG="update_prices.log"
echo "=== $(date -Iseconds) ===" >> "$LOG"

python3 update_prices.py >> "$LOG" 2>&1 || {
  echo "update_prices.py failed — see $LOG" >> "$LOG"
  exit 1
}

if git diff --quiet items.json; then
  echo "no price changes" >> "$LOG"
  exit 0
fi

git add items.json
git commit -m "weekly price refresh $(date +%Y-%m-%d)" >> "$LOG" 2>&1
git push >> "$LOG" 2>&1
echo "pushed" >> "$LOG"
