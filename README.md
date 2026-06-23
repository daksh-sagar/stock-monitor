# Stock monitor

A **GitHub Actions workflow**, triggered every few minutes by a free external
cron service (via the `workflow_dispatch` API), that watches a Shopify store and
sends a **free push notification to your phone** (via a
[Telegram](https://telegram.org) bot) for:

- **new-arrivals** → when a **new product is listed**.
- **restocked-gems** → when a tester / retail / partial / set variant comes
  **back in stock** (decants, samples, miniatures & roll-ons are ignored).

No server to run, no cost — it runs on GitHub's free Actions minutes and keeps
its state in this repo.

## How it works

1. The site is a Shopify store, so each collection has a JSON feed at
   `…/collections/<name>/products.json`. No HTML scraping.
2. On each run (`src/run.mjs` → `src/monitor.js`) it reads **both feeds in full**
   and compares them to the last snapshot stored as JSON under `state/`:
   - **new-arrivals (`added`):** tracks every listed product ID; alerts on a new
     one. We read the **whole** collection (~5k items) because a product can be
     (re-)listed with an old `created_at` and land deep in the feed — a
     top-pages window would miss it.
   - **restocked-gems (`restocked`):** this collection *keeps sold-out items
     listed* and flips their stock flag, so a restock is a **variant** becoming
     available, not a new product. We track availability at the **SKU/variant**
     level and ignore small formats (`EXCLUDE_VARIANT_TYPES` in
     `src/monitor.js`). We read the whole collection (~1.1k items).
3. **Availability is double-checked.** The collection feed's `available` flag is
   accurate but CDN-cached, and Shopify's per-product `.json` endpoint omits
   availability. So before alerting a restock we re-confirm that exact variant
   in real time via `…/products/<handle>.js`. Unconfirmed candidates aren't
   alerted and get re-checked next run.
4. **State writes only on change.** The workflow commits `state/*.json` back to
   the repo only when the tracked set actually changes (ids are sorted, so diffs
   stay minimal).
5. **A product is recorded as seen only after its notification is delivered.**
   If a send fails, the item stays unseen and is retried next run instead of
   being silently lost. (Failed-send-then-mark-seen was a real bug.)
6. What does **not** alert: a decant restocking, an item selling out, or a
   product being removed.
7. **First run seeds silently** — records current state and sends one
   "monitoring started" message per collection instead of flooding you.

> **Why GitHub Actions (and not a Cloudflare Worker)?** Reading the *whole*
> ~5k-item collection needs more CPU than the Workers Free plan allows (10 ms
> per invocation — the run was killed mid-parse). GitHub Actions runners have no
> per-run CPU cap.
>
> **Why an external trigger (and not GitHub's `schedule`)?** GitHub's native
> cron is best-effort: it never reliably dispatched the schedule (first-run
> activation can lag for hours, and `*/5` ticks get dropped under load). So the
> workflow is `workflow_dispatch`-only and a free external cron service calls
> the dispatch API on a fixed cadence — punctual, and with no 5-minute floor.

> **Why Telegram (and not ntfy)?** ntfy.sh meters its free quota **per IP**
> (`"basis": "ip"`), so on shared/cloud egress IPs the daily cap is burned
> through by unrelated tenants and every send 429s — *even with an access
> token*. The Telegram Bot API is keyed by bot token + chat, independent of
> source IP, with no daily cap.

## One-time setup

### 1. Create a Telegram bot and find your chat id

- In Telegram, message [**@BotFather**](https://t.me/BotFather): send `/newbot`,
  follow the prompts, and copy the **bot token** (looks like `123456789:AA…`).
- **Start a chat with your new bot** and send it any message (e.g. `hi`) — a bot
  can't message you until you've messaged it first.
- **Get your chat id:** open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read
  `result[].message.chat.id` (a number, negative for groups).

### 2. Add the GitHub Secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret** (or via the CLI). The store URL is a secret too, so the target site
isn't hard-coded in this repo:

```bash
printf '%s' "https://your-store.example"  | gh secret set STORE_BASE_URL
printf '%s' "123456789:AA_your_bot_token" | gh secret set TG_BOT_TOKEN
printf '%s' "your_chat_id"                | gh secret set TG_CHAT_ID
```

### 3. Allow the workflow to commit state back

**Settings → Actions → General → Workflow permissions → Read and write
permissions.** (The job commits `state/*.json` back to the repo.) The workflow
lives in `.github/workflows/monitor.yml` on the default branch; its first run
seeds `state/` and sends the "monitoring started" messages.

### 4. Set up the external trigger (the scheduler)

The workflow is `workflow_dispatch`-only, so something must call it on a
cadence. Use any free cron service (e.g. [cron-job.com](https://cron-job.com)):

1. **Create a fine-grained PAT** (github.com/settings/personal-access-tokens):
   repository access = **only this repo**; permission **Actions: Read and
   write**; set an expiry.
2. **Create a cron job** that POSTs every ~2 minutes:
   ```
   URL:    https://api.github.com/repos/<owner>/<repo>/actions/workflows/monitor.yml/dispatches
   Method: POST
   Headers: Accept: application/vnd.github+json
            Authorization: Bearer <YOUR_PAT>
            X-GitHub-Api-Version: 2022-11-28
   Body:   {"ref":"main"}
   ```
   A successful trigger returns HTTP **204**.

## Testing & operating it

- **Logic tests** (offline, no network): `npm test`
- **Run it locally** (does a real check + sends real notifications):
  ```bash
  TG_BOT_TOKEN=… TG_CHAT_ID=… npm start
  ```
- **Run it on demand in CI:** Actions tab → *Stock monitor* → **Run
  workflow** (or `gh workflow run monitor.yml`).
- **Logs:** the Actions tab; each run prints a one-line status per collection.
- **Change frequency:** adjust the schedule in the external cron service
  (no 5-minute floor — dispatch can fire as often as you like).
- **Change which SKU types alert:** edit `EXCLUDE_VARIANT_TYPES` in
  `src/monitor.js`.
- **Reset a collection:** delete `state/<name>.json` (`new-arrivals` or
  `restocked-gems`) and commit; the next run re-seeds it silently.

## Notes

- State (`state/*.json`) is committed by `github-actions[bot]` with
  `[skip ci]`, and a `concurrency` group prevents two runs from racing.
- Credentials live in **GitHub Secrets**, so this repo is safe to keep public.
