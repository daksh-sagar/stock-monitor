# Scentoria stock monitor

Watches two Scentoria collections and sends a **free push notification to your
phone** (via [ntfy](https://ntfy.sh)):

- **new-arrivals** → alerts when a **new product is listed**.
- **restocked-gems** → alerts when an item is **back in stock** (or a new
  in-stock item drops).

It runs on a **GitHub Actions** cron (every ~5 minutes). No server, no cost.

## How it works

1. The site is a Shopify store, so each collection exposes a clean JSON feed at
   `…/collections/<name>/products.json`. No HTML scraping.
2. `monitor.py` reads both feeds and compares against the last saved snapshot in
   `state/`. Each collection uses a different detection mode:
   - **new-arrivals (`added` mode):** tracks the set of listed product IDs and
     alerts on a newly-appearing one. It's sorted newest-first and has no
     sold-out items, so we only read the top few pages (the collection has
     5000+ items — no need to fetch them all every few minutes).
   - **restocked-gems (`restocked` mode):** this collection *keeps sold-out
     items listed* and just flips their stock flag, so a restock is **not** a new
     product ID. Instead we track the set of **in-stock** product IDs and alert
     when one becomes available — i.e. a sold-out item coming back, or a new
     in-stock item. We read the whole collection (~1.2k items) since it isn't
     date-sorted.
3. What does **not** alert: a product selling out, or a product being removed
   from a collection. We only notify on new listings / items becoming available.
4. The Action commits the updated `state/*.json` back to the repo so the next
   run remembers what it has already seen.
5. **First run seeds silently** — it records the current state and sends one
   "monitoring started" message instead of flooding you with everything.

## One-time setup

### 1. Install the ntfy app and subscribe to your topic

- Install **ntfy** on your phone: [iOS](https://apps.apple.com/app/ntfy/id1625396347)
  / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
  (or F-Droid). No account needed.
- In the app: **+ → Subscribe to topic** and enter your secret topic name.
  A pre-generated random one for you:

  ```
  scentoria-kmiw08euftlucpwx
  ```

  > The topic name is your only "password". On ntfy's free tier topics aren't
  > reserved, so anyone who knows the name could read/spam it — that's why we use
  > a long random one. You can pick your own; just keep it unguessable.
- Test it from any terminal — you should get a push instantly:

  ```bash
  curl -d "It works!" ntfy.sh/scentoria-kmiw08euftlucpwx
  ```

### 2. Create the GitHub repo

`gh` isn't installed here, so create it on github.com (or install `gh`):

```bash
cd /Users/daksh/Documents/scentoria-monitor
git init
git add .
git commit -m "Initial commit: Scentoria stock monitor"
git branch -M main
# Create an EMPTY repo named scentoria-monitor on github.com, then:
git remote add origin https://github.com/<your-username>/scentoria-monitor.git
git push -u origin main
```

> **Make the repo public.** GitHub Actions minutes are *unlimited and free for
> public repos*. On a private repo the free tier is 2,000 min/month, which a
> 5-minute cron would blow past. Your code and the saved product IDs are
> harmless to expose; the topic name stays hidden in Secrets (next step).

### 3. Add your topic as a repository secret

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**

| Name | Value |
| --- | --- |
| `NTFY_TOPIC` | `scentoria-kmiw08euftlucpwx` (or your chosen topic) |

Optional secrets:
- `NTFY_SERVER` — only if you self-host ntfy (default `https://ntfy.sh`).
- `NTFY_TOKEN` — only if you later reserve the topic and protect it with auth.

### 4. Turn it on and test

- **Settings → Actions → General**: ensure Actions are enabled. (If GitHub
  prompts to enable scheduled workflows, confirm.)
- **Actions tab → "Scentoria stock monitor" → Run workflow** to trigger it
  manually. The first run seeds state and sends the "monitoring started" push.
- After that, the cron takes over automatically.

## Notes & tuning

- **Frequency:** change the `cron:` line in
  `.github/workflows/monitor.yml`. `*/5 * * * *` is every 5 min (GitHub's
  minimum). Scheduled runs can be delayed several minutes under GitHub load —
  normal and unavoidable on the free tier.
- **Local test run** (sends real notifications):
  ```bash
  NTFY_TOPIC=scentoria-kmiw08euftlucpwx python3 monitor.py
  ```
- **Reset / re-seed:** delete the relevant `state/*.json` and the next run
  re-seeds that collection silently.
- **If the state commit step fails** (`403`/permission denied on push): go to
  **Settings → Actions → General → Workflow permissions** and select
  *"Read and write permissions"*. (The workflow also declares this itself, which
  is usually enough.)
- **If the cron stops after ~60 days:** GitHub disables scheduled workflows in
  repos with no recent activity. The state commits usually keep it alive; if not,
  just open the Actions tab and re-enable, or push any commit.
- **Add more collections:** add entries to the `COLLECTIONS` dict in
  `monitor.py`.
