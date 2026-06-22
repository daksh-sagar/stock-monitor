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

# Per-collection config:
#   max_pages - how much of the feed to read (None = the whole collection).
#   mode      - what counts as an alert:
#       "added"     -> a product ID newly appears in the feed (a new listing).
#       "restocked" -> a product becomes available: sold-out -> in stock, or a
#                      brand-new in-stock item. Needed because this store keeps
#                      sold-out items listed and just flips their stock flag, so a
#                      restock is NOT a new product ID — it's an availability change.
#
# new-arrivals is sorted newest-first and has no sold-out items, so we watch a
#   small window from the top of the feed for newly-listed products.
# restocked-gems is "Featured"-sorted and keeps ~half its items sold-out-but-listed,
#   so we read the whole collection and watch for flips to in-stock.
COLLECTIONS = {
    "new-arrivals": {
        "url": "https://scentoria.co.in/collections/new-arrivals/products.json",
        "max_pages": 3,      # top ~750 newest; far more headroom than needed per 5 min
        "mode": "added",
    },
    "restocked-gems": {
        "url": "https://scentoria.co.in/collections/restocked-gems/products.json",
        "max_pages": None,   # read the entire collection
        "mode": "restocked",
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


def is_available(p):
    """True if any variant of the product is in stock."""
    return any(v.get("available") for v in (p.get("variants") or []))


def tracked_products(products, mode):
    """Map of product-id -> product for the items this collection alerts on.

    "restocked" mode tracks only in-stock products, so a sold-out item coming
    back (or a new in-stock item) shows up as a newly-tracked id. "added" mode
    tracks every listed product, so a new listing shows up as a new id.
    """
    if mode == "restocked":
        return {str(p["id"]): p for p in products if is_available(p)}
    return {str(p["id"]): p for p in products}


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


def notify(collection, mode, products):
    label = collection.replace("-", " ").title()
    tag = "fire" if mode == "restocked" else "shopping_bags"

    if len(products) > MAX_INDIVIDUAL:
        preview = ", ".join(p.get("title", "?") for p in products[:5])
        headline = (f"{len(products)} back in stock in {label}"
                    if mode == "restocked"
                    else f"{len(products)} new in {label}")
        send_ntfy(
            title=headline,
            message=f"{preview} … and {len(products) - 5} more.",
            click=COLLECTION_URL.format(collection=collection),
            tags=[tag],
            priority=4,
        )
        return

    for p in products:
        price = product_price(p)
        price_str = f"₹{price}" if price else None
        if mode == "restocked":
            head = "In stock now" + (f" · {price_str}" if price_str else "")
        else:
            head = price_str or "New product"
        send_ntfy(
            title=p.get("title", "Product"),
            message=f"{head}\nin {label}",
            click=PRODUCT_URL.format(handle=p.get("handle", "")),
            tags=[tag],
            priority=4,
            icon=product_image(p),
        )


def main():
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC env var is required.")

    exit_code = 0
    for name, cfg in COLLECTIONS.items():
        mode = cfg.get("mode", "added")
        label = name.replace("-", " ").title()
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

        tracked = tracked_products(products, mode)
        seen = load_seen(name)

        if seen is None:
            save_seen(name, tracked.keys())
            print(f"[{name}] first run — seeded {len(tracked)} (mode={mode}, no alerts).")
            if mode == "restocked":
                msg = (f"Watching {label} for restocks. {len(tracked)} items "
                       f"currently in stock — you'll be alerted when sold-out "
                       f"items come back or new in-stock ones drop.")
            else:
                msg = (f"Now tracking {len(tracked)} products in {label}. "
                       f"You'll be alerted on new additions.")
            send_ntfy(title=f"Monitoring started: {label}", message=msg, tags=["eyes"])
            continue

        # A "fresh" item is one tracked now but not in the previous snapshot:
        #   added mode     -> a newly-listed product.
        #   restocked mode -> in stock now but wasn't before (back in stock, or a
        #                     brand-new in-stock item). Sold-outs/removals just
        #                     drop out of the set and never alert.
        fresh = [tracked[pid] for pid in tracked if pid not in seen]
        if fresh:
            print(f"[{name}] {len(fresh)} alert(s) (mode={mode}):")
            for p in fresh:
                print(f"    + {p.get('title')} ({p.get('handle')})")
            notify(name, mode, fresh)
        else:
            print(f"[{name}] nothing new ({len(tracked)} tracked, mode={mode}).")

        save_seen(name, tracked.keys())

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
