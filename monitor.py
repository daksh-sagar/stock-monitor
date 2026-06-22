#!/usr/bin/env python3
"""Monitor Scentoria collections for newly added products and push a phone
notification via ntfy (https://ntfy.sh) for each new item.

State (the set of product IDs already seen) is stored as JSON under state/ and
committed back to the repo by the GitHub Action, so it persists between runs.

Required env var:
    NTFY_TOPIC   - your secret ntfy topic name (e.g. scentoria-xxxxxxxx)
Optional env vars:
    NTFY_SERVER  - ntfy server base URL (default: https://ntfy.sh)
    NTFY_TOKEN   - bearer token, only if you use a reserved/auth-protected topic
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Per-collection config. `max_pages` controls how much of the feed we read:
#   - new-arrivals is sorted newest-published-first, so brand-new products always
#     appear on page 1. We only need a small window from the top (the collection
#     itself has 5000+ items — fetching it all every few minutes is needless).
#   - restocked-gems is NOT date-sorted (it's ordered by when items were added to
#     the collection, which the feed doesn't expose), so we must read the whole
#     collection and diff the full set of IDs. It's small (~1.2k items / 5 pages).
COLLECTIONS = {
    "new-arrivals": {
        "url": "https://scentoria.co.in/collections/new-arrivals/products.json",
        "max_pages": 3,      # top ~750 newest; far more headroom than needed per 5 min
    },
    "restocked-gems": {
        "url": "https://scentoria.co.in/collections/restocked-gems/products.json",
        "max_pages": None,   # read the entire collection
    },
}
PRODUCT_URL = "https://scentoria.co.in/products/{handle}"
COLLECTION_URL = "https://scentoria.co.in/collections/{collection}"

STATE_DIR = Path("state")
USER_AGENT = "scentoria-monitor/1.0 (+https://github.com/)"
PAGE_SIZE = 250          # Shopify max per page
HARD_PAGE_CAP = 25       # safety stop for full fetches (= up to 6250 products)
# If more than this many new products appear at once, send one summary instead
# of spamming an individual notification per product.
MAX_INDIVIDUAL = 8

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()


def fetch_products(url, max_pages=None):
    """Fetch products from a collection feed, following Shopify pagination.

    max_pages=None reads the whole collection (up to HARD_PAGE_CAP pages);
    an integer reads only that many pages from the top of the feed.
    """
    limit = max_pages if max_pages is not None else HARD_PAGE_CAP
    products = []
    for page in range(1, limit + 1):
        full_url = f"{url}?limit={PAGE_SIZE}&page={page}"
        req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.load(resp).get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return products


def load_seen(name):
    """Return the set of previously-seen product IDs, or None on first run."""
    f = STATE_DIR / f"{name}.json"
    if not f.exists():
        return None
    try:
        return set(json.loads(f.read_text()))
    except (json.JSONDecodeError, ValueError):
        # Corrupt state: treat as empty so we re-seed rather than crash.
        return set()


def save_seen(name, ids):
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(json.dumps(sorted(ids)))


def product_price(p):
    variants = p.get("variants") or []
    if variants and variants[0].get("price"):
        return variants[0]["price"]
    return None


def product_image(p):
    images = p.get("images") or []
    if images and images[0].get("src"):
        return images[0]["src"]
    return None


def send_ntfy(title, message, click=None, tags=None, priority=None, icon=None):
    """Publish a notification using ntfy's JSON format (UTF-8 safe)."""
    if not NTFY_TOPIC:
        print("ERROR: NTFY_TOPIC is not set; cannot send notification.", file=sys.stderr)
        return False
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    if priority:
        payload["priority"] = priority
    if icon:
        payload["icon"] = icon

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    req = urllib.request.Request(NTFY_SERVER, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ntfy HTTP {e.code}: {body}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"ntfy network error: {e}", file=sys.stderr)
    return False


def notify_new(collection, products):
    label = collection.replace("-", " ").title()

    if len(products) > MAX_INDIVIDUAL:
        preview = ", ".join(p.get("title", "?") for p in products[:5])
        send_ntfy(
            title=f"{len(products)} new in {label}",
            message=f"{preview} … and {len(products) - 5} more.",
            click=COLLECTION_URL.format(collection=collection),
            tags=["shopping_bags"],
            priority=4,
        )
        return

    for p in products:
        price = product_price(p)
        body = f"₹{price}" if price else "New product"
        send_ntfy(
            title=p.get("title", "New product"),
            message=f"{body}\nin {label}",
            click=PRODUCT_URL.format(handle=p.get("handle", "")),
            tags=["shopping_bags"],
            priority=4,
            icon=product_image(p),
        )


def main():
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC env var is required.")

    exit_code = 0
    for name, cfg in COLLECTIONS.items():
        try:
            products = fetch_products(cfg["url"], cfg.get("max_pages"))
        except Exception as e:  # network / parse errors: skip without touching state
            print(f"[{name}] fetch failed: {e}", file=sys.stderr)
            exit_code = 1
            continue

        if not products:
            # Empty response is almost certainly a transient glitch, not a real
            # "collection is now empty". Skip so we never wipe state or misfire.
            print(f"[{name}] returned 0 products — skipping this run.", file=sys.stderr)
            exit_code = 1
            continue

        current = {str(p["id"]): p for p in products}
        seen = load_seen(name)

        if seen is None:
            save_seen(name, current.keys())
            print(f"[{name}] first run — seeded {len(current)} products (no alerts).")
            send_ntfy(
                title=f"Monitoring started: {name}",
                message=(f"Now tracking {len(current)} products in "
                         f"{name.replace('-', ' ').title()}. "
                         f"You'll be alerted on new additions."),
                tags=["eyes"],
            )
            continue

        new_products = [current[pid] for pid in current if pid not in seen]
        if new_products:
            print(f"[{name}] {len(new_products)} new product(s):")
            for p in new_products:
                print(f"    + {p.get('title')} ({p.get('handle')})")
            notify_new(name, new_products)
        else:
            print(f"[{name}] no new products ({len(current)} tracked).")

        # Update state to the current catalog. Comparing against the *previous*
        # snapshot means an item removed then re-added (a genuine restock) will
        # alert again — which is exactly what 'restocked-gems' is about.
        save_seen(name, current.keys())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
