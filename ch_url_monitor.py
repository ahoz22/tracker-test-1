"""
Chrome Hearts URL Monitor
--------------------------
Periodically crawls target pages on the Chrome Hearts site, extracts all
product/collection links, and pings a Discord webhook whenever a URL shows
up that wasn't seen on the previous sweep.

SETUP:
1. pip install requests beautifulsoup4 lxml
2. Set DISCORD_WEBHOOK_URL below (or as an env var).
3. Add/adjust TARGET_PAGES to whatever sections you want watched
   (e.g. new arrivals, a specific category, the homepage).
4. Run it. First run just builds the baseline (no alerts fired, since
   everything is "new"). From the second run onward, only genuinely new
   URLs trigger a Discord message.
5. Schedule it (see bottom of file for a simple loop, or use cron).
"""

import concurrent.futures
import json
import os
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

# Pages to crawl each sweep. Add category/collection pages you care about.
TARGET_PAGES = [
    "https://www.chromehearts.com/",
    # "https://www.chromehearts.com/collections/new-arrivals",
    # add more category URLs here
]

# Only alert on links that look like product/collection pages (adjust to taste)
URL_MUST_CONTAIN = ["/products/", "/collections/"]

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


def fetch_links(page_url: str) -> set:
    """Grab all internal links from a page that match our filters.

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
        print(f"[warn] {page_url} took too long (>25s) — skipping this sweep. "
              f"This usually means the site is throttling/blocking automated requests.")
        return set()
    except requests.RequestException as e:
        print(f"[warn] failed to fetch {page_url}: {e}")
        return set()

    soup = BeautifulSoup(resp.text, "lxml")
    base_domain = urlparse(page_url).netloc

    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(page_url, href)
        parsed = urlparse(full_url)

        # stay on-domain
        if parsed.netloc != base_domain:
            continue

        # only keep URLs matching our patterns of interest
        if URL_MUST_CONTAIN and not any(p in full_url for p in URL_MUST_CONTAIN):
            continue

        # strip query params/fragments so ?variant=123 doesn't look "new"
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        found.add(clean_url)

    return found


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

    current_urls = set()
    for page in TARGET_PAGES:
        current_urls |= fetch_links(page)
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
