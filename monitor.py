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
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
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


# Variant types we IGNORE for restock alerts (small try-size formats). Matched
# case-insensitively as a substring of the variant's option/title. "DECAANT"
# covers a real typo present in the store's data.
EXCLUDE_VARIANT_TYPES = (
    "DECANT", "DECAANT", "SAMPLE", "MINIATURE", "ROLL ON", "ROLL-ON", "ROLLON",
)


def variant_name(v):
    return v.get("option1") or v.get("title") or "?"


def is_notifiable_variant(v):
    """False for the small try-size formats we ignore (decant, sample,
    miniature, roll-on); True for testers, retail, partials, sets, etc."""
    name = variant_name(v).upper()
    return not any(t in name for t in EXCLUDE_VARIANT_TYPES)


def live_available_variants(handle):
    """Real-time set of in-stock variant-id strings for a product, read from the
    storefront `.js` endpoint — the same data the live product page uses.

    The collection products.json feed carries an `available` flag that matches
    the live page, but it's CDN-cached, and Shopify's per-product `.json`
    endpoint omits availability entirely. So we use this to *confirm* a candidate
    restock in real time before notifying. Returns None if the check itself
    failed (network/parse), so the caller can retry next run instead of acting.
    """
    url = f"https://scentoria.co.in/products/{handle}.js"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"   live-check failed for {handle}: {e}", file=sys.stderr)
        return None
    return {str(v.get("id")) for v in data.get("variants", []) if v.get("available")}


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


def notify_added(collection, label, products):
    """Notify about newly-listed products (new-arrivals)."""
    if len(products) > MAX_INDIVIDUAL:
        preview = ", ".join(p.get("title", "?") for p in products[:5])
        send_ntfy(
            title=f"{len(products)} new in {label}",
            message=f"{preview} … and {len(products) - 5} more.",
            click=COLLECTION_URL.format(collection=collection),
            tags=["shopping_bags"], priority=4,
        )
        return
    for p in products:
        price = product_price(p)
        head = f"₹{price}" if price else "New product"
        send_ntfy(
            title=p.get("title", "Product"),
            message=f"{head}\nin {label}",
            click=PRODUCT_URL.format(handle=p.get("handle", "")),
            tags=["shopping_bags"], priority=4, icon=product_image(p),
        )


def notify_restocked(collection, label, items):
    """Notify about variants back in stock. `items` is a list of
    (product, variant); we group by product so a product restocking several
    variants is one notification that names the variants."""
    by_product = {}
    for p, v in items:
        by_product.setdefault(str(p["id"]), (p, []))[1].append(v)
    groups = list(by_product.values())

    if len(groups) > MAX_INDIVIDUAL:
        preview = ", ".join(p.get("title", "?") for p, _ in groups[:5])
        send_ntfy(
            title=f"{len(groups)} back in stock in {label}",
            message=f"{preview} … and {len(groups) - 5} more.",
            click=COLLECTION_URL.format(collection=collection),
            tags=["fire"], priority=4,
        )
        return

    for p, vs in groups:
        names = ", ".join(variant_name(v) for v in vs)
        prices = [v.get("price") for v in vs if v.get("price")]
        if prices:
            cheapest = min(prices, key=float)
            price_line = f" · ₹{cheapest}" if len(prices) == 1 else f" · from ₹{cheapest}"
        else:
            price_line = ""
        send_ntfy(
            title=p.get("title", "Product"),
            message=f"Back in stock: {names}{price_line}\nin {label}",
            click=PRODUCT_URL.format(handle=p.get("handle", "")),
            tags=["fire"], priority=4, icon=product_image(p),
        )


def process_added(name, label, products):
    """new-arrivals: alert when a new product id appears in the feed."""
    current = {str(p["id"]): p for p in products}
    seen = load_seen(name)
    if seen is None:
        save_seen(name, current.keys())
        print(f"[{name}] first run — seeded {len(current)} products (no alerts).")
        send_ntfy(
            title=f"Monitoring started: {label}",
            message=(f"Now tracking {len(current)} products in {label}. "
                     f"You'll be alerted on new additions."),
            tags=["eyes"],
        )
        return
    fresh = [current[k] for k in current if k not in seen]
    if fresh:
        print(f"[{name}] {len(fresh)} new listing(s):")
        for p in fresh:
            print(f"    + {p.get('title')} ({p.get('handle')})")
        notify_added(name, label, fresh)
    else:
        print(f"[{name}] no new products ({len(current)} tracked).")
    save_seen(name, current.keys())


def process_restocked(name, label, products):
    """restocked-gems: track in-stock, notifiable variants and alert when one
    becomes available — confirmed in real time before notifying."""
    # Candidate in-stock notifiable variants from the (cached) collection feed.
    candidates = {}  # variant-id -> (product, variant)
    for p in products:
        for v in (p.get("variants") or []):
            if v.get("available") and is_notifiable_variant(v):
                candidates[str(v["id"])] = (p, v)

    # Health guard: this collection always has plenty in stock. If the feed
    # suddenly reports none (e.g. the `available` field vanished), don't wipe
    # state or alert — just skip and let it recover.
    if not candidates:
        print(f"[{name}] 0 in-stock notifiable variants — suspicious, skipping.",
              file=sys.stderr)
        return

    seen = load_seen(name)
    if seen is None:
        save_seen(name, candidates.keys())
        print(f"[{name}] first run — seeded {len(candidates)} in-stock variants (no alerts).")
        send_ntfy(
            title=f"Monitoring started: {label}",
            message=(f"Watching {label} for restocks of testers, retail, partials "
                     f"& sets (decants, samples & miniatures ignored). Tracking "
                     f"{len(candidates)} in-stock variants — you'll be alerted when "
                     f"more come back."),
            tags=["eyes"],
        )
        return

    fresh_ids = [vid for vid in candidates if vid not in seen]

    # Confirm each candidate restock in real time via the .js endpoint before
    # notifying (one fetch per product). Guards against a stale cached flag.
    confirmed, confirmed_ids = [], set()
    by_handle = {}
    for vid in fresh_ids:
        p, _ = candidates[vid]
        by_handle.setdefault(p.get("handle"), []).append(vid)
    for handle, vids in by_handle.items():
        live = live_available_variants(handle)
        if live is None:
            continue  # couldn't confirm — leave unseen so we retry next run
        for vid in vids:
            if vid in live:
                confirmed_ids.add(vid)
                confirmed.append(candidates[vid])

    if confirmed:
        print(f"[{name}] {len(confirmed)} confirmed restock(s):")
        for p, v in confirmed:
            print(f"    + {p.get('title')} — {variant_name(v)}")
        notify_restocked(name, label, confirmed)
    else:
        unconfirmed = len(fresh_ids) - len(confirmed_ids)
        extra = f" ({unconfirmed} candidate(s) not confirmed)" if unconfirmed else ""
        print(f"[{name}] no confirmed restocks ({len(candidates)} in stock){extra}.")

    # Keep previously-seen variants that are still in stock, plus the newly
    # confirmed ones. Unconfirmed candidates stay out so they're re-checked.
    save_seen(name, (seen & set(candidates)) | confirmed_ids)


def main():
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC env var is required.")

    for name, cfg in COLLECTIONS.items():
        mode = cfg.get("mode", "added")
        label = name.replace("-", " ").title()
        try:
            products = fetch_products(cfg["url"], cfg.get("max_pages"))
        except Exception as e:  # network / parse errors: skip without touching state
            print(f"[{name}] fetch failed: {e}", file=sys.stderr)
            continue

        if not products:
            # Empty response is almost certainly a transient glitch, not a real
            # "collection is now empty". Skip so we never wipe state or misfire.
            print(f"[{name}] returned 0 products — skipping this run.", file=sys.stderr)
            continue

        if mode == "restocked":
            process_restocked(name, label, products)
        else:
            process_added(name, label, products)

    return 0


if __name__ == "__main__":
    sys.exit(main())
