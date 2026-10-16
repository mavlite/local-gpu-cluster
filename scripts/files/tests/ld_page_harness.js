// Behavioural check for live_dashboard/static/index.html under a tiny DOM stub (run by test_ld_page.py).
// Usage: node ld_page_harness.js [path-to-index.html]; exit 0 = ok, 1 = failure (reason on stderr).
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const file = process.argv[2] || path.join(__dirname, "..", "live_dashboard", "static", "index.html");
const html = fs.readFileSync(file, "utf8");
const js = /<script>([\s\S]*)<\/script>/.exec(html)[1];
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]);

const els = {};
const mk = () => ({textContent: "", innerHTML: "", hidden: false, className: "", dataset: {}, style: {}, title: "",
  classList: {toggle() {}}, setAttribute(k, v) { this[k] = v; }});
ids.forEach(i => { els[i] = mk(); });
const sources = [];
class FakeES { constructor(u) { this.url = u; sources.push(this); } close() { this.closed = true; } }
const ctx = {
  document: {getElementById: i => els[i] || null},
  EventSource: FakeES, fetch: () => Promise.reject(new Error("offline")),
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {}, AbortSignal,
  console: {warn() {}, error: e => { throw new Error("console.error: " + e); }, log: console.log},
};
vm.createContext(ctx);
vm.runInContext(js + "\n;this.__render = render; this.__series = series;", ctx);
const render = ctx.__render;
const series = ctx.__series;

const ok = {ok: true, stale_since: null, error: null};
const now = Date.now() / 1000;
const good = {
  ts: now, sources: {slots: ok, metrics: ok, journal: {ok: false, stale_since: now - 9, error: "x"}, gpu: ok, host: ok, services: ok, escalation: ok},
  model: {profile: "p", n_ctx: 131072, slot_count: 3},
  slots: [{id: 0, state: "reading", progress: 0.5, prompt_tokens: 10, processed_tokens: 5, cached_tokens: 2, n_ctx: 131072, ctx_used: 5},
          {id: 1, state: "generating", output_tokens: 7, tok_s: 40.5}, {id: 2, state: "idle", last_done: now - 3}],
  queue: {deferred: 0}, speed: {gen_tok_s: 48.6, gen_live: true, prefill_tok_s: 600, prefill_live: false, gen_avg15: 44, prefill_avg15: 500,
          cache_reuse_pct15: 88, cache_reuse_pct: 85, mtp_accept: 0.8, mtp_pos: [0.9, 0.8, 0.7]},
  gpus: [{load_pct: 90, vram_used: 28e9, vram_total: 32e9, junction_c: 84, power_w: 176, power_cap_w: 180, link: "Gen4 x16"}, {}],
  fans: {pwm5_pct: 90, pwm6_pct: 90}, host: {cpu_pct: 14, mem_used: 24e9, mem_total: 124e9, disk_read_mbs: 0.1, arc_bytes: 11e9},
  services: {router: {ok: true}, embed: {ok: true, activity_per_min: 12}, rerank: {ok: false}, restart: {next_possible: now + 3600, stale_since: now}},
  escalation: {month: {spent_usd: 1, cap_usd: 150}, day: {spent_usd: 0.1, cap_usd: 7}, calls_left: {"claude-ask": 1, "claude-auto": 2, "claude-hard": 3},
               recent: [{alias: "claude-ask", input_tokens: 1, output_tokens: 2, cost_usd: 0.006}]},
  recent: [{status: "done", slow: true, slot: 1, finished: now, prompt_tokens: 5}, {status: "interrupted", slot: 2, started: now}], history_ok: true,
};
const hostile = {
  ts: null, slots: [null, {id: 1, state: "<img src=x onerror=1>"}, {id: 1.5, state: "idle"}, {id: "0", state: "idle"}], gpus: [null],
  escalation: {month: null, recent: [{alias: "<b>x</b>"}]}, recent: [{status: "<script>1</script>", slow: true}, null],
  speed: {mtp_pos: null, gen_tok_s: NaN, prefill_tok_s: Infinity}, services: {restart: {stale_since: 1}}, model: {profile: "<i>"},
};

const fail = m => { console.error("FAIL: " + m); process.exit(1); };
const dump = () => Object.entries(els).map(([k, e]) => [k, `${e.textContent}|${e.innerHTML}|${e.className}|${e.title}`]);
const bad = () => dump().filter(([, v]) => /\b(null|NaN|undefined)\b/.test(v)).map(([k]) => k);

render(good);
if (bad().length) fail("good snapshot rendered null/NaN/undefined in " + bad());
if (els["recent-body"].innerHTML.indexOf("slow") < 0) fail("slow row not shown");
if (!/stale/.test(els["svc-restart"].textContent)) fail("stale restart not marked");
if (els["esc-month"].textContent === "—") fail("escalation not shown");
if (!els["gen-label"] || !/\blive\b/.test(els["gen-label"].textContent)) fail("live generation speed not labelled live");
if (!els["prefill-label"] || !/last request/.test(els["prefill-label"].textContent)) fail("held prefill speed not labelled 'last request'");
const lastCache = series.cache[series.cache.length - 1];
if (!lastCache || lastCache[1] !== 85) fail("cache sparkline must plot the per-request reuse %, got " + JSON.stringify(lastCache));
if (els["cache-now"].textContent !== "88") fail("cache card big number must stay the 15-min average");
render({});
render(hostile);
if (bad().length) fail("hostile snapshot rendered null/NaN/undefined in " + bad());
const markup = Object.values(els).map(e => e.innerHTML).join(" ");   // textContent is inert by construction
if (/<img|<script|<b>|<i>/.test(markup)) fail("injected markup is live");
if (els["esc-month"].textContent !== "—" || els["esc-day"].textContent !== "—") fail("escalation values not cleared/guarded");
render({escalation: null});
if (els["esc-last"].textContent !== "—") fail("escalation not cleared when null");
if (sources.length !== 1) fail("expected exactly one EventSource, got " + sources.length);
console.log("ok");
