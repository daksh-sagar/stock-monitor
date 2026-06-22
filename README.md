# Scentoria stock monitor

A **Cloudflare Worker** (cron trigger, every 2 minutes) that sends a **free push
notification to your phone** (via [ntfy](https://ntfy.sh)) for:

- **new-arrivals** → when a **new product is listed**.
- **restocked-gems** → when a tester / retail / partial / set variant comes
  **back in stock** (decants, samples, miniatures & roll-ons are ignored).

No server to run, no cost. Cloudflare's free tier covers it comfortably.

## How it works

1. The site is a Shopify store, so each collection has a JSON feed at
   `…/collections/<name>/products.json`. No HTML scraping.
2. On each run the Worker reads both feeds and compares to the last snapshot
   stored in **Workers KV**:
   - **new-arrivals (`added`):** tracks listed product IDs; alerts on a new one.
     Sorted newest-first, so we only read the top 3 pages (it has 5000+ items).
   - **restocked-gems (`restocked`):** this collection *keeps sold-out items
     listed* and flips their stock flag, so a restock is a **variant** becoming
     available, not a new product. We track availability at the **SKU/variant**
     level and ignore small formats (`EXCLUDE_VARIANT_TYPES` in
     `src/worker.js`). We read the whole collection (~1.2k items).
3. **Availability is double-checked.** The collection feed's `available` flag is
   accurate but CDN-cached, and Shopify's per-product `.json` endpoint omits
   availability. So before alerting a restock we re-confirm that exact variant
   in real time via `…/products/<handle>.js`. Unconfirmed candidates aren't
   alerted and get re-checked next run.
4. **KV writes only on change.** We read every run but only write when the
   tracked set actually changes — staying well under the free tier's 1,000
   writes/day.
5. What does **not** alert: a decant restocking, an item selling out, or a
   product being removed.
6. **First run seeds silently** — records current state and sends one
   "monitoring started" message instead of flooding you.

## One-time setup

### 1. Install ntfy and subscribe to your topic

- Install **ntfy**: [iOS](https://apps.apple.com/app/ntfy/id1625396347) /
  [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy). No
  account needed.
- **+ → Subscribe to topic**, enter a long random topic name (this is your only
  "password" on the free tier, so keep it unguessable):

  ```
  scentoria-kmiw08euftlucpwx
  ```
- Test it: `curl -d "It works!" ntfy.sh/scentoria-kmiw08euftlucpwx`

### 2. Authenticate Cloudflare

```bash
cd /Users/daksh/Documents/scentoria-monitor
npx wrangler login        # opens a browser; pick your Cloudflare account
```

(Free Cloudflare account is fine. If you don't have one, sign up at
dash.cloudflare.com first.)

### 3. Create the KV namespace and add its id

```bash
npx wrangler kv namespace create STATE
```

Copy the printed `id` into `wrangler.toml`, replacing `REPLACE_WITH_KV_NAMESPACE_ID`.

### 4. Set your ntfy topic as a secret

```bash
echo "scentoria-kmiw08euftlucpwx" | npx wrangler secret put NTFY_TOPIC
```

Optional secrets: `NTFY_TOKEN` (only for an auth-protected/reserved topic).
`NTFY_SERVER` defaults to `https://ntfy.sh` (see `[vars]` in `wrangler.toml`).

### 5. Deploy

```bash
npx wrangler deploy
```

The cron starts immediately. The first run seeds state silently and sends the
"monitoring started" pushes.

## Testing & operating it

- **Logic tests** (offline, no Cloudflare needed): `npm test`
- **Trigger a run manually** (also a health check) — open in a browser:
  `https://scentoria-monitor.<your-subdomain>.workers.dev/?key=scentoria-kmiw08euftlucpwx`
  (the `key` must equal your `NTFY_TOPIC`). It returns a one-line status per
  collection.
- **Live logs:** `npx wrangler tail`
- **Change frequency:** edit `crons` in `wrangler.toml` (e.g. `"*/1 * * * *"`
  for every minute) and redeploy.
- **Change which SKU types alert:** edit `EXCLUDE_VARIANT_TYPES` in
  `src/worker.js` and redeploy.
- **Reset a collection:** `npx wrangler kv key delete --binding=STATE <name>`
  (`new-arrivals` or `restocked-gems`); next run re-seeds it silently.

## Notes

- **Politeness/limits:** the restocked feed is ~3.5 MB. Every 2 minutes is a
  deliberate balance — feel free to loosen it. Cloudflare free tier allows down
  to 1-minute cron.
- The notification target lives in a Worker **secret**, so this repo is safe to
  keep public.
