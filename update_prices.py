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
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
ITEMS_FILE = ROOT / "items.json"
CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """You are a price-research assistant for a wishlist hosted in Zurich, Switzerland.

For each item the user names, search the web to find the current cheapest in-stock price in CHF available to a Swiss buyer. Consider:
- Swiss retailers: Galaxus, Digitec, Microspot, Brack, melectronics, Interdiscount, Manor, Coop, Migros, Jelmoli, Conrad.ch, Fust, Daniel-shop, etc.
- EU retailers that ship to Switzerland: Amazon.de, Amazon.fr, Amazon.it, Notebooksbilliger.de, MediaMarkt.de, Saturn.de, Cyberport.de, Bax-shop, Thomann, Decathlon, etc.

For EU listings, convert the price to CHF (rough rate: 1 EUR ≈ 0.95 CHF). Avoid grey-market sellers, marketplaces with unclear shipping to CH, and obviously misleading listings.

Use the web_search tool to find listings (3-6 searches). Return a single JSON object with these fields exactly:
- name: the item name (echo back)
- best_price_chf: number, cheapest in-stock CHF-equivalent price you found
- best_url: direct URL to that listing (a real product page on the retailer's site, NOT a search/comparison page when possible)
- best_store: retailer name (e.g. "Galaxus" or "Amazon.de")"""

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

PRICE_PATTERNS = [
    re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?'),
    re.compile(r'itemprop=["\']price["\'][^>]*content=["\'](\d+(?:\.\d{1,2})?)["\']', re.IGNORECASE),
    re.compile(r'content=["\'](\d+(?:\.\d{1,2})?)["\'][^>]*itemprop=["\']price["\']', re.IGNORECASE),
    re.compile(r'property=["\']product:price:amount["\'][^>]*content=["\'](\d+(?:\.\d{1,2})?)["\']', re.IGNORECASE),
]


def _extract_prices(html: str):
    prices = []
    for pat in PRICE_PATTERNS:
        for m in pat.finditer(html):
            try:
                p = float(m.group(1))
                if 0 < p < 100_000:
                    prices.append(p)
            except ValueError:
                pass
    return prices


def _fetch_rendered_html(url: str) -> str:
    """Render the page in a headless browser to bypass scraper blocks. Slow (~3-5s)."""
    sys.path.insert(0, "/Library/Python/3.9/lib/python/site-packages")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    try:
        with sync_playwright() as p:
            # --disable-http2: Galaxus/Digitec choke on HTTP/2 from headless Chrome (TLS fingerprint)
            # --disable-blink-features=AutomationControlled: hide webdriver flag
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-http2", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        print(f"    playwright failed: {e}")
        return ""


def _claude_price_lookup(client: anthropic.Anthropic, page_url: str) -> Optional[float]:
    """Last resort: use Claude's web_fetch (bypasses retailer scraper blocks) to read the live price."""
    host = urlparse(page_url).netloc
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=256,
            tools=[
                {
                    "type": "web_fetch_20260209",
                    "name": "web_fetch",
                    "allowed_callers": ["direct"],
                    "allowed_domains": [host],
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Use web_fetch on this product page: {page_url}\n\n"
                        f"Read the current price in CHF. Reply with ONLY the number — no currency symbol, "
                        f"no prose, no markdown. Example: 294.00. If you cannot find a price, reply NONE."
                    ),
                }
            ],
        )
        for block in response.content:
            if block.type == "text":
                text = block.text.strip()
                m = re.search(r'(\d+(?:\.\d{1,2})?)', text)
                if m:
                    return float(m.group(1))
        return None
    except Exception:
        return None


def verify_price(page_url: str, candidate_price: float, client: Optional[anthropic.Anthropic] = None,
                 tolerance: float = 0.30) -> Tuple[float, str]:
    """Verify price via direct GET → headless browser → Claude web_fetch (if client given).
    Returns (price, source): 'verified' / 'verified-rendered' / 'verified-claude' / 'claude'."""
    html = ""
    source_if_found = "verified"
    try:
        r = requests.get(page_url, headers=BROWSER_HEADERS, timeout=12, allow_redirects=True)
        if r.status_code == 200 and r.text:
            html = r.text
    except Exception:
        pass

    prices = _extract_prices(html) if html else []
    if not prices:
        html = _fetch_rendered_html(page_url)
        prices = _extract_prices(html) if html else []
        if prices:
            source_if_found = "verified-rendered"

    if prices:
        in_range = [p for p in prices if abs(p - candidate_price) / candidate_price <= tolerance]
        if in_range:
            from collections import Counter
            best = Counter(in_range).most_common(1)[0][0]
            return best, source_if_found

    # Final fallback: ask Claude (skip if no client provided to avoid unwanted API calls)
    if client is not None:
        claude_price = _claude_price_lookup(client, page_url)
        if claude_price and abs(claude_price - candidate_price) / candidate_price <= tolerance:
            return claude_price, "verified-claude"

    return candidate_price, "claude"


JUNK_IMG_HINTS = ("favicon", "logo", "sprite", "placeholder", "default")


def is_real_product_image(img_url: str) -> bool:
    low = img_url.lower()
    return not any(hint in low for hint in JUNK_IMG_HINTS)


def og_image_from_page(page_url: str) -> str:
    """Fetch a page and extract og:image (or twitter:image). Returns '' on failure."""
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
                if is_real_product_image(img):
                    return img
        return ""
    except Exception:
        return ""


BING_MURL_RE = re.compile(r'"murl":"([^"]+)"')


def bing_image_search(query: str) -> str:
    """Fall back to Bing image search for a product image. Returns '' on failure."""
    try:
        r = requests.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2", "first": "1"},
            headers=BROWSER_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        for m in BING_MURL_RE.finditer(r.text):
            img = m.group(1).replace("\\", "")
            if img.startswith("http") and not img.endswith(".svg"):
                return img
        return ""
    except Exception:
        return ""


def fetch_image_url(page_url: str, item_name: str, client=None) -> str:
    """Try og:image from retailer page → Toppreise via Claude web_search → Google CSE."""
    img = og_image_from_page(page_url)
    if img:
        return img
    # Fallback: ask Claude to find a Toppreise.ch product URL for this item, then scrape og:image
    if client is not None:
        toppreise_url = _find_toppreise_url(client, item_name)
        if toppreise_url:
            img = og_image_from_page(toppreise_url)
            if img and "imgsrv.toppreise.ch" in img:
                return img
    # Fallback: Google Custom Search (if configured)
    img = google_image_search(item_name)
    if img:
        return img
    return ""


def _find_toppreise_url(client: anthropic.Anthropic, item_name: str) -> str:
    """Use Claude's web_search to find a Toppreise.ch product page URL for this item."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=256,
            tools=[{"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}],
            messages=[{
                "role": "user",
                "content": (
                    f"Search the web for: site:toppreise.ch {item_name}\n\n"
                    f"Reply with ONLY the URL of the first Toppreise.ch product page result "
                    f"(must end with -p<number>). No prose. If no match, reply NONE."
                ),
            }],
        )
        for block in response.content:
            if block.type == "text":
                m = re.search(r'https?://(?:www\.)?toppreise\.ch/[^\s]+-p\d+', block.text)
                if m:
                    return m.group(0)
        return ""
    except Exception:
        return ""


def claude_image_lookup(client: anthropic.Anthropic, item_name: str, page_url: str) -> str:
    """Use Claude's web_fetch to get og:image from a page Python can't access."""
    host = urlparse(page_url).netloc
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            tools=[
                {
                    "type": "web_fetch_20260209",
                    "name": "web_fetch",
                    "allowed_callers": ["direct"],
                    "allowed_domains": [host],
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Use web_fetch on this product page: {page_url}\n\n"
                        f"Find the `<meta property=\"og:image\" content=\"...\">` tag in the HTML and "
                        f"reply with ONLY the exact image URL — nothing else, no prose, no markdown. "
                        f"If there's no og:image tag, reply with the literal string NONE."
                    ),
                }
            ],
        )
        for block in response.content:
            if block.type == "text":
                text = block.text.strip()
                # Take first URL-like token
                m = re.search(r'https?://\S+', text)
                if m:
                    img = m.group(0).rstrip('.,);]"\'')
                    if is_real_product_image(img):
                        return img
        return ""
    except Exception:
        return ""


class PriceResult(BaseModel):
    name: str
    best_price_chf: float
    best_url: str
    best_store: str


def _load_creds() -> dict:
    with open(CREDS_FILE) as f:
        return json.load(f)


def get_client() -> anthropic.Anthropic:
    creds = _load_creds()
    token = creds["claudeAiOauth"]["accessToken"]
    # OAuth token → Bearer auth, not x-api-key
    return anthropic.Anthropic(
        auth_token=token,
        default_headers={"anthropic-beta": "oauth-2025-04-20"},
    )


def _google_cse_config():
    """Return (api_key, cx) tuple if Google Custom Search is configured, else (None, None)."""
    try:
        creds = _load_creds()
        cfg = creds.get("googleCustomSearch", {})
        return cfg.get("apiKey"), cfg.get("searchEngineId")
    except Exception:
        return None, None


def google_image_search(query: str) -> str:
    """Search Google Custom Search for an image URL. Returns '' if not configured or no result."""
    api_key, cx = _google_cse_config()
    if not (api_key and cx):
        return ""
    try:
        r = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": api_key,
                "cx": cx,
                "q": query,
                "searchType": "image",
                "num": 5,
                "safe": "active",
                "imgType": "photo",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        for hit in r.json().get("items", []):
            link = hit.get("link", "")
            if link.startswith("http") and not link.endswith(".svg") and is_real_product_image(link):
                return link
        return ""
    except Exception:
        return ""


def lookup_item(client: anthropic.Anthropic, name: str) -> PriceResult:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[
            {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]},
        ],
        output_format=PriceResult,
        messages=[
            {
                "role": "user",
                "content": f"Find the current cheapest CHF price for: {name}",
            }
        ],
    )
    if response.parsed_output is None:
        raise RuntimeError(f"no parsed output (stop_reason={response.stop_reason})")
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
        if item.get("manual"):
            print(f"  [{name}] manual — skipped")
            continue
        print(f"  [{name}] looking up…")
        try:
            result = lookup_item(client, name)
        except Exception as e:
            print(f"    failed: {e}")
            failed += 1
            continue

        verified_price, price_source = verify_price(result.best_url.strip(), result.best_price_chf, client=client)
        item["best_price_chf"] = verified_price
        item["best_url"] = result.best_url.strip()
        item["best_store"] = result.best_store
        item["last_checked"] = today
        item.pop("notes", None)
        item.pop("avg_price_chf", None)
        if verified_price != result.best_price_chf:
            print(f"    verified price: CHF {verified_price:.2f} (claude said {result.best_price_chf:.2f})")
        # Image: only look up if we don't already have one — first image stays
        if item.get("image_url"):
            img_source = "kept"
        else:
            img = fetch_image_url(item["best_url"], name, client=client)
            img_source = "scrape"
            if not img:
                img = claude_image_lookup(client, name, item["best_url"])
                img_source = "claude"
            if img:
                item["image_url"] = img
            else:
                img_source = "none"
        updated += 1
        print(f"    {result.best_store}: CHF {result.best_price_chf:.2f} [img: {img_source}]")

    data["last_updated"] = today
    with open(ITEMS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nDone. Updated: {updated}, failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
