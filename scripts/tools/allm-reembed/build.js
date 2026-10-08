// Swap phase, step 1 (AnythingLLM STOPPED): build fresh tables in lancedb.new/
// with every column and the schema copied from the live tables, only `vector`
// replaced by the staged re-embedding. Never writes to lancedb/.
//   node build.js   -> exit 0 only if every table verifies
"use strict";
const fs = require("fs");
const path = require("path");
const { STORAGE, DIR, TABLES, openDb, toArr, allRows, cosine, log } = require("./common");

const NEW = path.join(STORAGE, "lancedb.new");

function loadStaging(name) {
  const m = new Map();
  for (const line of fs.readFileSync(path.join(DIR, "staging", `${name}.jsonl`), "utf8").split("\n")) {
    if (!line) continue;
    const o = JSON.parse(line);
    m.set(o.id, o.v);
  }
  return m;
}

async function main() {
  if (fs.existsSync(NEW)) throw new Error(`${NEW} already exists -- remove it deliberately first`);
  const oldDb = await openDb();
  const newDb = await openDb(NEW);
  let ok = true;
  for (const name of TABLES) {
    const old = await oldDb.openTable(name);
    const schema = await old.schema();
    const rows = await allRows(old);
    const st = loadStaging(name);
    if (st.size !== rows.length || rows.some((r) => !st.has(r.id)))
      throw new Error(`${name}: staging (${st.size}) does not cover the table (${rows.length}) -- data changed since validation?`);

    const data = rows.map((r) => {
      const o = {};
      for (const f of schema.fields) o[f.name] = f.name === "vector" ? st.get(r.id) : (r[f.name] ?? null);
      return o;
    });
    const t = await newDb.createTable(name, data, { schema, mode: "create" });

    // Verify: count, schema, and a 20-row spot check (text unchanged, vector == staged).
    const n = await t.countRows();
    const sameSchema = JSON.stringify((await t.schema()).fields.map((f) => [f.name, String(f.type)])) ===
                       JSON.stringify(schema.fields.map((f) => [f.name, String(f.type)]));
    const built = await allRows(t, ["id", "text", "vector"]);
    const byId = new Map(built.map((r) => [r.id, r]));
    let spotBad = 0;
    for (let i = 0; i < 20; i++) {
      const r = rows[Math.floor((i * rows.length) / 20)];
      const b = byId.get(r.id);
      if (!b || b.text !== r.text || cosine(toArr(b.vector), st.get(r.id)) < 0.999999) spotBad++;
    }
    const pass = n === rows.length && sameSchema && spotBad === 0;
    ok = ok && pass;
    log("build.log", `${name}: ${pass ? "PASS" : "FAIL"} rows old=${rows.length} new=${n} schemaSame=${sameSchema} spotBad=${spotBad}`);
  }
  log("build.log", ok ? "BUILD PASSED" : "BUILD FAILED");
  process.exit(ok ? 0 : 2);
}

main().catch((e) => { log("build.log", `FATAL: ${e.message}`); process.exit(1); });
