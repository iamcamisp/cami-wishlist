#!/usr/bin/env python3
"""Wishlist price updater. Runs weekly via cron.

For each item in items.json, asks Claude with web_search to find the cheapest
current CHF price across Swiss retailers (Galaxus, Microspot, Brack, Digitec,
melectronics, etc.) and updates the file in place. Then `git commit && git push`.
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import anthropic
import requests
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
ITEMS_FILE = ROOT / "items.json"
CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """You are a price-research assistant for a wishlist hosted in Zurich, Switzerland.

For each item the user names, search the web to find the current cheapest price in CHF available to a Swiss buyer, prioritizing Swiss retailers (Galaxus, Digitec, Microspot, Brack, melectronics, Interdiscount, Manor, Coop, Migros, Jelmoli, etc.). Avoid grey-market sellers, marketplaces with unclear shipping to CH, and obviously misleading listings.

For each item, also estimate the typical "average" market price in CHF across reputable Swiss retailers (median of 3-5 listings is fine).

Use the web_search tool to find listings (3-6 searches). Then use the web_fetch tool to open the cheapest retailer's product page and extract the `og:image` meta tag content — that is the product image URL.

Return a single JSON object with these fields exactly:
- name: the item name (echo back)
- best_price_chf: number, cheapest in-stock CHF price you found
- best_url: direct URL to that listing (a real product page on the retailer's site, NOT a search/comparison page when possible)
- best_store: retailer name (e.g. "Galaxus")
- avg_price_chf: number, typical CHF price across reputable listings
- image_url: the `og:image` URL from the product page (use web_fetch to read the page HTML and find the `<meta property="og:image" content="...">` tag — return the exact content URL). If the page has no og:image, return an empty string.

If you cannot find a Swiss retailer carrying the item, use the best EU-shippable listing you found."""

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
OG_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
OG_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE)
TWITTER_IMG_RE = re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)


def fetch_image_url(page_url: str) -> str:
    """Fetch the product page and extract og:image (or twitter:image). Returns '' on failure."""
    try:
        r = requests.get(page_url, headers=BROWSER_HEADERS, timeout=12, allow_redirects=True)
        if r.status_code != 200 or not r.text:
            return ""
        for pattern in (OG_RE, OG_RE_ALT, TWITTER_IMG_RE):
            m = pattern.search(r.text)
            if m:
                img = m.group(1).strip()
                if img.startswith("//"):
                    img = "https:" + img
                elif img.startswith("/"):
                    img = urljoin(page_url, img)
                return img
        return ""
    except Exception:
        return ""


class PriceResult(BaseModel):
    name: str
    best_price_chf: float
    best_url: str
    best_store: str
    avg_price_chf: float
    image_url: str = Field(default="")


def get_client() -> anthropic.Anthropic:
    with open(CREDS_FILE) as f:
        creds = json.load(f)
    token = creds["claudeAiOauth"]["accessToken"]
    # OAuth token → Bearer auth, not x-api-key
    return anthropic.Anthropic(
        auth_token=token,
        default_headers={"anthropic-beta": "oauth-2025-04-20"},
    )


def lookup_item(client: anthropic.Anthropic, name: str) -> PriceResult:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[
            {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]},
            {"type": "web_fetch_20260209", "name": "web_fetch", "allowed_callers": ["direct"]},
        ],
        output_format=PriceResult,
        messages=[
            {
                "role": "user",
                "content": f"Find the current cheapest CHF price for: {name}",
            }
        ],
    )
    return response.parsed_output


def main() -> int:
    with open(ITEMS_FILE) as f:
        data = json.load(f)

    items = data.get("items", [])
    if not items:
        print("No items to update.")
        return 0

    client = get_client()
    today = date.today().isoformat()
    updated = 0
    failed = 0

    for item in items:
        name = item["name"]
        print(f"  [{name}] looking up…")
        try:
            result = lookup_item(client, name)
        except Exception as e:
            print(f"    failed: {e}")
            failed += 1
            continue

        item["best_price_chf"] = result.best_price_chf
        item["best_url"] = result.best_url.strip()
        item["best_store"] = result.best_store
        item["avg_price_chf"] = result.avg_price_chf
        item["last_checked"] = today
        item.pop("notes", None)
        # Prefer Claude's web_fetch-derived og:image; fall back to our own scrape if it failed
        img = result.image_url.strip() or fetch_image_url(result.best_url)
        if img:
            item["image_url"] = img
        else:
            item.pop("image_url", None)
        updated += 1
        print(f"    {result.best_store}: CHF {result.best_price_chf:.2f} (avg {result.avg_price_chf:.2f}){' [img]' if img else ' [no img]'}")

    data["last_updated"] = today
    with open(ITEMS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nDone. Updated: {updated}, failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
