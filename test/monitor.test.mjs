// Offline tests for the Worker logic. Mocks global fetch (Shopify feed, .js
// endpoint, Telegram) and KV, then exercises the same scenarios as the original
// Python tests. Run: npm test
import assert from "node:assert";
import { processAdded, processRestocked, isNotifiableVariant } from "../src/monitor.js";

// --- mock state -----------------------------------------------------------
let CATALOG = {}; // collection -> [products]
let LIVE = {}; // handle -> [available variant id strings]
let SENT = []; // captured Telegram payloads
let FAIL_SEND = false; // when true, the Telegram POST responds non-2xx

function findByHandle(handle) {
  for (const products of Object.values(CATALOG)) {
    const p = products.find((x) => x.handle === handle);
    if (p) return p;
  }
  return null;
}

function resp(obj, ok = true) {
  return { ok, status: ok ? 200 : 500, json: async () => obj, text: async () => (typeof obj === "string" ? obj : JSON.stringify(obj)) };
}

global.fetch = async (url, opts = {}) => {
  url = String(url);
  if (url.includes("/collections/") && url.includes("products.json")) {
    const coll = url.includes("new-arrivals") ? "new-arrivals" : "restocked-gems";
    const page = Number(new URL(url).searchParams.get("page") || "1");
    return resp({ products: page === 1 ? CATALOG[coll] || [] : [] });
  }
  if (url.includes("/products/") && url.endsWith(".js")) {
    const handle = url.split("/products/")[1].replace(/\.js$/, "");
    const liveIds = LIVE[handle] || [];
    const prod = findByHandle(handle);
    const variants = (prod?.variants || []).map((v) => ({ id: v.id, available: liveIds.includes(String(v.id)) }));
    return resp({ variants });
  }
  if (opts.method === "POST") {
    SENT.push(JSON.parse(opts.body));
    return resp(FAIL_SEND ? "rate limited" : "ok", !FAIL_SEND);
  }
  return resp("unhandled", false);
};

function makeEnv() {
  const store = new Map();
  return {
    STORE_BASE_URL: "https://example.test",
    TG_BOT_TOKEN: "x",
    TG_CHAT_ID: "1",
    STATE: {
      get: async (k, type) => (store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null),
      put: async (k, v) => void store.set(k, v),
    },
    _store: store,
  };
}

const V = (id, name, available, price = "1000.00") => ({ id, option1: name, title: name, available, price });
let passed = 0;
function check(desc, cond) {
  assert.ok(cond, "FAILED: " + desc);
  console.log("  ok -", desc);
  passed++;
}

// --- unit: variant filter --------------------------------------------------
check("decant excluded", !isNotifiableVariant(V("x", "10 ML DECANT", true)));
check("typo DECAANT excluded", !isNotifiableVariant(V("x", "20 ML DECAANT", true)));
check("sample excluded", !isNotifiableVariant(V("x", "1.2 ML SAMPLE", true)));
check("miniature excluded", !isNotifiableVariant(V("x", "7 ML MINIATURE", true)));
check("tester included", isNotifiableVariant(V("x", "100 ML TESTER", true)));
check("retail included", isNotifiableVariant(V("x", "50 ML RETAIL", true)));

// --- restocked-gems scenarios ---------------------------------------------
async function restockedTests() {
  const env = makeEnv();
  const NAME = "restocked-gems";
  const P1 = { id: 1, title: "Musc", handle: "musc", variants: [V("d1", "10 ML DECANT", true), V("r1", "50 ML RETAIL", false), V("t1", "100 ML TESTER", false)] };
  const P2 = { id: 2, title: "Other", handle: "other", variants: [V("o1", "30 ML TESTER", true), V("o2", "100 ML RETAIL", false)] };
  CATALOG[NAME] = [P1, P2];
  const setv = (p, id, a) => { p.variants.find((v) => v.id === id).available = a; };
  const run = async () => { SENT = []; return processRestocked(env, NAME, "Restocked Gems", CATALOG[NAME]); };

  await run(); // seed
  check("seed: only o1 tracked (decant d1 excluded, r1/t1 sold out)", JSON.stringify(JSON.parse(env._store.get(NAME)).sort()) === JSON.stringify(["o1"]));
  check("seed: one monitoring-started notification", SENT.length === 1 && SENT[0].text.includes("Monitoring started"));

  setv(P1, "d1", false); setv(P1, "d1", true); LIVE.musc = ["d1"]; // decant toggles
  await run();
  check("decant flip -> no alerts", SENT.length === 0);

  setv(P1, "r1", true); LIVE.musc = ["d1", "r1"]; // retail back, .js confirms
  await run();
  check("retail restock confirmed -> 1 alert naming 50 ML RETAIL", SENT.length === 1 && SENT[0].text.includes("50 ML RETAIL"));

  setv(P2, "o2", true); LIVE.other = ["o1"]; // feed says o2 avail, .js does NOT confirm
  await run();
  check("unconfirmed candidate -> no alert", SENT.length === 0);
  check("unconfirmed candidate -> not saved to state", !JSON.parse(env._store.get(NAME)).includes("o2"));

  LIVE.other = ["o1", "o2"]; // now .js confirms
  await run();
  check("retry confirms o2 -> 1 alert", SENT.length === 1 && SENT[0].text.includes("100 ML RETAIL"));

  setv(P1, "t1", true);
  P1.variants.push(V("t2", "30 ML TESTER", true));
  LIVE.musc = ["d1", "r1", "t1", "t2"];
  await run();
  check("two variants same product -> 1 grouped alert", SENT.length === 1 && SENT[0].text.includes("100 ML TESTER") && SENT[0].text.includes("30 ML TESTER"));

  setv(P2, "o1", false); LIVE.other = ["o2"]; // tester sells out
  await run();
  check("sell-out -> no alert", SENT.length === 0);
  check("sell-out -> o1 dropped from state", !JSON.parse(env._store.get(NAME)).includes("o1"));

  // Pagination-flicker fix: a seen in-stock variant whose product drops out of
  // the feed must NOT be treated as sold-out, else it re-alerts on return.
  CATALOG[NAME] = [P1]; // P2 (with in-stock o2) momentarily missing from feed
  await run();
  check("restock: variant missing from feed -> not dropped", JSON.parse(env._store.get(NAME)).includes("o2"));
  check("restock: missing-from-feed -> no alert", SENT.length === 0);
  CATALOG[NAME] = [P1, P2]; LIVE.other = ["o2"]; // P2 returns, o2 still in stock
  await run();
  check("restock: reappearing in-stock variant -> no duplicate alert", SENT.length === 0);
}

// --- new-arrivals scenarios ------------------------------------------------
async function addedTests() {
  const env = makeEnv();
  const NAME = "new-arrivals";
  CATALOG[NAME] = [{ id: 50, title: "Arrival1", handle: "x", variants: [V("a1", "100 ML RETAIL", true)] }];
  const run = async () => { SENT = []; return processAdded(env, NAME, "New Arrivals", CATALOG[NAME]); };

  await run(); // seed
  check("added seed: one monitoring-started notification", SENT.length === 1);
  await run(); // no change
  check("added: no change -> no alert + no write detail", SENT.length === 0);
  CATALOG[NAME].unshift({ id: 99, title: "Arrival2", handle: "y", variants: [V("a2", "50 ML RETAIL", true)] });
  await run();
  check("added: new listing -> 1 alert", SENT.length === 1 && SENT[0].text.includes("Arrival2"));

  // Silent-drop fix: a failed send must NOT mark the product seen.
  FAIL_SEND = true;
  CATALOG[NAME].unshift({ id: 123, title: "Arrival3", handle: "z", variants: [V("a3", "50 ML RETAIL", true)] });
  await run();
  check("added: send fails -> attempted but not saved", SENT.length === 1 && !JSON.parse(env._store.get(NAME)).includes("123"));
  FAIL_SEND = false;
  await run();
  check("added: next run retries the dropped product -> delivered + saved", SENT.length === 1 && SENT[0].text.includes("Arrival3") && JSON.parse(env._store.get(NAME)).includes("123"));

  // Pagination-flicker fix: a seen product that drops out of the feed must stay
  // seen and must NOT re-notify when it reappears.
  const full = CATALOG[NAME];
  CATALOG[NAME] = full.filter((p) => p.id !== 99); // 99 momentarily missing
  await run();
  check("added: seen product missing from feed -> no alert, stays seen", SENT.length === 0 && JSON.parse(env._store.get(NAME)).includes("99"));
  CATALOG[NAME] = full; // 99 reappears in the feed
  await run();
  check("added: reappearing product -> no duplicate alert", SENT.length === 0);
}

await restockedTests();
await addedTests();
console.log(`\nAll ${passed} checks passed.`);
