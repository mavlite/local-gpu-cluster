// Retrieval probe: run once BEFORE the swap (baseline) and once AFTER (verify).
//   node retrieval.js <label>       -> reembed/retrieval-<label>.json
// Same seed => same queries and same self-retrieval sample both times.
// Mirrors AnythingLLM's search: vectorSearch(q).distanceType("cosine").limit(N).
"use strict";
const fs = require("fs");
const path = require("path");
const { DIR, TABLES, embed, openDb, toArr, allRows, rng, log } = require("./common");

const TOPN = 12;
const QUERIES_PER_TABLE = 40;
const SELF_PER_TABLE = 100;
const FIXED = {
  "vcf-reference": [
    "What's new in VCF 9.1.1",
    "VCF 9.1 upgrade prerequisites and vSAN capacity planning",
    "NSX Edge cluster compatible version before SDDC Manager permits upgrade",
    "vSAN storage policy failures to tolerate and stripe width configuration",
    "VCF licensing and license usage reporting",
    "memory tiering requirements NVMe",
  ],
  "sdg-documentation": [],
};

async function search(table, vec) {
  const res = await table.vectorSearch(vec).distanceType("cosine").limit(TOPN).toArray();
  return res.map((r) => ({ id: r.id, sim: +(1 - r._distance).toFixed(6) }));
}

async function main() {
  const label = process.argv[2];
  if (!label) throw new Error("usage: node retrieval.js <label>");
  const db = await openDb();
  const out = { label, at: new Date().toISOString(), tables: {} };
  for (const name of TABLES) {
    const t = await db.openTable(name);
    const rows = await allRows(t, ["id", "title", "text"]);
    rows.sort((a, b) => (a.id < b.id ? -1 : 1));                    // stable order for the seed
    const r = rng(name === "vcf-reference" ? 1234 : 5678);
    const titles = [...new Set(rows.map((x) => x.title).filter(Boolean))].sort();
    const queries = [...FIXED[name]];
    while (queries.length < FIXED[name].length + QUERIES_PER_TABLE && titles.length)
      queries.push(titles[Math.floor(r() * titles.length)]);
    const selfIdx = [];
    while (selfIdx.length < SELF_PER_TABLE) selfIdx.push(Math.floor(r() * rows.length));

    const q = [];
    for (let i = 0; i < queries.length; i += 8) {
      const batch = queries.slice(i, i + 8);
      const vecs = await embed(batch);
      for (let j = 0; j < batch.length; j++) q.push({ query: batch[j], top: await search(t, vecs[j]) });
    }
    const self = [];
    for (let i = 0; i < selfIdx.length; i += 8) {
      const batch = selfIdx.slice(i, i + 8).map((k) => rows[k]);
      const vecs = await embed(batch.map((x) => x.text));
      for (let j = 0; j < batch.length; j++) {
        const top = await search(t, vecs[j]);
        const rank = top.findIndex((h) => h.id === batch[j].id) + 1;   // 0 = not in top N
        self.push({ id: batch[j].id, rank, sim: top[0] ? top[0].sim : null, selfSim: rank ? top[rank - 1].sim : null });
      }
    }
    out.tables[name] = { rows: rows.length, queries: q, self };
    const r1 = self.filter((s) => s.rank === 1).length;
    const meanSelf = self.filter((s) => s.selfSim != null).reduce((a, s) => a + s.selfSim, 0) / Math.max(1, self.filter((s) => s.selfSim != null).length);
    log("retrieval.log", `${label} ${name}: rows=${rows.length} queries=${q.length} self rank1=${r1}/${self.length} mean self-sim=${meanSelf.toFixed(6)}`);
  }
  fs.writeFileSync(path.join(DIR, `retrieval-${label}.json`), JSON.stringify(out));
  log("retrieval.log", `${label} written`);
}

main().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });
