// Embed phase (online, no downtime): re-embed every chunk's stored `text`
// VERBATIM through the router into reembed/staging/<table>.jsonl.
// Read-only on LanceDB. Resumable: ids already staged are skipped.
//   node embed.js
"use strict";
const fs = require("fs");
const path = require("path");
const { DIR, TABLES, embed, openDb, allRows, log, sleep } = require("./common");

const BATCH = 8;          // AnythingLLM's own batch size; router now budgets per input
const INFLIGHT = 3;       // leaves EMBED_CONCURRENCY=4 headroom for live RAG queries

function staged(file) {
  const ids = new Set();
  if (!fs.existsSync(file)) return ids;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    if (!line) continue;
    try { ids.add(JSON.parse(line).id); } catch { /* torn last line from a crash: re-embed it */ }
  }
  return ids;
}

async function main() {
  fs.mkdirSync(path.join(DIR, "staging"), { recursive: true });
  const db = await openDb();
  for (const name of TABLES) {
    const file = path.join(DIR, "staging", `${name}.jsonl`);
    const done = staged(file);
    const t = await db.openTable(name);
    const rows = (await allRows(t, ["id", "text"])).filter((r) => !done.has(r.id));
    log("embed.log", `${name}: ${done.size} already staged, ${rows.length} to embed`);
    const fd = fs.openSync(file, "a");
    let next = 0, n = 0, failed = 0;
    const started = Date.now();
    async function worker() {
      while (next < rows.length) {
        const batch = rows.slice(next, next + BATCH);
        next += BATCH;
        try {
          const vecs = await embed(batch.map((r) => r.text));
          const lines = batch.map((r, i) => JSON.stringify({ id: r.id, v: vecs[i] })).join("\n") + "\n";
          fs.writeSync(fd, lines);
          n += batch.length;
        } catch (e) {
          failed += batch.length;
          log("embed.log", `${name}: batch FAILED (${batch.map((r) => r.id).join(",")}): ${e.message}`);
        }
        if (n % 800 < BATCH) {
          const rate = n / ((Date.now() - started) / 1000);
          log("embed.log", `${name}: ${n}/${rows.length} (${rate.toFixed(1)} chunks/s, failed ${failed})`);
        }
      }
    }
    await Promise.all(Array.from({ length: INFLIGHT }, worker));
    fs.closeSync(fd);
    log("embed.log", `${name}: DONE ${n} embedded, ${failed} failed, ${((Date.now() - started) / 60000).toFixed(1)} min`);
    await sleep(100);
  }
  log("embed.log", "ALL TABLES DONE");
}

main().catch((e) => { log("embed.log", `FATAL: ${e.message}`); process.exit(1); });
