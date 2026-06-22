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

### 1. Install ntfy, subscribe, and get an access token

- Install **ntfy**: [iOS](https://apps.apple.com/app/ntfy/id1625396347) /
  [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
- **+ → Subscribe to topic**, enter a long random topic name (this is your only
  "password" on the free tier, so keep it unguessable):

  ```
  scentoria-kmiw08euftlucpwx
  ```
- **Create a free ntfy.sh account and access token** (required — see note
  below). At [ntfy.sh](https://ntfy.sh): **Sign up**, then **Account → Access
  tokens → Create access token** (label it `scentoria-worker`, expiry "Never").
  Copy the `tk_…` token for step 4.

  > **Why a token?** ntfy rate-limits *anonymous* publishing by the publisher's
  > IP, and Cloudflare Workers egress from **shared IPs** — so anonymous sends
  > collide with other Cloudflare users and get HTTP 429 almost immediately.
  > Authenticating ties the limit to your account (250 msgs/day free, we use a
  > handful), which fixes it. Your phone still just subscribes to the public
  > topic; the token is only used by the Worker to publish.

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

### 4. Set the ntfy secrets

```bash
printf '%s' "scentoria-kmiw08euftlucpwx" | npx wrangler secret put NTFY_TOPIC
printf '%s' "tk_your_token_here"          | npx wrangler secret put NTFY_TOKEN
```

(Use `printf`, not `echo`, to avoid a trailing newline in the value.)
`NTFY_SERVER` defaults to `https://ntfy.sh` (see `[vars]` in `wrangler.toml`).

### 5. Deploy

```bash
npx wrangler deploy
```

On a new account the first deploy prompts: **"register a workers.dev subdomain
now?"** — say yes and pick any name (this is a one-time account setup that also
enables cron triggers). The cron then starts immediately; the first run seeds
state silently and sends the "monitoring started" pushes.

## Testing & operating it

- **Logic tests** (offline, no Cloudflare needed): `npm test`
- **Test a push to your phone:** open
  `https://scentoria-monitor.<your-subdomain>.workers.dev/?key=<NTFY_TOPIC>&test=1`
  — sends one "✅ test" notification.
- **Trigger a check manually** (also a health check):
  `https://scentoria-monitor.<your-subdomain>.workers.dev/?key=<NTFY_TOPIC>`
  (the `key` must equal your `NTFY_TOPIC`). Returns a one-line status per
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
