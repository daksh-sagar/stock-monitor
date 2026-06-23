// Scentoria stock monitor — Cloudflare Worker (Cron Trigger).
//
// Watches two Shopify collections and pushes a phone notification via a
// Telegram bot:
//   - new-arrivals   -> alert when a NEW product is listed.
//   - restocked-gems -> alert when a tester/retail/partial/set variant comes
//                       BACK IN STOCK (decants/samples/miniatures/roll-ons are
//                       ignored). Restocks are confirmed in real time via the
//                       storefront .js endpoint before alerting.
//
// State (seen ids per collection) lives in Workers KV (binding: STATE). We read
// every run but only WRITE when the set changes, to stay under the free-tier
// 1,000 writes/day limit. Crucially, a product is only recorded as "seen" once
// its notification has actually been delivered — a failed send leaves it unseen
// so it is retried next run instead of being silently lost.
//
// Required secrets: env.TG_BOT_TOKEN, env.TG_CHAT_ID.
// Optional secret:  env.TRIGGER_KEY (enables the manual ?key=… HTTP trigger).
//
// Why Telegram and not ntfy: ntfy.sh meters its free quota per *IP* ("basis":
// "ip"), and Cloudflare Workers egress from shared IPs — so the daily quota is
// burned through by unrelated tenants and every send 429s. The Telegram Bot API
// is keyed by bot token + chat, independent of source IP, with no daily cap.

const COLLECTIONS = [
  {
    name: "new-arrivals",
    url: "https://scentoria.co.in/collections/new-arrivals/products.json",
    maxPages: 3, // sorted newest-first; new items land on page 1
    mode: "added",
  },
  {
    name: "restocked-gems",
    url: "https://scentoria.co.in/collections/restocked-gems/products.json",
    maxPages: null, // read the whole collection
    mode: "restocked",
  },
];

// Variant types we IGNORE for restock alerts (small try-size formats). Matched
// case-insensitively as a substring of the variant name. "DECAANT" covers a
// real typo present in the store's data.
const EXCLUDE_VARIANT_TYPES = [
  "DECANT", "DECAANT", "SAMPLE", "MINIATURE", "ROLL ON", "ROLL-ON", "ROLLON",
];

const PAGE_SIZE = 250;
const HARD_PAGE_CAP = 25; // safety stop for full fetches
const MAX_INDIVIDUAL = 8; // beyond this, send one summary instead of N pushes
const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

const PRODUCT_URL = (h) => `https://scentoria.co.in/products/${h}`;
const COLLECTION_URL = (c) => `https://scentoria.co.in/collections/${c}`;

// ---------------------------------------------------------------------------
// Helpers

function titleCase(name) {
  return name.split("-").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}

function variantName(v) {
  return v.option1 || v.title || "?";
}

function isNotifiableVariant(v) {
  const name = variantName(v).toUpperCase();
  return !EXCLUDE_VARIANT_TYPES.some((t) => name.includes(t));
}

function productPrice(p) {
  const v = (p.variants || [])[0];
  return v && v.price ? v.price : null;
}

// Escape the small set of characters Telegram's HTML parse mode treats specially.
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function setsEqual(a, b) {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

async function fetchProducts(url, maxPages) {
  const limit = maxPages == null ? HARD_PAGE_CAP : maxPages;
  const products = [];
  for (let page = 1; page <= limit; page++) {
    const r = await fetch(`${url}?limit=${PAGE_SIZE}&page=${page}`, {
      headers: { "User-Agent": UA },
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${url} page ${page}`);
    const batch = (await r.json()).products || [];
    if (batch.length === 0) break;
    products.push(...batch);
    if (batch.length < PAGE_SIZE) break;
  }
  return products;
}

// Real-time set of in-stock variant-id strings from the storefront .js endpoint
// (same data the live page uses). Returns null if the check itself failed, so
// the caller can retry next run rather than act on a guess.
async function liveAvailableVariants(handle) {
  try {
    const r = await fetch(`${PRODUCT_URL(handle)}.js`, { headers: { "User-Agent": UA } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    return new Set((data.variants || []).filter((v) => v.available).map((v) => String(v.id)));
  } catch (e) {
    console.error(`live-check failed for ${handle}: ${e}`);
    return null;
  }
}

async function loadSeen(env, name) {
  const arr = await env.STATE.get(name, "json"); // null if missing (first run)
  return arr === null ? null : new Set(arr);
}

async function saveSeen(env, name, ids) {
  await env.STATE.put(name, JSON.stringify([...ids]));
}

// Send one message via the Telegram Bot API. Returns true only if Telegram
// accepted it (HTTP 2xx) — callers rely on this to decide whether to record the
// item as seen. `preview` shows a rich link card (product photo) for the URL.
async function sendTelegram(env, { text, url, preview = false }) {
  if (!env.TG_BOT_TOKEN || !env.TG_CHAT_ID) {
    console.error("TG_BOT_TOKEN/TG_CHAT_ID not set; cannot notify.");
    return false;
  }
  const api = `https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`;
  const payload = {
    chat_id: env.TG_CHAT_ID,
    text,
    parse_mode: "HTML",
    link_preview_options:
      preview && url ? { url, prefer_large_media: true } : { is_disabled: true },
  };
  try {
    const r = await fetch(api, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) console.error(`telegram HTTP ${r.status}: ${await r.text()}`);
    return r.ok;
  } catch (e) {
    console.error(`telegram error: ${e}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Notifications
//
// Each notify* function returns the Set of product-id strings whose alert was
// actually delivered. For the summary path that's all-or-nothing (one message
// covers the whole batch); for the per-product path it's exactly the ones that
// succeeded. The caller records only delivered ids as seen.

async function notifyAdded(env, collection, label, products) {
  if (products.length > MAX_INDIVIDUAL) {
    const preview = products.slice(0, 5).map((p) => p.title).join(", ");
    const ok = await sendTelegram(env, {
      text:
        `🛍️ <b>${products.length} new in ${esc(label)}</b>\n` +
        `${esc(preview)} … and ${products.length - 5} more.\n` +
        `<a href="${COLLECTION_URL(collection)}">View collection</a>`,
    });
    return ok ? new Set(products.map((p) => String(p.id))) : new Set();
  }
  const results = await Promise.all(
    products.map(async (p) => {
      const price = productPrice(p);
      const head = price ? `₹${esc(price)}` : "New product";
      const link = PRODUCT_URL(p.handle || "");
      const ok = await sendTelegram(env, {
        text:
          `🛍️ <b><a href="${link}">${esc(p.title || "Product")}</a></b>\n` +
          `${head} · in ${esc(label)}`,
        url: link,
        preview: true,
      });
      return ok ? String(p.id) : null;
    })
  );
  return new Set(results.filter(Boolean));
}

// `items` is a list of { p, v }. Group by product so a product restocking
// several variants becomes one notification that names the variants.
async function notifyRestocked(env, collection, label, items) {
  const byProduct = new Map();
  for (const { p, v } of items) {
    if (!byProduct.has(p.id)) byProduct.set(p.id, { p, vs: [] });
    byProduct.get(p.id).vs.push(v);
  }
  const groups = [...byProduct.values()];

  if (groups.length > MAX_INDIVIDUAL) {
    const preview = groups.slice(0, 5).map((g) => g.p.title).join(", ");
    const ok = await sendTelegram(env, {
      text:
        `🔥 <b>${groups.length} back in stock in ${esc(label)}</b>\n` +
        `${esc(preview)} … and ${groups.length - 5} more.\n` +
        `<a href="${COLLECTION_URL(collection)}">View collection</a>`,
    });
    return ok ? new Set(groups.map((g) => String(g.p.id))) : new Set();
  }

  const results = await Promise.all(
    groups.map(async ({ p, vs }) => {
      const names = vs.map(variantName).join(", ");
      const prices = vs.map((v) => v.price).filter(Boolean);
      let priceLine = "";
      if (prices.length) {
        const cheapest = prices.reduce((a, b) => (Number(b) < Number(a) ? b : a));
        priceLine = prices.length === 1 ? ` · ₹${cheapest}` : ` · from ₹${cheapest}`;
      }
      const link = PRODUCT_URL(p.handle || "");
      const ok = await sendTelegram(env, {
        text:
          `🔥 <b><a href="${link}">${esc(p.title || "Product")}</a></b>\n` +
          `Back in stock: ${esc(names + priceLine)}\nin ${esc(label)}`,
        url: link,
        preview: true,
      });
      return ok ? String(p.id) : null;
    })
  );
  return new Set(results.filter(Boolean));
}

// ---------------------------------------------------------------------------
// Per-collection processing

async function processAdded(env, name, label, products) {
  const seen = await loadSeen(env, name);

  if (seen === null) {
    const currentIds = products.map((p) => String(p.id));
    await saveSeen(env, name, currentIds);
    await sendTelegram(env, {
      text:
        `👀 <b>Monitoring started: ${esc(label)}</b>\n` +
        `Now tracking ${currentIds.length} products. You'll be alerted on new additions.`,
    });
    return `[${name}] first run — seeded ${currentIds.length} products.`;
  }

  const fresh = products.filter((p) => !seen.has(String(p.id)));
  let delivered = new Set();
  if (fresh.length) delivered = await notifyAdded(env, name, label, fresh);

  // New seen = (previously-seen ids still in the window) + (freshly delivered).
  // Fresh items whose push failed are omitted, so they retry next run. Ids that
  // fell off the tracked pages naturally drop out.
  const newSeen = new Set();
  for (const p of products) {
    const id = String(p.id);
    if (seen.has(id) || delivered.has(id)) newSeen.add(id);
  }
  if (!setsEqual(newSeen, seen)) await saveSeen(env, name, newSeen);

  const failed = fresh.length - delivered.size;
  return `[${name}] ${fresh.length} new (${delivered.size} delivered${failed ? `, ${failed} retrying` : ""}); ${products.length} in window.`;
}

async function processRestocked(env, name, label, products) {
  // Candidate in-stock notifiable variants from the (cached) collection feed.
  const candidates = new Map(); // variant-id -> { p, v }
  for (const p of products) {
    for (const v of p.variants || []) {
      if (v.available && isNotifiableVariant(v)) candidates.set(String(v.id), { p, v });
    }
  }

  // Health guard: this collection always has plenty in stock. If the feed
  // reports none (e.g. the `available` field vanished), don't wipe state.
  if (candidates.size === 0) {
    console.error(`[${name}] 0 in-stock notifiable variants — suspicious, skipping.`);
    return `[${name}] 0 in-stock notifiable variants — skipped.`;
  }

  const seen = await loadSeen(env, name);
  if (seen === null) {
    await saveSeen(env, name, [...candidates.keys()]);
    await sendTelegram(env, {
      text:
        `👀 <b>Monitoring started: ${esc(label)}</b>\n` +
        `Watching for restocks of testers, retail, partials & sets ` +
        `(decants, samples & miniatures ignored). Tracking ${candidates.size} ` +
        `in-stock variants — you'll be alerted when more come back.`,
    });
    return `[${name}] first run — seeded ${candidates.size} in-stock variants.`;
  }

  const freshIds = [...candidates.keys()].filter((id) => !seen.has(id));

  // Confirm candidate restocks in real time (.js), one fetch per product.
  const byHandle = new Map();
  for (const id of freshIds) {
    const { p } = candidates.get(id);
    if (!byHandle.has(p.handle)) byHandle.set(p.handle, []);
    byHandle.get(p.handle).push(id);
  }
  const confirmedIds = new Set();
  const confirmed = [];
  for (const [handle, ids] of byHandle) {
    const live = await liveAvailableVariants(handle);
    if (live === null) continue; // couldn't confirm — retry next run
    for (const id of ids) {
      if (live.has(id)) {
        confirmedIds.add(id);
        confirmed.push(candidates.get(id));
      }
    }
  }

  let delivered = new Set(); // product-id strings whose alert was delivered
  if (confirmed.length) delivered = await notifyRestocked(env, name, label, confirmed);

  // Keep previously-seen variants still in stock + newly confirmed ones whose
  // alert actually delivered. Unconfirmed/undelivered stay out for a retry.
  const newSeen = new Set();
  for (const id of seen) if (candidates.has(id)) newSeen.add(id);
  let deliveredVariants = 0;
  for (const id of confirmedIds) {
    if (delivered.has(String(candidates.get(id).p.id))) {
      newSeen.add(id);
      deliveredVariants++;
    }
  }
  if (!setsEqual(newSeen, seen)) await saveSeen(env, name, newSeen);

  return `[${name}] ${confirmed.length} confirmed restock(s) (${deliveredVariants} delivered); ${candidates.size} in stock; ${freshIds.length} candidate(s).`;
}

// ---------------------------------------------------------------------------
// Entry points

async function runChecks(env) {
  const log = [];
  for (const c of COLLECTIONS) {
    const label = titleCase(c.name);
    let products;
    try {
      products = await fetchProducts(c.url, c.maxPages);
    } catch (e) {
      log.push(`[${c.name}] fetch failed: ${e}`);
      console.error(log[log.length - 1]);
      continue;
    }
    if (products.length === 0) {
      log.push(`[${c.name}] 0 products — skipped (transient?).`);
      continue;
    }
    const fn = c.mode === "restocked" ? processRestocked : processAdded;
    log.push(await fn(env, c.name, label, products));
  }
  const out = log.join("\n");
  console.log(out);
  return out;
}

export default {
  async scheduled(event, env, ctx) {
    await runChecks(env);
  },

  // Manual trigger / health check: GET ...?key=<your TRIGGER_KEY>
  // Disabled unless the TRIGGER_KEY secret is set.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.TRIGGER_KEY || url.searchParams.get("key") !== env.TRIGGER_KEY) {
      return new Response(
        "forbidden — set the TRIGGER_KEY secret and append ?key=<TRIGGER_KEY>\n",
        { status: 403 }
      );
    }
    // ?test=1 -> send a one-off push via the real notify path (verifies delivery).
    if (url.searchParams.get("test") === "1") {
      const ok = await sendTelegram(env, {
        text: "✅ <b>Scentoria monitor test</b>\nIf you can read this in Telegram, notifications work.",
      });
      return new Response(
        ok ? "test push sent ✅\n" : "test push FAILED — run `npx wrangler tail` for details\n"
      );
    }
    const out = await runChecks(env);
    return new Response(out + "\n", { headers: { "content-type": "text/plain" } });
  },
};

// Exported for tests.
export {
  isNotifiableVariant,
  processAdded,
  processRestocked,
  runChecks,
  setsEqual,
  COLLECTIONS,
};
