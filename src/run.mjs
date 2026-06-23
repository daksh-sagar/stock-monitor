#!/usr/bin/env node
// Node entry point for the GitHub Actions cron. Provides a file-backed `STATE`
// (JSON under state/, committed back to the repo by the workflow) and Telegram
// credentials from the environment, then runs the shared logic in monitor.js.
//
//   TG_BOT_TOKEN, TG_CHAT_ID  — required (GitHub Secrets in CI).
//
// Run locally:  TG_BOT_TOKEN=… TG_CHAT_ID=… node src/run.mjs

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runChecks } from "./monitor.js";

const STATE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "state");

const env = {
  TG_BOT_TOKEN: process.env.TG_BOT_TOKEN,
  TG_CHAT_ID: process.env.TG_CHAT_ID,
  STATE: {
    // Mirrors the Workers KV interface monitor.js expects.
    async get(name, type) {
      try {
        const txt = await fs.readFile(path.join(STATE_DIR, `${name}.json`), "utf8");
        return type === "json" ? JSON.parse(txt) : txt;
      } catch (e) {
        if (e.code === "ENOENT") return null; // first run for this collection
        throw e;
      }
    },
    async put(name, value) {
      await fs.mkdir(STATE_DIR, { recursive: true });
      await fs.writeFile(path.join(STATE_DIR, `${name}.json`), value);
    },
  },
};

if (!env.TG_BOT_TOKEN || !env.TG_CHAT_ID) {
  console.error("TG_BOT_TOKEN and TG_CHAT_ID environment variables are required.");
  process.exit(1);
}

// runChecks logs its own per-collection summary. Surface unexpected crashes as
// a non-zero exit so the Actions run is marked failed (state is still committed
// by the workflow's `if: always()` step).
try {
  await runChecks(env);
} catch (e) {
  console.error("run failed:", e);
  process.exit(1);
}
