# Scentoria stock monitor

A **Cloudflare Worker** (cron trigger, every 2 minutes) that sends a **free push
notification to your phone** (via a [Telegram](https://telegram.org) bot) for:

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
5. **A product is recorded as seen only after its notification is delivered.**
   If a send fails, the item stays unseen and is retried next run instead of
   being silently lost. (Failed-send-then-mark-seen was a real bug.)
6. What does **not** alert: a decant restocking, an item selling out, or a
   product being removed.
7. **First run seeds silently** — records current state and sends one
   "monitoring started" message instead of flooding you.

## One-time setup

### 1. Create a Telegram bot and find your chat id

- In Telegram, message [**@BotFather**](https://t.me/BotFather): send
  `/newbot`, follow the prompts, and copy the **bot token** it gives you
  (looks like `123456789:AA…`). This is `TG_BOT_TOKEN` in step 4.
- **Start a chat with your new bot** and send it any message (e.g. `hi`) — a
  bot can't message you until you've messaged it first.
- **Get your chat id:** open
  `https://api.telegram.org/bot<TG_BOT_TOKEN>/getUpdates` in a browser and read
  `result[].message.chat.id` (a number, possibly negative for groups). That's
  `TG_CHAT_ID` in step 4.

  > **Why Telegram and not ntfy?** ntfy.sh meters its free quota **per IP**
  > (`"basis": "ip"` on free accounts), and Cloudflare Workers egress from
  > **shared IPs** — so the daily 250-message cap is burned through by unrelated
  > Cloudflare tenants and every send 429s, *even with an access token* (the
  > token authenticates but doesn't move you off the shared-IP quota; only a
  > paid tier does). The Telegram Bot API is keyed by bot token + chat,
  > independent of source IP, with no daily cap.

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

### 4. Set the Telegram secrets

```bash
printf '%s' "123456789:AA_your_bot_token" | npx wrangler secret put TG_BOT_TOKEN
printf '%s' "your_chat_id"                | npx wrangler secret put TG_CHAT_ID
# Optional — enables the manual ?key=… HTTP trigger/test endpoint:
printf '%s' "$(openssl rand -hex 16)"     | npx wrangler secret put TRIGGER_KEY
```

(Use `printf`, not `echo`, to avoid a trailing newline in the value.)
The old `NTFY_*` secrets are unused now and can be removed:
`npx wrangler secret delete NTFY_TOKEN` and `npx wrangler secret delete NTFY_TOPIC`.

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
  `https://scentoria-monitor.<your-subdomain>.workers.dev/?key=<TRIGGER_KEY>&test=1`
  — sends one "✅ test" notification.
- **Trigger a check manually** (also a health check):
  `https://scentoria-monitor.<your-subdomain>.workers.dev/?key=<TRIGGER_KEY>`
  (the `key` must equal your `TRIGGER_KEY` secret; the endpoint is disabled if
  that secret isn't set). Returns a one-line status per collection.
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
