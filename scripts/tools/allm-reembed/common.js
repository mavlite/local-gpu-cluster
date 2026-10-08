// Shared helpers for the offline re-embed. Runs INSIDE the anythingllm container
// (node 18, @lancedb/lancedb 0.15 from /app/server/node_modules). The router key
// is read from the container's own env and never printed.
"use strict";
const path = require("path");
const fs = require("fs");
const lancedb = require("/app/server/node_modules/@lancedb/lancedb");

const STORAGE = "/app/server/storage";
const DIR = path.join(STORAGE, "reembed");
const TABLES = ["vcf-reference", "sdg-documentation"];

function cfg() {
  const base = process.env.EMBEDDING_BASE_PATH;            // http://192.168.6.153:8000/v1 (the router)
  const key = process.env.GENERIC_OPEN_AI_EMBEDDING_API_KEY;
  const model = process.env.EMBEDDING_MODEL_PREF;           // qwen3-embed
  if (!base || !key || !model) throw new Error("embedding env (base/key/model) not set in this container");
  return { base, key, model };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Embed texts VERBATIM through the router -- the exact path AnythingLLM's own
// ingest and query embeddings take (router appends the EOT token identically).
async function embed(texts, { tries = 6 } = {}) {
  const { base, key, model } = cfg();
  let last = "";
  for (let a = 0; a < tries; a++) {
    let r;
    try {
      r = await fetch(`${base}/embeddings`, {
        method: "POST",
        headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
        body: JSON.stringify({ model, input: texts }),
      });
    } catch (e) {
      last = `network: ${e.message}`;
      await sleep(2000 * (a + 1));
      continue;
    }
    if (r.status === 200) {
      const d = await r.json();
      const out = new Array(texts.length);
      for (const it of d.data) out[it.index] = it.embedding;
      if (out.some((v) => !Array.isArray(v))) throw new Error("embedding response missing an index");
      return out;
    }
    last = `HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`;
    if (r.status === 413 || r.status === 400) throw new Error(last);     // not retryable
    await sleep(r.status === 429 ? 15000 : 3000 * (a + 1));
  }
  throw new Error(`embed failed after ${tries} tries: ${last}`);
}

async function openDb(dir = path.join(STORAGE, "lancedb")) {
  return lancedb.connect(dir);
}

function toArr(v) {
  if (v == null) return null;
  if (typeof v.toArray === "function") return Array.from(v.toArray());
  return Array.from(v);
}

async function allRows(table, columns) {
  const n = await table.countRows();
  let q = table.query();
  if (columns) q = q.select(columns);
  return q.limit(n + 10).toArray();
}

function cosine(a, b) {
  let d = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { d += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  return d / (Math.sqrt(na) * Math.sqrt(nb));
}

// Deterministic PRNG so baseline and verify sample the SAME rows and queries.
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

function log(file, msg) {
  const line = `${new Date().toISOString()} ${msg}\n`;
  fs.appendFileSync(path.join(DIR, file), line);
  process.stdout.write(line);
}

module.exports = { STORAGE, DIR, TABLES, embed, openDb, toArr, allRows, cosine, rng, log, sleep };
