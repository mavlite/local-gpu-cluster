// Validate staging against the live tables BEFORE any outage. Read-only.
//   node validate.js   -> exit 0 only if every check passes
// Checks per table: staged id set == table id set; dim 1024; all finite;
// cosine(old, new) for EVERY row: median ~0.9995 expected, min must be > 0.99
// (lower means the stored text was not what the embedder originally received).
"use strict";
const fs = require("fs");
const path = require("path");
const { DIR, TABLES, openDb, toArr, allRows, cosine, log } = require("./common");

const MIN_COS = 0.99;

function loadStaging(name) {
  const m = new Map();
  for (const line of fs.readFileSync(path.join(DIR, "staging", `${name}.jsonl`), "utf8").split("\n")) {
    if (!line) continue;
    const o = JSON.parse(line);
    m.set(o.id, o.v);
  }
  return m;
}

const q = (a, p) => a[Math.min(a.length - 1, Math.floor(p * (a.length - 1)))];

async function main() {
  const db = await openDb();
  let ok = true;
  const report = {};
  for (const name of TABLES) {
    const t = await db.openTable(name);
    const rows = await allRows(t, ["id", "vector"]);
    const st = loadStaging(name);
    const ids = new Set(rows.map((r) => r.id));
    const missing = rows.filter((r) => !st.has(r.id)).length;
    const extra = [...st.keys()].filter((k) => !ids.has(k)).length;
    let badDim = 0, nonFinite = 0;
    const cos = [], worst = [];
    for (const r of rows) {
      const v = st.get(r.id);
      if (!v) continue;
      if (v.length !== 1024) { badDim++; continue; }
      if (!v.every(Number.isFinite)) { nonFinite++; continue; }
      const c = cosine(toArr(r.vector), v);
      cos.push(c);
      if (c < MIN_COS) worst.push({ id: r.id, cos: c });
    }
    cos.sort((a, b) => a - b);
    const res = {
      rows: rows.length, staged: st.size, missing, extra, badDim, nonFinite,
      cos_min: cos[0], cos_p01: q(cos, 0.01), cos_median: q(cos, 0.5), cos_max: cos[cos.length - 1],
      below_min: worst.length, worst: worst.sort((a, b) => a.cos - b.cos).slice(0, 10),
    };
    const pass = missing === 0 && extra === 0 && badDim === 0 && nonFinite === 0 && worst.length === 0;
    ok = ok && pass;
    report[name] = { pass, ...res };
    log("validate.log", `${name}: ${pass ? "PASS" : "FAIL"} ${JSON.stringify(res)}`);
  }
  fs.writeFileSync(path.join(DIR, "validate.json"), JSON.stringify(report, null, 1));
  log("validate.log", ok ? "VALIDATION PASSED" : "VALIDATION FAILED");
  process.exit(ok ? 0 : 2);
}

main().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
