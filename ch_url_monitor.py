"""
Chrome Hearts URL Monitor
--------------------------
Each sweep:
  1. Visits the Chrome Hearts homepage and reads the current nav menu to
     discover whatever category pages are LIVE right now (Chrome Hearts
     rotates these — "Baccarat", "Scents", "Scarf", "Hat" etc. come and go
     with current drops, so we don't hardcode a fixed category list).
  2. Visits each of those category pages and collects product links.
  3. Compares against the saved list from the last run.
  4. Pings a Discord webhook for anything genuinely new.

SETUP:
1. pip install requests beautifulsoup4 lxml
2. Set DISCORD_WEBHOOK_URL below (or as an env var).
3. Run it. First run just builds the baseline (no alerts fired, since
   everything is "new"). From the second run onward, only genuinely new
   product URLs trigger a Discord message.
4. Schedule it (see bottom of file for a simple loop, or use cron/GitHub
   Actions with RUN_ONCE=true).
"""

import concurrent.futures
import json
import os
import re
import time
import random
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ----------------------- CONFIG -----------------------

DISCORD_WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "PUT_YOUR_WEBHOOK_URL_HERE",
)

HOMEPAGE_URL = "https://www.chromehearts.com/"

# Optional: pin specific category pages here as a safety net, in case a
# category is ever unlinked from the nav but you still want it watched.
# Auto-discovery (below) usually makes this unnecessary, but it's a
# harmless fallback.
EXTRA_PAGES = [
    "https://www.chromehearts.com/baccarat",
    "https://www.chromehearts.com/scents",
    "https://www.chromehearts.com/boxers-leggings",
    "https://www.chromehearts.com/intimates",
    "https://www.chromehearts.com/socks",
    "https://www.chromehearts.com/hoodie",
    "https://www.chromehearts.com/shirt",
    "https://www.chromehearts.com/shirts",
]

# Chrome Hearts product pages follow the pattern:
#   /{category}/{product-name}/{SKU}.html
# e.g. /baccarat/tumbler/174132CRYXXX015.html
# e.g. /scarf/ch-scarf/075372A7TXXX007.html
# This works for ANY category name (hat, hoodie, scarf, etc.) since it
# matches the URL *shape*, not a specific category word.
PRODUCT_URL_PATTERN = re.compile(r"^/[^/]+/[^/]+/[A-Za-z0-9\-]+\.html$")

# Known non-category pages that show up in the nav/footer but aren't
# product categories — used to filter out junk when auto-discovering
# category pages from the homepage.
EXCLUDED_SLUGS = {
    "", "locations", "magazine", "login", "cart", "terms",
    "disclosure", "privacy", "general", "contact",
}

STATE_FILE = Path("seen_urls.json")
CHECK_INTERVAL_SECONDS = 60 * 15  # 15 minutes — tune as you like

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ----------------------- CORE LOGIC -----------------------


def load_seen_urls() -> set:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_urls(urls: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(urls), f, indent=2)


def get_page_links(page_url: str) -> set:
    """Fetch a page and return every on-domain link found (unfiltered).

    Uses a hard wall-clock timeout (via a worker thread) in addition to
    requests' own timeout. Some sites with bot-protection deliberately
    trickle bytes just slowly enough to dodge a plain read-timeout, which
    can make a request hang far longer than the timeout= value suggests.
    """
    print(f"[info] fetching {page_url} ...", flush=True)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                requests.get, page_url, headers=HEADERS, timeout=15
            )
            resp = future.result(timeout=25)  # absolute cap, no matter what
        resp.raise_for_status()
    except concurrent.futures.TimeoutError:
        print(f"[warn] {page_url} took too long (>25s) — skipping. "
              f"This usually means the site is throttling/blocking automated requests.")
        return set()
    except requests.RequestException as e:
        print(f"[warn] failed to fetch {page_url}: {e}")
        return set()

    soup = BeautifulSoup(resp.text, "lxml")
    base_domain = urlparse(page_url).netloc

    links = set()
    for a in soup.find_all("a", href=True):
        full_url = urljoin(page_url, a["href"])
        parsed = urlparse(full_url)
        if parsed.netloc != base_domain:
            continue
        # strip query params/fragments so ?variant=123 doesn't look "new"
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        links.add(clean_url)

    return links


def is_category_url(path: str) -> bool:
    """A category page is a single clean path segment, e.g. /scarf,
    /baccarat, /hat — not a file (.html) and not a known junk page."""
    clean = path.strip("/")
    if not clean or "." in clean or "/" in clean:
        return False
    return clean.lower() not in EXCLUDED_SLUGS


def is_product_url(path: str) -> bool:
    return bool(PRODUCT_URL_PATTERN.match(path))


def discover_category_pages() -> set:
    """Read the live homepage nav to find whatever category pages are
    currently active — adapts automatically as Chrome Hearts rotates
    categories in and out (e.g. "scarf" one week, "hat" the next)."""
    homepage_links = get_page_links(HOMEPAGE_URL)
    categories = {u for u in homepage_links if is_category_url(urlparse(u).path)}
    print(f"[info] discovered {len(categories)} live category page(s): {sorted(categories)}")
    return categories


def send_discord_alert(new_urls: set) -> None:
    if not DISCORD_WEBHOOK_URL or "PUT_YOUR_WEBHOOK_URL_HERE" in DISCORD_WEBHOOK_URL:
        print("[warn] Discord webhook not configured — skipping alert, printing instead:")
        for u in new_urls:
            print("  NEW:", u)
        return

    # Discord embeds max out at 25 fields / message length limits, so batch if needed
    urls_list = sorted(new_urls)
    chunk_size = 10
    for i in range(0, len(urls_list), chunk_size):
        chunk = urls_list[i : i + chunk_size]
        description = "\n".join(f"• {u}" for u in chunk)
        payload = {
            "content": "<@222217823382929408>",
            "embeds": [
                {
                    "title": "🚨 New Chrome Hearts URL(s) Detected",
                    "description": description,
                    "color": 0x000000,
                }
            ]
        }
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[warn] failed to send Discord alert: {e}")


def run_sweep() -> None:
    seen = load_seen_urls()
    is_first_run = len(seen) == 0

    pages_to_check = discover_category_pages() | set(EXTRA_PAGES)

    current_urls = set()
    for page in pages_to_check:
        page_links = get_page_links(page)
        current_urls |= {u for u in page_links if is_product_url(urlparse(u).path)}
        time.sleep(random.uniform(1, 3))  # be polite between requests

    new_urls = current_urls - seen

    if is_first_run:
        print(f"[info] First run — baseline of {len(current_urls)} URLs saved. No alerts sent.")
    elif new_urls:
        print(f"[info] Found {len(new_urls)} new URL(s). Sending Discord alert...")
        send_discord_alert(new_urls)
    else:
        print("[info] No new URLs this sweep.")

    # keep the union so we never "forget" a URL that temporarily vanishes
    save_seen_urls(seen | current_urls)


# ----------------------- SCHEDULER -----------------------

if __name__ == "__main__":
    # When running under GitHub Actions (or anywhere the scheduling is
    # handled externally by a cron job), set RUN_ONCE=true so the script
    # does a single sweep and exits, instead of looping forever.
    if os.environ.get("RUN_ONCE", "").lower() == "true":
        run_sweep()
    else:
        print("Starting Chrome Hearts URL monitor. Press Ctrl+C to stop.")
        while True:
            try:
                run_sweep()
            except Exception as e:
                print(f"[error] sweep failed: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)
