# Live Inference Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cluster monitor on `http://192.168.6.175:8888/` with a Strata-style live view of the GPU server. It shows slot states, prefill progress, generation and prefill speed, GPU and host stats, services and the Claude budget. It keeps every finished request in SQLite for 30 days.

**Architecture:**
- A standard-library Python package, `scripts/files/live_dashboard/`, runs on the Proxmox host.
- **Collectors:**
  - poll llama-server `/slots` and `/metrics`, host sysfs and `/proc` every second;
  - stream the chat journal through `pct exec 151 -- journalctl -f`;
  - poll router, embed, rerank and the restart timer every 5–60 s.
- **A pure `Tracker`** turns journal events plus slot snapshots into live slot cards and finished request rows.
- **A single-writer SQLite store** keeps the rows for 30 days.
- **A `ThreadingHTTPServer`** serves one static page, a server-sent-events stream (one snapshot per second) and small JSON endpoints.

**Tech Stack:**
- Python 3.13 standard library only on the host (`http.server`, `sqlite3`, `subprocess`, `threading`, `urllib`).
- Vanilla JS and inline SVG in one HTML file.
- Bash for the deploy script.
- pytest for tests.

**Spec:** `docs/superpowers/specs/2026-10-16-live-dashboard-design.md`. Approved mock-up: `docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html` (Task 1 commits it there).

## Global Constraints

**Runtime and deployment**
- Python standard library only, with no third-party imports in `scripts/files/live_dashboard/` (tests may use pytest). Host Python 3.13.5, SQLite 3.46.1.
- Listen on `192.168.6.175` (`bind_host`). Port `8889` during the side-by-side run and `8888` after cut-over (`bind_port`). No authentication, read-only. No endpoint changes state.
- Responses carry numbers and fixed labels only. They never include prompt or response text, request bodies, keys or env values.
- **Chat server:** `http://192.168.6.151:8080`, embed `:8082`, rerank `:8083`. `/slots` and `/metrics` need `Authorization: Bearer <key>`, and the key is the same for all three.
  - The key is read on the host from `/proc/<pid>/cmdline` of the `llama-server` process whose argv has `--port 8080`, as the value after `--api-key`.
  - It is held in memory only and re-read after any 401. Never logged.
- **Router:** `http://192.168.6.153:8000/healthz` needs no auth. `GET /escalation/budget` uses `Authorization: Bearer <ROUTER_API_KEY>`, read on the host from `/proc/<pid>/environ` of the router's uvicorn process (argv contains `uvicorn` and `app:app`). Memory only.
- **Journal:** `pct exec 151 -- journalctl -u llamacpp-chat -f -o cat -n 0`. Lines look like `MMM.SS.mmm.uuu L tag message`; the prefix is time since server start.

**Hardware and polling**
- **GPUs:** the V620s are at PCI bus `0000:03:00.0` and `0000:07:00.0`. A card dir is found by `PCI_SLOT_NAME=` in `device/uevent`, never by card number. Units:
  - VRAM is in bytes;
  - temperature is in millidegrees C (`temp2_input` = junction);
  - power is in µW (`power1_average`, `power1_cap`).
- **Fans:** `pwm5`/`pwm6` (0–255) of the hwmon whose `name` is `nct6798`.
- **Cadences:**
  - slots, metrics, GPU and host: 1 s;
  - services (router `healthz`, embed/rerank `/metrics`): 5 s;
  - restart timer: 60 s;
  - escalation budget: 30 s.
- **Chat restart timer:** `systemctl show llamacpp-chat-restart.timer -p NextElapseUSecRealtime` inside LXC 151, plus the stamp file `/var/lib/llamacpp/last-proactive-restart` (epoch seconds, read on the host via `/proc/<chat pid>/root/...`). Next possible restart = max(stamp + 43200, next fire).

**History and limits**
- Rows kept 30 days (`retention_days`). Sparkline window 15 min (`sample_window_s` = 900). At most 8 stream clients (`max_stream_clients`).
- `GET /api/requests?limit=` capped at 500; `GET /api/history?days=` capped at 30.
- **Context bands** (prompt tokens): `0-8K` <8192, `8-32K` <32768, `32-64K` <65536, `64-96K` <98304, `96-128K` otherwise.
- **"Slow" request:** newly processed prompt tokens > 5000.
- **Request statuses:** `reading`, `generating`, `done`, `cancelled`, `approx`, `interrupted`.

**Repo conventions**
- Conventional commits with no `Co-Authored-By`. Commit by explicit path as a standalone `git commit`.
- Tests: `python -m pytest scripts/files/tests -q`. `bash -n` on every edited `.sh`.

## Review Focus

These are the failure modes the spec implies but a straight test suite would miss. Each has a test in its owning task.

1. **Chat restarts mid-request.** The journal shows `llama_server: initializing ...` while a row is open. The row must be written as `interrupted`, the slots must show `restarting` until `listening on`, and task ids starting over from 0 must not collide with old rows. Test: Task 3.
2. **Journal stream dies** (pct exits or the LXC restarts). The follower reconnects with backoff. Meanwhile the `/slots` going-idle transition produces `approx` rows, and the `journal` source shows stale. Tests: Tasks 3 and 5.
3. **Key rotates or llama-server restarts with a new pid.** The first 401 triggers a re-read of the key from `/proc`, with no crash and no stale card stuck as live. Test: Task 5.
4. **A browser leaves a stream open forever, or many tabs open.** The ninth stream client gets 503. Closed connections free their slot, and a write to a dead socket doesn't kill the server. Test: Task 6.
5. **A GPU sysfs file is missing or unreadable** (driver reload, card reset). That card's fields become `null`, the `gpu` source reports the error, and the other card and all other sources keep updating. Test: Task 2.

## File structure

| Path | Responsibility |
|---|---|
| `scripts/files/live_dashboard/__init__.py` | Package marker |
| `scripts/files/live_dashboard/parsers.py` | Pure parsers: journal lines, Prometheus text, `/slots`, GPU sysfs, `/proc` CPU/mem/disk/ARC, timer output |
| `scripts/files/live_dashboard/tracker.py` | Pure request state machine: journal events + slot snapshots → live slot views + finished rows |
| `scripts/files/live_dashboard/history.py` | SQLite store with a single writer thread, recent rows, context-band aggregates, prune |
| `scripts/files/live_dashboard/collectors.py` | Key discovery, HTTP fetch, journal follower, samplers, the `Hub` that assembles one snapshot per second, ring buffers |
| `scripts/files/live_dashboard/server.py` | HTTP routes, SSE stream with client cap |
| `scripts/files/live_dashboard/main.py` | Config load, wiring, run (`python3 -m live_dashboard.main --config ...`) |
| `scripts/files/live_dashboard/static/index.html` | The page, built from the approved mock-up |
| `scripts/files/live-dashboard.service` | systemd unit |
| `scripts/64-live-dashboard.sh` | Deploy, side-by-side, cut-over and rollback |
| `scripts/files/tests/fixtures/live_dashboard/` | Recorded fixtures (redacted) |
| `scripts/files/tests/test_ld_*.py` | Tests per module |
| `docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html` | The approved mock-up, committed for reference |

**Snapshot schema** (one SSE `data:` JSON per second; Tasks 5–7 depend on it exactly):

```
{"ts": float,
 "sources": {"slots"|"metrics"|"journal"|"gpu"|"host"|"services"|"escalation": {"ok": bool, "stale_since": float|null, "error": str|null}},
 "model": {"profile": str|null, "n_ctx": int|null, "slot_count": int},
 "slots": [{"id": int, "state": "idle"|"reading"|"generating"|"restarting", "task": int|null,
            "progress": float|null, "prompt_tokens": int|null, "processed_tokens": int|null,
            "cached_tokens": int|null, "output_tokens": int|null, "tok_s": float|null,
            "ctx_used": int|null, "n_ctx": int|null, "started": float|null, "last_done": float|null}],
 "queue": {"deferred": int|null, "processing": int|null},
 "speed": {"gen_tok_s": float|null, "prefill_tok_s": float|null, "gen_avg15": float|null,
           "prefill_avg15": float|null, "cache_reuse_pct15": float|null,
           "mtp_accept": float|null, "mtp_pos": [float|null, float|null, float|null]},
 "gpus": [{"bus": str, "load_pct": float|null, "vram_used": int|null, "vram_total": int|null,
           "junction_c": float|null, "power_w": float|null, "power_cap_w": float|null, "link": str|null}],
 "fans": {"pwm5_pct": float|null, "pwm6_pct": float|null},
 "host": {"cpu_pct": float|null, "mem_used": int|null, "mem_total": int|null,
          "disk_read_mbs": float|null, "disk_write_mbs": float|null, "arc_bytes": int|null, "arc_max": int|null},
 "services": {"router": {"ok": bool, "upstream": dict, "profile": str|null},
              "embed": {"ok": bool, "activity_per_min": float|null},
              "rerank": {"ok": bool, "activity_per_min": float|null},
              "restart": {"next_possible": float|null}},
 "escalation": dict|null,
 "recent": [row, ...],   // newest first, max 20, open rows first
 "history_ok": bool}
```

**Row schema** (live and stored):

```
{"key": str, "server_run": float, "slot": int, "task": int, "started": float, "finished": float|null,
 "status": str, "prompt_tokens": int|null, "new_prompt_tokens": int|null, "cached_tokens": int|null,
 "output_tokens": int|null, "prefill_tok_s": float|null, "gen_tok_s": float|null,
 "duration_s": float|null, "draft_accept": float|null, "slow": bool}
```

---

### Task 1: Fixtures, mock-up and the parsers

**Files:**
- Create:
  - `scripts/files/live_dashboard/__init__.py`
  - `scripts/files/live_dashboard/parsers.py`
  - `scripts/files/tests/test_ld_parsers.py`
  - `scripts/files/tests/fixtures/live_dashboard/*` (copied)
  - `docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html` (copied)

**Interfaces:**
- Produces, all pure:
  - `parse_journal_line(line: str) -> dict | None`
  - `parse_prometheus(text: str) -> dict[str, float]`
  - `parse_slots(data: list) -> list[dict]`
  - `find_gpu_dirs(drm_root: str, buses: list[str]) -> dict[str, str]`
  - `read_gpu(device_dir: str) -> dict`
  - `read_fans(hwmon_root: str, chip: str = "nct6798") -> dict`
  - `cpu_times(stat_text: str) -> tuple[int, int]` (busy, total)
  - `mem_from_meminfo(text: str) -> tuple[int, int]` (used_bytes, total_bytes)
  - `disk_sectors(diskstats_text: str, devices: list[str]) -> tuple[int, int]` (read_sectors, write_sectors)
  - `arc_from_arcstats(text: str) -> tuple[int, int]` (size, c_max)
  - `parse_next_elapse(text: str) -> float | None`

Journal event dicts carry `kind` and `since_start_s`, plus, by kind:
- `start`: `slot`, `task`
- `prefill`: `slot`, `task`, `n`, `progress`, `tok_s`
- `gen`: `slot`, `task`, `n`, `tok_s`
- `prompt_eval`: `slot`, `task`, `ms`, `n`
- `eval`: `slot`, `task`, `ms`, `n`
- `total`: `slot`, `task`, `ms`, `n`
- `draft`: `slot`, `task`, `rate`, `accepted`, `generated`
- `release`: `slot`, `task`, `n`
- `cancel`: `task`
- `server_start`, `server_ready`: no extra fields

- [ ] **Step 1: Copy the recorded fixtures and the mock-up into the repo, then check for secrets**

```bash
SRC="/c/Users/willi/AppData/Local/Temp/claude/c--Users-willi-Documents-GitHub-local-gpu-cluster/4bc0295a-452b-4bdf-ba9b-786ecd9a37c2/scratchpad/dash/fixtures"
mkdir -p scripts/files/tests/fixtures/live_dashboard
cp "$SRC"/*.txt "$SRC"/*.json scripts/files/tests/fixtures/live_dashboard/
cp .superpowers/brainstorm/136220-1792114066/content/live-dashboard-v1.html docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html
grep -rnE -- '--api-key [^<]|Bearer [A-Za-z0-9]{16,}|ROUTER_API_KEY=[^ <]' scripts/files/tests/fixtures/live_dashboard/ || echo "no secrets"
```

Expected: `no secrets`. If anything matches, stop and redact it before going on.

- [ ] **Step 2: Write the failing tests** in `scripts/files/tests/test_ld_parsers.py`

```python
"""live_dashboard.parsers -- recorded fixtures from the live cluster (2026-10-16, llama.cpp b11578)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
FX = os.path.join(HERE, "fixtures", "live_dashboard")

from live_dashboard import parsers as P  # noqa: E402


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


def journal_events():
    return [e for e in (P.parse_journal_line(l) for l in fx("journal_chat_last10min_raw.txt").splitlines()) if e]


def test_journal_request_block_parses_in_order():
    kinds = [e["kind"] for e in journal_events()]
    assert kinds == ["start", "prefill", "prefill", "prefill", "prompt_eval", "eval", "total", "draft", "release"]


def test_journal_values():
    ev = {e["kind"]: e for e in journal_events()}
    assert ev["start"]["slot"] == 1 and ev["start"]["task"] == 4772
    assert ev["prompt_eval"]["n"] == 3727 and abs(ev["prompt_eval"]["ms"] - 6154.43) < 1e-6
    assert ev["eval"]["n"] == 23 and abs(ev["eval"]["ms"] - 459.73) < 1e-6
    assert ev["total"]["n"] == 3750
    assert ev["draft"]["accepted"] == 16 and ev["draft"]["generated"] == 21
    assert ev["release"]["n"] == 3750
    p = [e for e in journal_events() if e["kind"] == "prefill"]
    assert [x["n"] for x in p] == [2048, 3211, 3723] and p[-1]["progress"] == 1.0
    assert abs(ev["start"]["since_start_s"] - (552 * 60 + 57.641854)) < 1e-6


def test_journal_noise_and_unauthorized_are_ignored():
    assert P.parse_journal_line("552.57.632.231 W srv    operator(): unauthorized: Invalid API Key") is None
    assert P.parse_journal_line("553.06.878.025 I slot print_timing: id  1 | task 4772 |    graphs reused =       4570") is None
    assert P.parse_journal_line("") is None


def test_journal_cancel_and_server_lifecycle():
    assert P.parse_journal_line("12.00.000.001 W srv          stop: cancel task, id_task = 77") == {
        "kind": "cancel", "since_start_s": 720.000001, "task": 77}
    starts = [e for e in (P.parse_journal_line(l) for l in fx("journal_chat_server_start.txt").splitlines()) if e]
    kinds = [e["kind"] for e in starts]
    assert "server_start" in kinds and "server_ready" in kinds
    assert kinds.index("server_start") < kinds.index("server_ready")


def test_prometheus_counters_and_labels():
    m = P.parse_prometheus(
        "# HELP x\nllamacpp:prompt_tokens_total 72775\nllamacpp:requests_deferred 0\n"
        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 3761\n')
    assert m["llamacpp:prompt_tokens_total"] == 72775.0 and m["llamacpp:requests_deferred"] == 0.0
    assert m['llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"}'] == 3761.0


def test_slots_normalised():
    d = json.loads(fx("llama_slots_metrics_session.json"))
    s = P.parse_slots(d["samples"][5]["slots"])
    one = [x for x in s if x["id"] == 1][0]
    assert set(one) == {"id", "n_ctx", "is_processing", "id_task", "n_prompt_tokens", "processed", "cache", "n_decoded"}
    assert one["is_processing"] is True and one["id_task"] == 4772 and one["n_ctx"] == 131072


def _materialise(tmp_path, mapping):
    for path, content in mapping.items():
        p = tmp_path / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def test_gpu_dirs_found_by_bus_and_read(tmp_path):
    root = _materialise(tmp_path, json.loads(fx("gpu_sysfs.json")))
    dirs = P.find_gpu_dirs(os.path.join(root, "sys", "class", "drm"), ["0000:03:00.0", "0000:07:00.0"])
    assert set(dirs) == {"0000:03:00.0", "0000:07:00.0"}
    g = P.read_gpu(dirs["0000:03:00.0"])
    assert g["vram_total"] == 32195477504 and g["vram_used"] == 28283002880
    assert g["junction_c"] == 33.0 and g["power_w"] == 12.0 and g["power_cap_w"] == 180.0
    assert g["load_pct"] == 0.0 and g["link"] == "16.0 GT/s x16" and g["error"] is None


def test_gpu_missing_file_gives_null_field_and_error(tmp_path):
    root = _materialise(tmp_path, json.loads(fx("gpu_sysfs.json")))
    dirs = P.find_gpu_dirs(os.path.join(root, "sys", "class", "drm"), ["0000:03:00.0"])
    os.remove(os.path.join(dirs["0000:03:00.0"], "gpu_busy_percent"))
    g = P.read_gpu(dirs["0000:03:00.0"])
    assert g["load_pct"] is None and g["vram_total"] == 32195477504 and "gpu_busy_percent" in g["error"]


def test_fans(tmp_path):
    hw = tmp_path / "hwmon"
    for name, chip in (("hwmon3", "k10temp"), ("hwmon14", "nct6798")):
        (hw / name).mkdir(parents=True)
        (hw / name / "name").write_text(chip + "\n")
    (hw / "hwmon14" / "pwm5").write_text("64\n")
    (hw / "hwmon14" / "pwm6").write_text("255\n")
    f = P.read_fans(str(hw))
    assert abs(f["pwm5_pct"] - 25.098) < 0.01 and f["pwm6_pct"] == 100.0


def test_proc_parsers():
    lines = fx("proc_stat_2samples.txt").splitlines()
    cpus = [l for l in lines if l.startswith("cpu ")]
    b0, t0 = P.cpu_times(cpus[0] + "\n")
    b1, t1 = P.cpu_times(cpus[1] + "\n")
    assert t1 > t0 and b1 >= b0
    used, total = P.mem_from_meminfo(fx("proc_meminfo.txt"))
    assert 0 < used < total
    r, w = P.disk_sectors(fx("proc_diskstats_and_zfs.txt"), ["nvme1n1", "nvme2n1"])
    assert r == 156792512 + 155774472 and w == 63890216 + 63890216
    size, cmax = P.arc_from_arcstats(fx("proc_diskstats_and_zfs.txt"))
    assert size == 17184893480 and cmax == 17179869184


def test_next_elapse():
    assert P.parse_next_elapse("NextElapseUSecRealtime=Fri 2026-10-16 03:02:27 UTC\n") == 1792119747.0
    assert P.parse_next_elapse("NextElapseUSecRealtime=\n") is None
```

- [ ] **Step 3: Run the tests to confirm they fail**

Run: `python -m pytest scripts/files/tests/test_ld_parsers.py -q`
Expected: ERROR, `No module named 'live_dashboard'`.

- [ ] **Step 4: Implement `scripts/files/live_dashboard/__init__.py`** (one line: `"""Live inference dashboard (spec docs/superpowers/specs/2026-10-16-live-dashboard-design.md)."""`) **and `parsers.py`**

```python
"""Pure parsers for the live dashboard. Formats recorded on the live cluster 2026-10-16 (llama.cpp b11578 with
--log-prefix; amdgpu sysfs; Linux /proc). No I/O here except the sysfs readers, which take a directory."""
import glob
import os
import re
from datetime import datetime, timezone

_PREFIX = re.compile(r"^(?P<m>\d+)\.(?P<s>\d{2})\.(?P<ms>\d{3})\.(?P<us>\d{3}) [IWE] ")
_ID = r"id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
_RX = (
    ("start", re.compile(r"slot launch_slot_: " + _ID + r"processing task")),
    ("prefill", re.compile(r"slot print_timing: " + _ID + r"prompt processing, n_tokens =\s+(?P<n>\d+), "
                           r"progress = (?P<progress>[\d.]+), t =\s+[\d.]+ s / (?P<tok_s>[\d.]+) tokens per second")),
    ("gen", re.compile(r"slot print_timing: " + _ID + r"n_gen =\s+(?P<n>\d+), tg =\s+(?P<tok_s>[\d.]+) t/s")),
    ("prompt_eval", re.compile(r"print_timing: " + _ID + r"prompt eval time =\s+(?P<ms>[\d.]+) ms /\s+(?P<n>\d+) tokens")),
    ("eval", re.compile(r"print_timing: " + _ID + r"\s+eval time =\s+(?P<ms>[\d.]+) ms /\s+(?P<n>\d+) tokens")),
    ("total", re.compile(r"print_timing: " + _ID + r"\s+total time =\s+(?P<ms>[\d.]+) ms /\s+(?P<n>\d+) tokens")),
    ("draft", re.compile(r"print_timing: " + _ID + r"draft acceptance = (?P<rate>[\d.]+) \(\s*(?P<accepted>\d+) "
                         r"accepted /\s*(?P<generated>\d+) generated\)")),
    ("release", re.compile(r"slot\s+release: " + _ID + r"stop processing: n_tokens = (?P<n>\d+)")),
)
_CANCEL = re.compile(r"srv\s+stop: cancel task, id_task = (?P<task>\d+)")
_SERVER_START = re.compile(r"llama_server: initializing \.\.\.")
_SERVER_READY = re.compile(r"llama_server: listening on ")
_INTS = {"slot", "task", "n", "accepted", "generated"}
_FLOATS = {"progress", "tok_s", "ms", "rate"}


def parse_journal_line(line: str):
    """One chat-journal line -> event dict, or None for anything the tracker does not use."""
    m = _PREFIX.match(line or "")
    if not m:
        return None
    since = int(m["m"]) * 60 + int(m["s"]) + int(m["ms"]) / 1e3 + int(m["us"]) / 1e6
    body = line[m.end():]
    if _SERVER_START.search(body):
        return {"kind": "server_start", "since_start_s": since}
    if _SERVER_READY.search(body):
        return {"kind": "server_ready", "since_start_s": since}
    c = _CANCEL.search(body)
    if c:
        return {"kind": "cancel", "since_start_s": since, "task": int(c["task"])}
    for kind, rx in _RX:
        r = rx.search(body)
        if r:
            ev = {"kind": kind, "since_start_s": since}
            for k, v in r.groupdict().items():
                ev[k] = int(v) if k in _INTS else float(v) if k in _FLOATS else v
            return ev
    return None


def parse_prometheus(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


def parse_slots(data) -> list:
    out = []
    for s in data or []:
        nt = (s.get("next_token") or [{}])[0] if isinstance(s.get("next_token"), list) else {}
        out.append({"id": int(s["id"]), "n_ctx": s.get("n_ctx"), "is_processing": bool(s.get("is_processing")),
                    "id_task": s.get("id_task"), "n_prompt_tokens": s.get("n_prompt_tokens"),
                    "processed": s.get("n_prompt_tokens_processed"), "cache": s.get("n_prompt_tokens_cache"),
                    "n_decoded": int(nt.get("n_decoded") or 0)})
    return out


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def find_gpu_dirs(drm_root: str, buses: list) -> dict:
    """bus address -> card device dir, matched by PCI_SLOT_NAME in device/uevent (card numbers drift)."""
    found = {}
    for dev in sorted(glob.glob(os.path.join(drm_root, "card*", "device"))):
        try:
            for line in _read(os.path.join(dev, "uevent")).splitlines():
                if line.startswith("PCI_SLOT_NAME=") and line.split("=", 1)[1] in buses:
                    found[line.split("=", 1)[1]] = dev
        except OSError:
            continue
    return found


def read_gpu(device_dir: str) -> dict:
    """One card's live stats; a missing/unreadable file nulls only its field and is named in `error`."""
    errors = []

    def num(rel, scale=1.0):
        try:
            return float(_read(os.path.join(device_dir, rel))) * scale
        except (OSError, ValueError):
            errors.append(rel)
            return None

    hw = sorted(glob.glob(os.path.join(device_dir, "hwmon", "hwmon*")))
    h = os.path.relpath(hw[0], device_dir) if hw else "hwmon/missing"
    used, total = num("mem_info_vram_used"), num("mem_info_vram_total")
    try:
        link = f"{_read(os.path.join(device_dir, 'current_link_speed')).replace(' PCIe', '')} x" \
               f"{_read(os.path.join(device_dir, 'current_link_width'))}"
    except OSError:
        errors.append("current_link_speed")
        link = None
    return {"load_pct": num("gpu_busy_percent"),
            "vram_used": int(used) if used is not None else None,
            "vram_total": int(total) if total is not None else None,
            "junction_c": num(os.path.join(h, "temp2_input"), 1e-3),
            "power_w": num(os.path.join(h, "power1_average"), 1e-6),
            "power_cap_w": num(os.path.join(h, "power1_cap"), 1e-6),
            "link": link, "error": ", ".join(errors) or None}


def read_fans(hwmon_root: str, chip: str = "nct6798") -> dict:
    for d in sorted(glob.glob(os.path.join(hwmon_root, "hwmon*"))):
        try:
            if _read(os.path.join(d, "name")) != chip:
                continue
            return {f"{p}_pct": round(float(_read(os.path.join(d, p))) * 100 / 255, 3) for p in ("pwm5", "pwm6")}
        except (OSError, ValueError):
            break
    return {"pwm5_pct": None, "pwm6_pct": None}


def cpu_times(stat_text: str):
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            total = sum(v[:8])
            return total - idle, total
    raise ValueError("no aggregate cpu line")


def mem_from_meminfo(text: str):
    kv = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        if rest.strip():
            kv[k] = int(rest.split()[0]) * 1024
    return kv["MemTotal"] - kv["MemAvailable"], kv["MemTotal"]


def disk_sectors(diskstats_text: str, devices: list):
    r = w = 0
    for line in diskstats_text.splitlines():
        f = line.split()
        if len(f) >= 10 and f[2] in devices:
            r += int(f[5])
            w += int(f[9])
    return r, w


def arc_from_arcstats(text: str):
    vals = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and f[0] in ("size", "c_max") and f[2].isdigit():
            vals[f[0]] = int(f[2])
    return vals["size"], vals["c_max"]


def parse_next_elapse(text: str):
    v = text.strip().partition("=")[2].strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest scripts/files/tests/test_ld_parsers.py -q`
Expected: 11 passed.

If a fixture-dependent assertion fails because the recorded file differs from these expectations, fix the parser, not the expectation. The fixture is ground truth. If the expectation itself misreads the fixture, correct it and record a `Ruling:`.

- [ ] **Step 6: Commit**

```bash
git add scripts/files/live_dashboard/__init__.py scripts/files/live_dashboard/parsers.py scripts/files/tests/test_ld_parsers.py scripts/files/tests/fixtures/live_dashboard docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html
git commit -m "feat(dashboard): live dashboard parsers with recorded cluster fixtures"
```

---

### Task 2: The request tracker

**Files:**
- Create: `scripts/files/live_dashboard/tracker.py`
- Test: `scripts/files/tests/test_ld_tracker.py`

**Interfaces:**
- Consumes (Task 1): journal event dicts; normalised slot dicts from `parse_slots`.
- Produces: `Tracker(slot_count: int = 3, approx_grace_s: float = 10.0, unreachable_s: float = 10.0)` with:
  - `on_journal(ev: dict, now: float) -> None`
  - `on_slots(slots: list[dict] | None, now: float) -> None` (`None` = unreachable)
  - `set_journal_up(up: bool, now: float) -> None`
  - `slot_views(now: float) -> list[dict]` (snapshot schema `slots` entries)
  - `open_rows() -> list[dict]` (row schema)
  - `pop_finished() -> list[dict]` (rows finished since the last call)

- [ ] **Step 1: Write the failing tests** in `scripts/files/tests/test_ld_tracker.py`

```python
"""live_dashboard.tracker -- request lifecycle from journal events + /slots snapshots."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard.tracker import Tracker  # noqa: E402


def ev(kind, t, **kw):
    return dict(kind=kind, since_start_s=t, **kw)


def slots(**per):
    out = []
    for i in range(3):
        s = {"id": i, "n_ctx": 131072, "is_processing": False, "id_task": None, "n_prompt_tokens": 0,
             "processed": 0, "cache": 0, "n_decoded": 0}
        s.update(per.get(i, {}))
        out.append(s)
    return out


def full_request(tr, t0=100.0, slot=1, task=4772):
    tr.on_journal(ev("start", 10.0, slot=slot, task=task), t0)
    tr.on_journal(ev("prefill", 13.0, slot=slot, task=task, n=2048, progress=0.55, tok_s=665.88), t0 + 3)
    tr.on_journal(ev("prefill", 16.0, slot=slot, task=task, n=3723, progress=1.0, tok_s=624.75), t0 + 6)
    tr.on_slots(slots(**{slot: {"is_processing": True, "id_task": task, "n_prompt_tokens": 3735, "n_decoded": 8}}), t0 + 7)
    tr.on_journal(ev("prompt_eval", 16.6, slot=slot, task=task, ms=6154.43, n=3727), t0 + 7.4)
    tr.on_journal(ev("eval", 16.6, slot=slot, task=task, ms=459.73, n=23), t0 + 7.4)
    tr.on_journal(ev("total", 16.6, slot=slot, task=task, ms=6614.16, n=3750), t0 + 7.4)
    tr.on_journal(ev("draft", 16.6, slot=slot, task=task, rate=0.7619, accepted=16, generated=21), t0 + 7.4)
    tr.on_journal(ev("release", 16.6, slot=slot, task=task, n=3750), t0 + 7.4)


def test_reading_progress_from_journal():
    tr = Tracker()
    tr.on_journal(ev("start", 10.0, slot=1, task=4772), 100.0)
    tr.on_journal(ev("prefill", 13.0, slot=1, task=4772, n=2048, progress=0.55, tok_s=665.88), 103.0)
    v = tr.slot_views(103.0)[1]
    assert v["state"] == "reading" and v["progress"] == 0.55 and v["processed_tokens"] == 2048
    assert v["tok_s"] == 665.88 and v["task"] == 4772
    assert tr.slot_views(103.0)[0]["state"] == "idle"


def test_generating_from_slots_then_done_row_from_journal():
    tr = Tracker()
    tr.on_journal(ev("start", 10.0, slot=1, task=4772), 100.0)
    tr.on_slots(slots(**{1: {"is_processing": True, "id_task": 4772, "n_prompt_tokens": 3735, "n_decoded": 8}}), 107.0)
    tr.on_slots(slots(**{1: {"is_processing": True, "id_task": 4772, "n_prompt_tokens": 3745, "n_decoded": 18}}), 108.0)
    v = tr.slot_views(108.0)[1]
    assert v["state"] == "generating" and v["output_tokens"] == 18 and v["tok_s"] == 10.0 and v["ctx_used"] == 3745
    tr2 = Tracker()
    full_request(tr2)
    rows = tr2.pop_finished()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "done" and r["slot"] == 1 and r["task"] == 4772
    assert r["prompt_tokens"] == 3727 and r["new_prompt_tokens"] == 3727 and r["cached_tokens"] == 0
    assert r["output_tokens"] == 23 and abs(r["prefill_tok_s"] - 3727 / 6.15443) < 1e-6
    assert abs(r["gen_tok_s"] - 23 / 0.45973) < 1e-6 and abs(r["duration_s"] - 6.61416) < 1e-9
    assert abs(r["draft_accept"] - 16 / 21) < 1e-9 and r["slow"] is False
    assert tr2.pop_finished() == [] and tr2.slot_views(108.0)[1]["state"] == "idle"
    assert tr2.slot_views(108.0)[1]["last_done"] == 107.4


def test_cached_prompt_and_slow_flag():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=9), 10.0)
    tr.on_journal(ev("prompt_eval", 2.0, slot=0, task=9, ms=10000.0, n=6000), 20.0)
    tr.on_journal(ev("eval", 2.0, slot=0, task=9, ms=1000.0, n=50), 20.0)
    tr.on_journal(ev("total", 2.0, slot=0, task=9, ms=11000.0, n=40050), 20.0)
    tr.on_journal(ev("release", 2.0, slot=0, task=9, n=40050), 20.0)
    r = tr.pop_finished()[0]
    assert r["prompt_tokens"] == 40000 and r["new_prompt_tokens"] == 6000 and r["cached_tokens"] == 34000
    assert r["slow"] is True and r["draft_accept"] is None


def test_cancelled_request():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=2, task=5), 10.0)
    tr.on_journal(ev("cancel", 3.0, task=5), 12.0)
    tr.on_journal(ev("release", 3.0, slot=2, task=5, n=100), 12.0)
    r = tr.pop_finished()[0]
    assert r["status"] == "cancelled" and r["output_tokens"] is None


def test_restart_interrupts_open_rows_and_keys_do_not_collide():
    tr = Tracker()
    tr.on_journal(ev("start", 500.0, slot=0, task=16), 100.0)
    tr.on_journal(ev("server_start", 0.01), 110.0)
    rows = tr.pop_finished()
    assert len(rows) == 1 and rows[0]["status"] == "interrupted" and rows[0]["finished"] == 110.0
    assert all(v["state"] == "restarting" for v in tr.slot_views(111.0))
    tr.on_journal(ev("server_ready", 22.0), 132.0)
    assert all(v["state"] == "idle" for v in tr.slot_views(133.0))
    tr.on_journal(ev("start", 30.0, slot=0, task=16), 140.0)
    assert tr.open_rows()[0]["key"] != rows[0]["key"]


def test_prefix_going_backwards_is_a_restart():
    tr = Tracker()
    tr.on_journal(ev("start", 600.0, slot=0, task=1), 100.0)
    tr.on_journal(ev("start", 5.0, slot=1, task=1), 200.0)
    rows = tr.pop_finished()
    assert len(rows) == 1 and rows[0]["status"] == "interrupted"
    assert tr.open_rows()[0]["slot"] == 1


def test_journal_down_gives_approx_rows_from_slots():
    tr = Tracker(approx_grace_s=0.0)
    tr.set_journal_up(False, 100.0)
    tr.on_slots(slots(**{0: {"is_processing": True, "id_task": 77, "n_prompt_tokens": 900, "n_decoded": 0}}), 100.0)
    tr.on_slots(slots(**{0: {"is_processing": True, "id_task": 77, "n_prompt_tokens": 950, "n_decoded": 50}}), 102.0)
    tr.on_slots(slots(), 103.0)
    r = tr.pop_finished()[0]
    assert r["status"] == "approx" and r["output_tokens"] == 50 and r["task"] == 77 and r["started"] == 100.0


def test_slots_unreachable_interrupts_after_threshold():
    tr = Tracker(unreachable_s=10.0)
    tr.on_journal(ev("start", 1.0, slot=0, task=3), 100.0)
    tr.on_slots(None, 105.0)
    tr.on_slots(None, 114.0)                         # 9 s unreachable: still open
    assert tr.pop_finished() == []
    tr.on_slots(None, 115.5)                         # 10.5 s: interrupted
    assert tr.pop_finished()[0]["status"] == "interrupted"


def test_release_without_timings_and_no_cancel_is_approx():
    tr = Tracker()
    tr.on_journal(ev("start", 1.0, slot=0, task=3), 100.0)
    tr.on_journal(ev("release", 2.0, slot=0, task=3, n=10), 101.0)
    assert tr.pop_finished()[0]["status"] == "approx"
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python -m pytest scripts/files/tests/test_ld_tracker.py -q`
Expected: ERROR, `No module named 'live_dashboard.tracker'`.

- [ ] **Step 3: Implement `scripts/files/live_dashboard/tracker.py`**

```python
"""Request state machine (spec §4). Pure: callers pass `now`. The journal drives the lifecycle (start, prefill
progress, exact timings, release, cancel, server restarts); /slots adds the generating detail and is the only source
while the journal stream is down (rows then end as `approx`)."""
SLOW_NEW_PROMPT_TOKENS = 5000


class Tracker:
    def __init__(self, slot_count: int = 3, approx_grace_s: float = 10.0, unreachable_s: float = 10.0):
        self.slot_count = slot_count
        self.approx_grace_s = approx_grace_s
        self.unreachable_s = unreachable_s
        self.server_run = None          # wall time this server run was first seen
        self.last_since = None          # journal prefix of the previous event (detects restarts)
        self.restarting = False
        self.journal_up = True
        self.unreachable_since = None
        self.open = {}                  # slot -> row dict (+ private live fields)
        self.cancelled = set()          # task ids cancelled in this run
        self.last_done = {}             # slot -> wall time
        self.idle_since = {}            # slot -> wall time the slot was seen idle with a row still open
        self.finished = []

    # ---- helpers
    def _key(self, slot, task):
        return f"{self.server_run:.3f}:{slot}:{task}"

    def _new_row(self, slot, task, now):
        if self.server_run is None:
            self.server_run = now
        row = {"key": self._key(slot, task), "server_run": self.server_run, "slot": slot, "task": task,
               "started": now, "finished": None, "status": "reading", "prompt_tokens": None,
               "new_prompt_tokens": None, "cached_tokens": None, "output_tokens": None, "prefill_tok_s": None,
               "gen_tok_s": None, "duration_s": None, "draft_accept": None, "slow": False,
               "_progress": None, "_processed": None, "_live_tok_s": None, "_ctx": None,
               "_dec": None, "_dec_t": None, "_eval": None, "_pe": None, "_total": None}
        self.open[slot] = row
        return row

    def _finish(self, slot, status, now):
        row = self.open.pop(slot, None)
        if row is None:
            return
        row["status"], row["finished"] = status, now
        pe, ev, tot = row["_pe"], row["_eval"], row["_total"]
        if pe:
            row["new_prompt_tokens"] = pe["n"]
            row["prefill_tok_s"] = pe["n"] / (pe["ms"] / 1e3) if pe["ms"] > 0 else None
        if ev:
            row["output_tokens"] = ev["n"]
            row["gen_tok_s"] = ev["n"] / (ev["ms"] / 1e3) if ev["ms"] > 0 else None
        if tot:
            row["duration_s"] = tot["ms"] / 1e3
            if ev:
                row["prompt_tokens"] = tot["n"] - ev["n"]
        elif status in ("approx", "interrupted"):
            row["duration_s"] = now - row["started"]
            if status == "approx" and row["_dec"] is not None:
                row["output_tokens"] = row["_dec"]
        if row["prompt_tokens"] is not None and row["new_prompt_tokens"] is not None:
            row["cached_tokens"] = max(0, row["prompt_tokens"] - row["new_prompt_tokens"])
        row["slow"] = bool(row["new_prompt_tokens"] and row["new_prompt_tokens"] > SLOW_NEW_PROMPT_TOKENS)
        self.last_done[slot] = now
        self.idle_since.pop(slot, None)
        self.finished.append({k: v for k, v in row.items() if not k.startswith("_")})

    def _restart(self, now):
        for slot in list(self.open):
            self._finish(slot, "interrupted", now)
        self.server_run = now
        self.cancelled.clear()

    # ---- inputs
    def on_journal(self, ev: dict, now: float) -> None:
        kind, since = ev["kind"], ev.get("since_start_s")
        if kind == "server_start" or (since is not None and self.last_since is not None and since < self.last_since - 1.0):
            self._restart(now)
            self.restarting = kind == "server_start"
        self.last_since = since
        if kind == "server_ready":
            self.restarting = False
            return
        if kind == "server_start":
            return
        if self.server_run is None:
            self.server_run = now
        if kind == "cancel":
            self.cancelled.add(ev["task"])
            return
        slot, task = ev.get("slot"), ev.get("task")
        if kind == "start":
            if slot in self.open:
                self._finish(slot, "approx", now)
            self._new_row(slot, task, now)
            return
        row = self.open.get(slot)
        if row is None or row["task"] != task:
            return
        if kind == "prefill":
            row.update(_progress=ev["progress"], _processed=ev["n"], _live_tok_s=ev["tok_s"])
        elif kind == "gen":
            row.update(status="generating", _dec=ev["n"], _live_tok_s=ev["tok_s"])
        elif kind == "prompt_eval":
            row["_pe"] = {"ms": ev["ms"], "n": ev["n"]}
        elif kind == "eval":
            row["_eval"] = {"ms": ev["ms"], "n": ev["n"]}
        elif kind == "total":
            row["_total"] = {"ms": ev["ms"], "n": ev["n"]}
        elif kind == "draft":
            row["draft_accept"] = ev["accepted"] / ev["generated"] if ev["generated"] else None
        elif kind == "release":
            status = "cancelled" if task in self.cancelled else "done" if row["_eval"] else "approx"
            self._finish(slot, status, now)

    def on_slots(self, slots, now: float) -> None:
        if slots is None:
            if self.unreachable_since is None:
                self.unreachable_since = now
            if now - self.unreachable_since >= self.unreachable_s:
                for slot in list(self.open):
                    self._finish(slot, "interrupted", now)
            return
        self.unreachable_since = None
        for s in slots:
            slot = s["id"]
            row = self.open.get(slot)
            if s["is_processing"]:
                if row is None and not self.journal_up:
                    row = self._new_row(slot, s["id_task"], now)
                if row is None:
                    continue
                self.idle_since.pop(slot, None)
                row["_ctx"] = s["n_prompt_tokens"]
                if s["n_decoded"] > 0:
                    if row["_dec"] is not None and row["_dec_t"] is not None and now > row["_dec_t"]:
                        row["_live_tok_s"] = (s["n_decoded"] - row["_dec"]) / (now - row["_dec_t"])
                    row.update(status="generating", _dec=s["n_decoded"], _dec_t=now)
            elif row is not None:
                first = self.idle_since.setdefault(slot, now)
                if not self.journal_up or now - first >= self.approx_grace_s:
                    self._finish(slot, "approx", now)

    def set_journal_up(self, up: bool, now: float) -> None:
        self.journal_up = up

    # ---- outputs
    def slot_views(self, now: float) -> list:
        views = []
        for i in range(self.slot_count):
            row = self.open.get(i)
            state = "restarting" if self.restarting else (row["status"] if row else "idle")
            views.append({"id": i, "state": state, "task": row["task"] if row else None,
                          "progress": row["_progress"] if row else None,
                          "prompt_tokens": row["prompt_tokens"] if row else None,
                          "processed_tokens": row["_processed"] if row else None,
                          "cached_tokens": row["cached_tokens"] if row else None,
                          "output_tokens": row["_dec"] if row else None,
                          "tok_s": row["_live_tok_s"] if row else None,
                          "ctx_used": row["_ctx"] if row else None, "n_ctx": None,
                          "started": row["started"] if row else None, "last_done": self.last_done.get(i)})
        return views

    def open_rows(self) -> list:
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in self.open.values()]

    def pop_finished(self) -> list:
        out, self.finished = self.finished, []
        return out
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest scripts/files/tests/test_ld_tracker.py -q`
Expected: 9 passed.

- [ ] **Step 5: Mutation check**

Use a matched replace and the exact restore. Never git checkout, restore or stash.
1. In `on_journal`, replace `since < self.last_since - 1.0` with `False`.
2. Run the tracker tests. Expected: `test_prefix_going_backwards_is_a_restart` fails.
3. Restore the exact text and re-run. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/files/live_dashboard/tracker.py scripts/files/tests/test_ld_tracker.py
git commit -m "feat(dashboard): request tracker - journal lifecycle, slot detail, restart/cancel/approx rows"
```

---

### Task 3: The history store

**Files:**
- Create: `scripts/files/live_dashboard/history.py`
- Test: `scripts/files/tests/test_ld_history.py`

**Interfaces:**
- Consumes: the row schema.
- Produces: `History(path: str, retention_days: int = 30)` with:
  - `add(row: dict) -> None` (queued; the single writer thread persists it)
  - `flush(timeout: float = 5.0) -> bool`
  - `recent(limit: int) -> list[dict]` (newest first; limit clamped to 1..500)
  - `bands(days: int, now: float) -> list[dict]` (days clamped to 1..30)
  - `prune(now: float) -> int`
  - `ok: bool`
  - `close() -> None`
- Module function `band_of(prompt_tokens: int | None) -> str | None`.

- [ ] **Step 1: Write the failing tests** in `scripts/files/tests/test_ld_history.py`

```python
"""live_dashboard.history -- SQLite request store, single writer, bands, prune, caps."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard.history import History, band_of  # noqa: E402

DAY = 86400.0


def row(key, finished, prompt, gen=40.0, pre=500.0, status="done"):
    return {"key": key, "server_run": 1.0, "slot": 0, "task": 1, "started": finished - 5, "finished": finished,
            "status": status, "prompt_tokens": prompt, "new_prompt_tokens": prompt, "cached_tokens": 0,
            "output_tokens": 100, "prefill_tok_s": pre, "gen_tok_s": gen, "duration_s": 5.0,
            "draft_accept": 0.8, "slow": False}


def test_band_edges():
    assert band_of(0) == "0-8K" and band_of(8191) == "0-8K" and band_of(8192) == "8-32K"
    assert band_of(65535) == "32-64K" and band_of(98303) == "64-96K" and band_of(131072) == "96-128K"
    assert band_of(None) is None


def test_add_recent_newest_first_and_upsert(tmp_path):
    h = History(str(tmp_path / "h.db"))
    h.add(row("a", 100.0, 1000))
    h.add(row("b", 200.0, 2000))
    h.add(row("a", 100.0, 1500))                     # same key replaces
    assert h.flush()
    keys = [r["key"] for r in h.recent(10)]
    assert keys == ["b", "a"] and h.recent(10)[1]["prompt_tokens"] == 1500
    assert h.recent(10)[0]["slow"] is False
    h.close()


def test_limit_and_days_are_clamped(tmp_path):
    h = History(str(tmp_path / "h.db"))
    for i in range(3):
        h.add(row(f"k{i}", 100.0 + i, 1000))
    h.flush()
    assert len(h.recent(0)) == 1 and len(h.recent(10_000)) == 3
    assert h.bands(10_000, now=200.0) == h.bands(30, now=200.0)
    h.close()


def test_bands_use_medians_and_skip_non_done(tmp_path):
    h = History(str(tmp_path / "h.db"))
    now = 10 * DAY
    h.add(row("a", now - 10, 1000, gen=40.0, pre=600.0))
    h.add(row("b", now - 20, 2000, gen=50.0, pre=500.0))
    h.add(row("c", now - 30, 3000, gen=60.0, pre=400.0))
    h.add(row("d", now - 40, 70000, gen=30.0, pre=300.0))
    h.add(row("e", now - 50, 1000, gen=1.0, pre=1.0, status="approx"))
    h.add(row("old", now - 8 * DAY, 1000, gen=99.0, pre=99.0))
    h.flush()
    b = {x["band"]: x for x in h.bands(7, now=now)}
    assert b["0-8K"]["n"] == 3 and b["0-8K"]["gen_tok_s"] == 50.0 and b["0-8K"]["prefill_tok_s"] == 500.0
    assert b["64-96K"]["n"] == 1 and "8-32K" not in b
    h.close()


def test_prune_and_survives_reopen(tmp_path):
    p = str(tmp_path / "h.db")
    h = History(p, retention_days=30)
    h.add(row("old", 1.0, 1000))
    h.add(row("new", 40 * DAY, 1000))
    h.flush()
    assert h.prune(now=40 * DAY + 1) == 1
    h.close()
    h2 = History(p)
    assert [r["key"] for r in h2.recent(10)] == ["new"]
    h2.close()


def test_unwritable_path_sets_ok_false_without_raising(tmp_path):
    bad = tmp_path / "dir_not_file"
    bad.mkdir()
    h = History(str(bad))                            # a directory cannot be opened as a database
    assert h.ok is False
    h.add(row("x", 1.0, 1))                          # never raises
    assert h.recent(5) == [] and h.bands(7, now=2.0) == []
    h.close()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python -m pytest scripts/files/tests/test_ld_history.py -q`
Expected: ERROR, `No module named 'live_dashboard.history'`.

- [ ] **Step 3: Implement `scripts/files/live_dashboard/history.py`**

```python
"""SQLite request history (spec §2, §5). One writer thread owns all writes; reads use their own short-lived
connections. If the database can't be opened or written, `ok` turns False and the live view carries on."""
import logging
import queue
import sqlite3
import statistics
import threading

log = logging.getLogger("live_dashboard.history")
COLUMNS = ("key", "server_run", "slot", "task", "started", "finished", "status", "prompt_tokens",
           "new_prompt_tokens", "cached_tokens", "output_tokens", "prefill_tok_s", "gen_tok_s", "duration_s",
           "draft_accept", "slow")
BANDS = ((8192, "0-8K"), (32768, "8-32K"), (65536, "32-64K"), (98304, "64-96K"))


def band_of(prompt_tokens):
    if prompt_tokens is None:
        return None
    for limit, name in BANDS:
        if prompt_tokens < limit:
            return name
    return "96-128K"


class History:
    def __init__(self, path: str, retention_days: int = 30):
        self.path = path
        self.retention_s = retention_days * 86400.0
        self.ok = True
        self._q = queue.Queue()
        try:
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE IF NOT EXISTS requests (" + ", ".join(
                f"{c} {'TEXT PRIMARY KEY' if c == 'key' else 'TEXT' if c == 'status' else 'REAL'}" for c in COLUMNS) + ")")
            con.execute("CREATE INDEX IF NOT EXISTS requests_finished ON requests(finished)")
            con.commit()
            con.close()
        except sqlite3.Error as e:
            log.error("history unavailable: %s", e)
            self.ok = False
        self._t = threading.Thread(target=self._writer, name="history-writer", daemon=True)
        self._t.start()

    def _writer(self):
        con = None
        while True:
            item = self._q.get()
            if item is None:
                break
            kind, payload, done = item
            try:
                if not self.ok:
                    raise sqlite3.OperationalError("history disabled")
                if con is None:
                    con = sqlite3.connect(self.path)
                if kind == "add":
                    con.execute(f"INSERT OR REPLACE INTO requests ({', '.join(COLUMNS)}) VALUES "
                                f"({', '.join('?' * len(COLUMNS))})",
                                [int(payload["slow"]) if c == "slow" else payload.get(c) for c in COLUMNS])
                elif kind == "prune":
                    cur = con.execute("DELETE FROM requests WHERE finished < ?", (payload,))
                    done["deleted"] = cur.rowcount
                con.commit()
            except sqlite3.Error as e:
                if self.ok:
                    log.error("history write failed: %s", e)
                self.ok = False
            finally:
                if done is not None:
                    done["event"].set()
        if con is not None:
            con.close()

    def add(self, row: dict) -> None:
        self._q.put(("add", dict(row), None))

    def flush(self, timeout: float = 5.0) -> bool:
        done = {"event": threading.Event()}
        self._q.put(("noop", None, done))
        return done["event"].wait(timeout)

    def prune(self, now: float) -> int:
        done = {"event": threading.Event(), "deleted": 0}
        self._q.put(("prune", now - self.retention_s, done))
        done["event"].wait(10.0)
        return done["deleted"]

    def _read(self, sql, args):
        if not self.ok:
            return []
        try:
            con = sqlite3.connect(self.path)
            try:
                return con.execute(sql, args).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    def recent(self, limit: int) -> list:
        limit = max(1, min(int(limit), 500))
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM requests ORDER BY finished DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(zip(COLUMNS, r))
            d["slow"] = bool(d["slow"])
            for c in ("slot", "task", "prompt_tokens", "new_prompt_tokens", "cached_tokens", "output_tokens"):
                d[c] = int(d[c]) if d[c] is not None else None
            out.append(d)
        return out

    def bands(self, days: int, now: float) -> list:
        days = max(1, min(int(days), 30))
        rows = self._read("SELECT prompt_tokens, gen_tok_s, prefill_tok_s FROM requests "
                          "WHERE status = 'done' AND finished >= ?", (now - days * 86400.0,))
        groups = {}
        for prompt, gen, pre in rows:
            b = band_of(int(prompt) if prompt is not None else None)
            if b:
                groups.setdefault(b, []).append((gen, pre))
        order = [name for _, name in BANDS] + ["96-128K"]
        out = []
        for name in order:
            if name in groups:
                g = [x for x, _ in groups[name] if x is not None]
                p = [y for _, y in groups[name] if y is not None]
                out.append({"band": name, "n": len(groups[name]),
                            "gen_tok_s": statistics.median(g) if g else None,
                            "prefill_tok_s": statistics.median(p) if p else None})
        return out

    def close(self) -> None:
        self._q.put(None)
        self._t.join(5.0)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest scripts/files/tests/test_ld_history.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/files/live_dashboard/history.py scripts/files/tests/test_ld_history.py
git commit -m "feat(dashboard): SQLite request history - single writer, bands, prune, caps"
```

---

### Task 4: Collectors and the Hub

**Files:**
- Create: `scripts/files/live_dashboard/collectors.py`
- Test: `scripts/files/tests/test_ld_collectors.py`

**Interfaces:**
- Consumes: Task 1 parsers, Task 2 `Tracker`, Task 3 `History`.
- Produces:
  - `find_llama_key(proc_root: str = "/proc", port: str = "8080") -> str | None`
  - `find_router_key(proc_root: str = "/proc") -> str | None`
  - `class Source(name)` with `ok`, `stale_since`, `error`, `mark_ok()`, `mark_fail(err, now)`, `as_dict()`
  - `class Ring(window_s: float)` with `push(t, v)`, `values()`, `avg()`
  - `class JournalFollower(argv: list[str], on_line, on_state, backoff=(1, 2, 5, 10, 30))` with `start()`, `stop()`
  - `class Hub(cfg: dict, http_get, read_text, run_cmd, now_fn)` with:
    - `poll_fast(now)`: slots, metrics, GPU, host
    - `poll_services(now)`, `poll_timer(now)`, `poll_escalation(now)`
    - `on_journal_line(line, now)`, `on_journal_state(up, now)`
    - `snapshot(now) -> dict` (snapshot schema)
    - `series() -> dict` (name → list of [t, v], 15 min)

The injected I/O lets tests run without a cluster:
- `http_get(url, headers) -> (status:int, body:str)`, where 0 means a transport error;
- `read_text(path) -> str`;
- `run_cmd(argv) -> (rc, stdout)`.

- [ ] **Step 1: Write the failing tests** in `scripts/files/tests/test_ld_collectors.py`

```python
"""live_dashboard.collectors -- Hub assembly from injected I/O; key discovery; journal follower restart."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
FX = os.path.join(HERE, "fixtures", "live_dashboard")

from live_dashboard import collectors as C  # noqa: E402


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


def fake_proc(tmp_path, entries):
    for pid, (argv, environ) in entries.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()) + b"\0")
    (tmp_path / "self").mkdir()
    return str(tmp_path)


def test_key_discovery_from_proc(tmp_path):
    root = fake_proc(tmp_path, {
        11: (["/opt/llama.cpp/build/bin/llama-server", "--port", "8082", "--api-key", "EMBEDKEY"], {}),
        12: (["/opt/llama.cpp/build/bin/llama-server", "--port", "8080", "--api-key", "CHATKEY"], {}),
        13: (["/opt/llm-router/venv/bin/python3", "/opt/llm-router/venv/bin/uvicorn", "app:app"],
             {"ROUTER_API_KEY": "ROUTERKEY", "OTHER": "x"}),
    })
    assert C.find_llama_key(root) == "CHATKEY"
    assert C.find_router_key(root) == "ROUTERKEY"
    assert C.find_llama_key(root, port="9999") is None


def test_ring_window_and_avg():
    r = C.Ring(10)
    for t, v in ((0, 10.0), (5, 20.0), (12, 30.0)):
        r.push(t, v)
    assert r.values() == [[5, 20.0], [12, 30.0]] and r.avg() == 25.0


class World:
    """Injected I/O standing in for the cluster."""
    def __init__(self, tmp_path):
        self.status = {}
        d = json.loads(fx("llama_slots_metrics_session.json"))
        self.slots = json.dumps(d["samples"][5]["slots"])
        self.metrics = ("llamacpp:prompt_tokens_total 72775\nllamacpp:prompt_seconds_total 144.129\n"
                        "llamacpp:tokens_predicted_total 15936\nllamacpp:tokens_predicted_seconds_total 338.927\n"
                        "llamacpp:prompt_tokens_cached_total 650897\nllamacpp:requests_deferred 0\n"
                        "llamacpp:requests_processing 1\nllamacpp:spec_decode_num_draft_tokens_total 13947\n"
                        "llamacpp:spec_decode_num_accepted_tokens_total 11297\n"
                        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 4258\n'
                        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 3761\n'
                        'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="2"} 3278\n'
                        "llamacpp:spec_decode_num_drafts_total 4649\n")
        self.files = {"/proc/stat": "cpu  100 0 50 800 50 0 0 0 0 0\n",
                      "/proc/meminfo": fx("proc_meminfo.txt"),
                      "/proc/diskstats": fx("proc_diskstats_and_zfs.txt"),
                      "/proc/spl/kstat/zfs/arcstats": fx("proc_diskstats_and_zfs.txt")}
        self.key_reads = 0

    def http_get(self, url, headers):
        if url in self.status:
            return self.status[url]
        if url.endswith("/slots"):
            return (200, self.slots) if headers.get("Authorization") == "Bearer CHATKEY" else (401, "")
        if url.endswith("/metrics"):
            return 200, self.metrics
        if url.endswith("/healthz"):
            return 200, json.dumps({"ok": True, "upstream": {"chat": "ok", "embed": "ok", "rerank": "ok"},
                                    "active_chat_profile": "qwen3.8"})
        if url.endswith("/escalation/budget"):
            return 200, fx("router_escalation_budget.txt").split("\n", 1)[1]
        return 0, ""

    def read_text(self, path):
        if path not in self.files:
            raise OSError(path)
        return self.files[path]

    def run_cmd(self, argv):
        return 0, "NextElapseUSecRealtime=Fri 2026-10-16 03:02:27 UTC\n"


def make_hub(tmp_path, world, monkeypatch):
    monkeypatch.setattr(C, "find_llama_key", lambda *a, **k: (setattr(world, "key_reads", world.key_reads + 1), "CHATKEY")[1])
    monkeypatch.setattr(C, "find_router_key", lambda *a, **k: "ROUTERKEY")
    cfg = dict(C.DEFAULTS, history_path=str(tmp_path / "h.db"), drm_root=str(tmp_path / "nodrm"),
               hwmon_root=str(tmp_path / "nohwmon"))
    return C.Hub(cfg, world.http_get, world.read_text, world.run_cmd, now_fn=lambda: 1000.0)


def test_snapshot_shape_and_values(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    hub.poll_services(1000.0)
    hub.poll_timer(1000.0)
    hub.poll_escalation(1000.0)
    s = hub.snapshot(1000.0)
    assert set(s) == {"ts", "sources", "model", "slots", "queue", "speed", "gpus", "fans", "host", "services",
                      "escalation", "recent", "history_ok"}
    assert s["sources"]["slots"]["ok"] is True and s["queue"] == {"deferred": 0, "processing": 1}
    assert len(s["slots"]) == 3 and s["slots"][1]["n_ctx"] == 131072
    assert abs(s["speed"]["mtp_accept"] - 11297 / 13947) < 1e-9
    assert s["services"]["router"]["ok"] is True and s["services"]["router"]["profile"] == "qwen3.8"
    assert s["escalation"]["month"]["cap_usd"] == 150.0
    assert s["services"]["restart"]["next_possible"] is not None
    assert s["sources"]["gpu"]["ok"] is False                       # no fake drm tree in this test
    assert s["sources"]["host"]["ok"] is True and s["host"]["arc_max"] == 17179869184
    text = json.dumps(s)
    assert "CHATKEY" not in text and "ROUTERKEY" not in text


def test_401_rereads_key_and_failure_marks_only_that_source(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    hub.poll_fast(1000.0)
    reads = w.key_reads
    w.status["http://192.168.6.151:8080/slots"] = (401, "")
    hub.poll_fast(1001.0)
    assert w.key_reads > reads
    s = hub.snapshot(1001.0)
    assert s["sources"]["slots"]["ok"] is False and s["sources"]["slots"]["stale_since"] == 1001.0
    assert s["sources"]["metrics"]["ok"] is True and s["sources"]["host"]["ok"] is True


def test_journal_lines_flow_to_recent_and_history(tmp_path, monkeypatch):
    w = World(tmp_path)
    hub = make_hub(tmp_path, w, monkeypatch)
    for i, line in enumerate(fx("journal_chat_last10min_raw.txt").splitlines()):
        hub.on_journal_line(line, 2000.0 + i)
    hub.poll_fast(2100.0)
    s = hub.snapshot(2100.0)
    assert s["recent"][0]["task"] == 4772 and s["recent"][0]["status"] == "done"
    assert hub.history.flush() and hub.history.recent(5)[0]["key"] == s["recent"][0]["key"]


def test_journal_follower_restarts_with_backoff():
    lines, states = [], []
    script = [sys.executable, "-c", "print('552.57.641.854 I slot launch_slot_: id  1 | task 9 | processing task')"]
    f = C.JournalFollower(script, on_line=lines.append, on_state=lambda up: states.append(up), backoff=(0.05,))
    f.start()
    deadline = time.time() + 5
    while time.time() < deadline and len(lines) < 2:
        time.sleep(0.05)
    f.stop()
    assert len(lines) >= 2 and False in states and True in states
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python -m pytest scripts/files/tests/test_ld_collectors.py -q`
Expected: ERROR, `No module named 'live_dashboard.collectors'`.

- [ ] **Step 3: Implement `scripts/files/live_dashboard/collectors.py`**

```python
"""Collectors and the Hub (spec §2, §5). All cluster I/O is injected (http_get, read_text, run_cmd) so the Hub is
testable without a cluster; main.py wires the real implementations. Keys come from the host's /proc, live in memory
only, and never appear in snapshots or logs."""
import glob
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import parsers as P
from .history import History
from .tracker import Tracker

log = logging.getLogger("live_dashboard.collectors")
DEFAULTS = {
    "bind_host": "192.168.6.175", "bind_port": 8889, "chat_url": "http://192.168.6.151:8080",
    "embed_url": "http://192.168.6.151:8082", "rerank_url": "http://192.168.6.151:8083",
    "router_url": "http://192.168.6.153:8000", "gpu_buses": ["0000:03:00.0", "0000:07:00.0"],
    "drm_root": "/sys/class/drm", "hwmon_root": "/sys/class/hwmon", "disk_devices": ["nvme1n1", "nvme2n1"],
    "history_path": "/var/lib/live-dashboard/history.db", "retention_days": 30, "sample_window_s": 900,
    "max_stream_clients": 8, "slot_count": 3, "chat_ct": 151,
    "journal_argv": ["pct", "exec", "151", "--", "journalctl", "-u", "llamacpp-chat", "-f", "-o", "cat", "-n", "0"],
}
SOURCES = ("slots", "metrics", "journal", "gpu", "host", "services", "escalation")


def _argv(proc_root, pid):
    try:
        with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _pids(proc_root):
    try:
        return [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:                                  # no /proc (e.g. tests on Windows): nothing to find
        return []


def find_llama_key(proc_root: str = "/proc", port: str = "8080"):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if a and a[0].endswith("llama-server") and "--port" in a and a[a.index("--port") + 1:a.index("--port") + 2] == [port]:
            if "--api-key" in a and a.index("--api-key") + 1 < len(a):
                return a[a.index("--api-key") + 1]
    return None


def find_router_key(proc_root: str = "/proc"):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if any(x.endswith("uvicorn") for x in a) and "app:app" in a:
            try:
                with open(os.path.join(proc_root, pid, "environ"), "rb") as f:
                    for kv in f.read().split(b"\0"):
                        if kv.startswith(b"ROUTER_API_KEY="):
                            return kv.split(b"=", 1)[1].decode()
            except OSError:
                continue
    return None


def find_chat_pid(proc_root: str = "/proc"):
    for pid in _pids(proc_root):
        a = _argv(proc_root, pid)
        if a and a[0].endswith("llama-server") and "--port" in a and a[a.index("--port") + 1:a.index("--port") + 2] == ["8080"]:
            return pid
    return None


class Source:
    def __init__(self, name):
        self.name, self.ok, self.stale_since, self.error = name, False, None, "not polled yet"

    def mark_ok(self):
        self.ok, self.stale_since, self.error = True, None, None

    def mark_fail(self, err, now):
        if self.ok or self.stale_since is None:
            self.stale_since = now
        self.ok, self.error = False, str(err)[:200]

    def as_dict(self):
        return {"ok": self.ok, "stale_since": self.stale_since, "error": self.error}


class Ring:
    def __init__(self, window_s: float):
        self.window_s, self.pts = window_s, []

    def push(self, t, v):
        if v is None:
            return
        self.pts.append([t, v])
        cutoff = t - self.window_s
        while self.pts and self.pts[0][0] < cutoff:
            self.pts.pop(0)

    def values(self):
        return [list(p) for p in self.pts]

    def avg(self):
        return sum(v for _, v in self.pts) / len(self.pts) if self.pts else None


class JournalFollower:
    """Runs argv (pct exec ... journalctl -f), feeds each line to on_line; restarts with backoff when it exits."""
    def __init__(self, argv, on_line, on_state, backoff=(1, 2, 5, 10, 30)):
        self.argv, self.on_line, self.on_state, self.backoff = argv, on_line, on_state, backoff
        self._stop, self._proc, self._t = threading.Event(), None, None

    def start(self):
        self._t = threading.Thread(target=self._run, name="journal-follower", daemon=True)
        self._t.start()

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                              text=True, bufsize=1)
                self.on_state(True)
                for line in self._proc.stdout:
                    attempt = 0
                    self.on_line(line.rstrip("\n"))
                    if self._stop.is_set():
                        break
                self._proc.wait()
            except OSError as e:
                log.warning("journal follower failed to start: %s", e)
            self.on_state(False)
            if self._stop.wait(self.backoff[min(attempt, len(self.backoff) - 1)]):
                break
            attempt += 1

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._t:
            self._t.join(5)


def default_http_get(url, headers, timeout=3.0):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def default_read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def default_run_cmd(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


class Hub:
    def __init__(self, cfg, http_get, read_text, run_cmd, now_fn=time.time):
        self.cfg = cfg
        self.http_get, self.read_text, self.run_cmd, self.now_fn = http_get, read_text, run_cmd, now_fn
        self.lock = threading.Lock()
        self.src = {n: Source(n) for n in SOURCES}
        self.tracker = Tracker(slot_count=cfg["slot_count"])
        self.history = History(cfg["history_path"], cfg["retention_days"])
        w = cfg["sample_window_s"]
        self.rings = {n: Ring(w) for n in ("gen", "prefill", "cache", "mtp", "gpu0_load", "gpu1_load", "host_cpu")}
        self.recent_done = []
        self._key = None
        self._router_key = None
        self._prev_metrics = None
        self._prev_cpu = None
        self._prev_disk = None
        self._prev_embed = {}
        self.gpu_dirs = {}
        self.state = {"slots_raw": None, "metrics": {}, "gpus": [], "fans": {"pwm5_pct": None, "pwm6_pct": None},
                      "host": {}, "speed": {}, "services": {"router": {"ok": False, "upstream": {}, "profile": None},
                                                            "embed": {"ok": False, "activity_per_min": None},
                                                            "rerank": {"ok": False, "activity_per_min": None},
                                                            "restart": {"next_possible": None}},
                      "escalation": None}

    # ---- llama
    def _llama(self, path):
        if self._key is None:
            self._key = find_llama_key()
        status, body = self.http_get(self.cfg["chat_url"] + path, {"Authorization": f"Bearer {self._key or ''}"})
        if status == 401:
            self._key = find_llama_key()
        return status, body

    def poll_fast(self, now):
        with self.lock:
            self._poll_slots(now)
            self._poll_metrics(now)
            self._poll_gpu(now)
            self._poll_host(now)
            self._drain_finished()

    def _poll_slots(self, now):
        status, body = self._llama("/slots")
        if status != 200:
            self.src["slots"].mark_fail(f"/slots HTTP {status}", now)
            self.tracker.on_slots(None, now)
            return
        try:
            slots = P.parse_slots(json.loads(body))
        except (ValueError, KeyError, TypeError) as e:
            self.src["slots"].mark_fail(f"/slots parse: {type(e).__name__}", now)
            return
        self.src["slots"].mark_ok()
        self.state["slots_raw"] = slots
        self.tracker.on_slots(slots, now)

    def _poll_metrics(self, now):
        status, body = self._llama("/metrics")
        if status != 200:
            self.src["metrics"].mark_fail(f"/metrics HTTP {status}", now)
            return
        m = P.parse_prometheus(body)
        self.src["metrics"].mark_ok()
        self.state["metrics"] = m
        sp = {}
        prev = self._prev_metrics
        if prev:
            def rate(tok, sec):
                dt = m.get(sec, 0) - prev.get(sec, 0)
                dn = m.get(tok, 0) - prev.get(tok, 0)
                return dn / dt if dt > 0 and dn >= 0 else None
            sp["gen_tok_s"] = rate("llamacpp:tokens_predicted_total", "llamacpp:tokens_predicted_seconds_total")
            sp["prefill_tok_s"] = rate("llamacpp:prompt_tokens_total", "llamacpp:prompt_seconds_total")
            new = m.get("llamacpp:prompt_tokens_total", 0) - prev.get("llamacpp:prompt_tokens_total", 0)
            cached = m.get("llamacpp:prompt_tokens_cached_total", 0) - prev.get("llamacpp:prompt_tokens_cached_total", 0)
            if new + cached > 0 and new >= 0 and cached >= 0:
                self.rings["cache"].push(now, 100.0 * cached / (new + cached))
            self.rings["gen"].push(now, sp["gen_tok_s"])
            self.rings["prefill"].push(now, sp["prefill_tok_s"])
        self._prev_metrics = m
        drafted = m.get("llamacpp:spec_decode_num_draft_tokens_total")
        acc = m.get("llamacpp:spec_decode_num_accepted_tokens_total")
        sp["mtp_accept"] = acc / drafted if drafted else None
        drafts = m.get("llamacpp:spec_decode_num_drafts_total")
        sp["mtp_pos"] = [m.get(f'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="{i}"}}') / drafts
                         if drafts and m.get(f'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{{position="{i}"}}') is not None
                         else None for i in range(3)]
        self.rings["mtp"].push(now, sp["mtp_accept"])
        self.state["speed"] = sp

    def _poll_gpu(self, now):
        if not self.gpu_dirs:
            self.gpu_dirs = P.find_gpu_dirs(self.cfg["drm_root"], self.cfg["gpu_buses"])
        gpus, errors = [], []
        for i, bus in enumerate(self.cfg["gpu_buses"]):
            d = self.gpu_dirs.get(bus)
            g = P.read_gpu(d) if d else {"load_pct": None, "vram_used": None, "vram_total": None, "junction_c": None,
                                         "power_w": None, "power_cap_w": None, "link": None, "error": "card not found"}
            if g["error"]:
                errors.append(f"{bus}: {g['error']}")
            g = {k: v for k, v in g.items() if k != "error"}
            g["bus"] = bus
            gpus.append(g)
            self.rings[f"gpu{i}_load"].push(now, g["load_pct"])
        self.state["gpus"] = gpus
        self.state["fans"] = P.read_fans(self.cfg["hwmon_root"])
        if errors:
            self.src["gpu"].mark_fail("; ".join(errors), now)
            if any("card not found" in e for e in errors):
                self.gpu_dirs = {}
        else:
            self.src["gpu"].mark_ok()

    def _poll_host(self, now):
        try:
            busy, total = P.cpu_times(self.read_text("/proc/stat"))
            used, mem_total = P.mem_from_meminfo(self.read_text("/proc/meminfo"))
            rs, ws = P.disk_sectors(self.read_text("/proc/diskstats"), self.cfg["disk_devices"])
            arc, arc_max = P.arc_from_arcstats(self.read_text("/proc/spl/kstat/zfs/arcstats"))
        except (OSError, ValueError, KeyError) as e:
            self.src["host"].mark_fail(f"host: {type(e).__name__}", now)
            return
        h = {"cpu_pct": None, "mem_used": used, "mem_total": mem_total, "disk_read_mbs": None,
             "disk_write_mbs": None, "arc_bytes": arc, "arc_max": arc_max}
        if self._prev_cpu:
            (pb, pt, pnow) = self._prev_cpu
            if total > pt:
                h["cpu_pct"] = 100.0 * (busy - pb) / (total - pt)
            if self._prev_disk and now > pnow:
                (pr, pw) = self._prev_disk
                h["disk_read_mbs"] = (rs - pr) * 512 / 1e6 / (now - pnow)
                h["disk_write_mbs"] = (ws - pw) * 512 / 1e6 / (now - pnow)
        self._prev_cpu, self._prev_disk = (busy, total, now), (rs, ws)
        self.rings["host_cpu"].push(now, h["cpu_pct"])
        self.state["host"] = h
        self.src["host"].mark_ok()

    # ---- services, timer, escalation
    def poll_services(self, now):
        with self.lock:
            sv = self.state["services"]
            status, body = self.http_get(self.cfg["router_url"] + "/healthz", {})
            try:
                hz = json.loads(body) if status == 200 else None
            except ValueError:
                hz = None
            sv["router"] = {"ok": bool(hz and hz.get("ok")), "upstream": (hz or {}).get("upstream", {}),
                            "profile": (hz or {}).get("active_chat_profile")}
            for name in ("embed", "rerank"):
                st, mb = self.http_get(self.cfg[f"{name}_url"] + "/metrics",
                                       {"Authorization": f"Bearer {self._key or ''}"})
                m = P.parse_prometheus(mb) if st == 200 else {}
                cur = m.get("llamacpp:n_decode_total")
                prev = self._prev_embed.get(name)
                act = None
                if cur is not None and prev is not None and now > prev[1] and cur >= prev[0]:
                    act = (cur - prev[0]) * 60.0 / (now - prev[1])
                if cur is not None:
                    self._prev_embed[name] = (cur, now)
                sv[name] = {"ok": st == 200, "activity_per_min": act}
            if sv["router"]["ok"]:
                self.src["services"].mark_ok()
            else:
                self.src["services"].mark_fail(f"router /healthz HTTP {status}", now)

    def poll_timer(self, now):
        rc, out = self.run_cmd(["pct", "exec", str(self.cfg["chat_ct"]), "--", "systemctl", "show",
                                "llamacpp-chat-restart.timer", "-p", "NextElapseUSecRealtime"])
        nxt = P.parse_next_elapse(out) if rc == 0 else None
        stamp = None
        pid = find_chat_pid()
        if pid:
            try:
                stamp = float(self.read_text(f"/proc/{pid}/root/var/lib/llamacpp/last-proactive-restart").strip())
            except (OSError, ValueError):
                stamp = None
        cands = [x for x in (nxt, stamp + 43200 if stamp else None) if x]
        with self.lock:
            self.state["services"]["restart"] = {"next_possible": max(cands) if cands else None}

    def poll_escalation(self, now):
        if self._router_key is None:
            self._router_key = find_router_key()
        status, body = self.http_get(self.cfg["router_url"] + "/escalation/budget",
                                     {"Authorization": f"Bearer {self._router_key or ''}"})
        if status == 401:
            self._router_key = find_router_key()
        with self.lock:
            try:
                self.state["escalation"] = json.loads(body) if status == 200 else None
            except ValueError:
                self.state["escalation"] = None
            if self.state["escalation"] is not None:
                self.src["escalation"].mark_ok()
            else:
                self.src["escalation"].mark_fail(f"/escalation/budget HTTP {status}", now)

    # ---- journal
    def on_journal_line(self, line, now):
        ev = P.parse_journal_line(line)
        if ev is None:
            return
        with self.lock:
            self.tracker.on_journal(ev, now)
            self._drain_finished()

    def on_journal_state(self, up, now):
        with self.lock:
            self.tracker.set_journal_up(up, now)
            if up:
                self.src["journal"].mark_ok()
            else:
                self.src["journal"].mark_fail("journal stream down", now)

    def _drain_finished(self):
        for row in self.tracker.pop_finished():
            self.history.add(row)
            self.recent_done.insert(0, row)
        del self.recent_done[50:]

    # ---- outputs
    def snapshot(self, now):
        with self.lock:
            m, raw = self.state["metrics"], self.state["slots_raw"] or []
            views = self.tracker.slot_views(now)
            n_ctx = None
            for v in views:
                s = next((x for x in raw if x["id"] == v["id"]), None)
                if s:
                    v["n_ctx"] = s["n_ctx"]
                    n_ctx = n_ctx or s["n_ctx"]
            sp = dict(self.state["speed"])
            sp.setdefault("gen_tok_s", None)
            sp.setdefault("prefill_tok_s", None)
            sp.setdefault("mtp_accept", None)
            sp.setdefault("mtp_pos", [None, None, None])
            sp["gen_avg15"], sp["prefill_avg15"] = self.rings["gen"].avg(), self.rings["prefill"].avg()
            sp["cache_reuse_pct15"] = self.rings["cache"].avg()
            recent = (self.tracker.open_rows() + self.recent_done)[:20]
            return {"ts": now,
                    "sources": {n: s.as_dict() for n, s in self.src.items()},
                    "model": {"profile": self.state["services"]["router"]["profile"], "n_ctx": n_ctx,
                              "slot_count": self.cfg["slot_count"]},
                    "slots": views,
                    "queue": {"deferred": int(m["llamacpp:requests_deferred"]) if "llamacpp:requests_deferred" in m else None,
                              "processing": int(m["llamacpp:requests_processing"]) if "llamacpp:requests_processing" in m else None},
                    "speed": sp, "gpus": self.state["gpus"], "fans": self.state["fans"],
                    "host": {k: self.state["host"].get(k) for k in ("cpu_pct", "mem_used", "mem_total", "disk_read_mbs",
                                                                    "disk_write_mbs", "arc_bytes", "arc_max")},
                    "services": self.state["services"], "escalation": self.state["escalation"],
                    "recent": recent, "history_ok": self.history.ok}

    def series(self):
        with self.lock:
            return {n: r.values() for n, r in self.rings.items()}
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest scripts/files/tests/test_ld_collectors.py -q`
Expected: 6 passed.

- [ ] **Step 5: Mutation check**

Use a matched replace and the exact restore.
1. In `Hub._llama`, replace `if status == 401:` with `if False:`.
2. Run the collectors tests. Expected: `test_401_rereads_key_and_failure_marks_only_that_source` fails.
3. Restore the exact text and re-run. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/files/live_dashboard/collectors.py scripts/files/tests/test_ld_collectors.py
git commit -m "feat(dashboard): collectors and Hub - keys from /proc, isolated sources, journal follower"
```

---

### Task 5: HTTP server and SSE stream

**Files:**
- Create:
  - `scripts/files/live_dashboard/server.py`
  - `scripts/files/live_dashboard/main.py`
- Test: `scripts/files/tests/test_ld_server.py`

**Interfaces:**
- Consumes (Task 4): `Hub.snapshot(now)`, `Hub.series()`, `Hub.history.recent(limit)`, `Hub.history.bands(days, now)`.
- Produces:
  - `make_server(hub, cfg, static_dir) -> ThreadingHTTPServer`, with routes:
    - `GET /` → `static/index.html`
    - `GET /healthz` → `{"status": "ok"}`
    - `GET /api/stream` → SSE, one `data: <snapshot>` per second; at most `max_stream_clients`, else 503
    - `GET /api/series` → `Hub.series()`
    - `GET /api/requests?limit=N` → `{"rows": [...]}`, limit clamped by History
    - `GET /api/history?days=N` → `{"bands": [...], "days": N}`, days clamped to 1..30
    - anything else → 404
  - `main.main(argv=None) -> int` loads `/etc/live-dashboard.json` (DEFAULTS merged), starts the pollers and the journal follower, prunes nightly, and serves forever.

- [ ] **Step 1: Write the failing tests** in `scripts/files/tests/test_ld_server.py`

```python
"""live_dashboard.server -- routes, SSE cadence, client cap, caps on query params, no secrets."""
import http.client
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard import server as S  # noqa: E402


class FakeHistory:
    def __init__(self):
        self.calls = []

    def recent(self, limit):
        self.calls.append(("recent", limit))
        return [{"key": "k", "status": "done"}]

    def bands(self, days, now):
        self.calls.append(("bands", days))
        return [{"band": "0-8K", "n": 1, "gen_tok_s": 40.0, "prefill_tok_s": 500.0}]


class FakeHub:
    def __init__(self):
        self.history = FakeHistory()
        self.n = 0

    def snapshot(self, now):
        self.n += 1
        return {"ts": now, "n": self.n}

    def series(self):
        return {"gen": [[1, 2.0]]}


def start(tmp_path, max_clients=2, interval=0.05):
    (tmp_path / "index.html").write_text("<!DOCTYPE html><title>Live</title>")
    hub = FakeHub()
    cfg = {"bind_host": "127.0.0.1", "bind_port": 0, "max_stream_clients": max_clients, "stream_interval_s": interval}
    srv = S.make_server(hub, cfg, str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hub


def get(srv, path):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read().decode(), r.getheader("Content-Type")


def test_routes(tmp_path):
    srv, hub = start(tmp_path)
    try:
        assert get(srv, "/")[0] == 200 and "Live" in get(srv, "/")[1]
        assert json.loads(get(srv, "/healthz")[1]) == {"status": "ok"}
        assert json.loads(get(srv, "/api/series")[1]) == {"gen": [[1, 2.0]]}
        st, body, _ = get(srv, "/api/requests?limit=99999")
        assert st == 200 and json.loads(body)["rows"][0]["key"] == "k" and ("recent", 99999) in hub.history.calls
        st, body, _ = get(srv, "/api/history?days=400")
        assert st == 200 and json.loads(body)["days"] == 30
        assert get(srv, "/api/requests?limit=abc")[0] == 400
        assert get(srv, "/nope")[0] == 404
        assert get(srv, "/../etc/passwd")[0] == 404
    finally:
        srv.shutdown()


def read_events(srv, n, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=timeout)
    c.request("GET", "/api/stream")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    events = []
    buf = b""
    while len(events) < n:
        buf += r.read1(4096)
        while b"\n\n" in buf:
            chunk, buf = buf.split(b"\n\n", 1)
            if chunk.startswith(b"data: "):
                events.append(json.loads(chunk[6:]))
    c.close()
    return events


def test_stream_sends_snapshots(tmp_path):
    srv, _ = start(tmp_path)
    try:
        ev = read_events(srv, 3)
        assert [e["n"] for e in ev] == sorted(e["n"] for e in ev) and len(ev) == 3
    finally:
        srv.shutdown()


def test_client_cap_and_slot_released(tmp_path):
    srv, _ = start(tmp_path, max_clients=1)
    try:
        c1 = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c1.request("GET", "/api/stream")
        r1 = c1.getresponse()
        assert r1.status == 200
        r1.read1(100)
        assert get(srv, "/api/stream")[0] == 503
        c1.close()
        deadline = time.time() + 5
        ok = False
        while time.time() < deadline and not ok:
            time.sleep(0.1)
            try:
                ok = len(read_events(srv, 1, timeout=2)) == 1
            except AssertionError:
                ok = False
        assert ok
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python -m pytest scripts/files/tests/test_ld_server.py -q`
Expected: ERROR, `No module named 'live_dashboard.server'`.

- [ ] **Step 3: Implement `scripts/files/live_dashboard/server.py`**

```python
"""HTTP + SSE for the live dashboard (spec §2, §5). Read-only; numbers only; client cap on the stream."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def make_server(hub, cfg, static_dir):
    lock = threading.Lock()
    clients = {"n": 0}
    interval = float(cfg.get("stream_interval_s", 1.0))
    cap = int(cfg.get("max_stream_clients", 8))
    with open(os.path.join(static_dir, "index.html"), "rb") as f:
        page = f.read()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _int(self, q, name, default):
            v = q.get(name, [str(default)])[0]
            if not v.isdigit():
                raise ValueError(name)
            return int(v)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, page, "text/html; charset=utf-8")
                if u.path == "/healthz":
                    return self._send(200, json.dumps({"status": "ok"}))
                if u.path == "/api/series":
                    return self._send(200, json.dumps(hub.series()))
                if u.path == "/api/requests":
                    return self._send(200, json.dumps({"rows": hub.history.recent(self._int(q, "limit", 50))}))
                if u.path == "/api/history":
                    days = max(1, min(self._int(q, "days", 7), 30))
                    return self._send(200, json.dumps({"bands": hub.history.bands(days, time.time()), "days": days}))
                if u.path == "/api/stream":
                    return self._stream()
            except ValueError as e:
                return self._send(400, json.dumps({"error": f"bad parameter {e}"}))
            return self._send(404, json.dumps({"error": "not found"}))

        def _stream(self):
            with lock:
                if clients["n"] >= cap:
                    return self._send(503, json.dumps({"error": "too many live viewers", "retry_after_s": 10}))
                clients["n"] += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    self.wfile.write(b"data: " + json.dumps(hub.snapshot(time.time())).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with lock:
                    clients["n"] -= 1

    srv = ThreadingHTTPServer((cfg["bind_host"], int(cfg["bind_port"])), Handler)
    srv.daemon_threads = True
    return srv
```

- [ ] **Step 4: Implement `scripts/files/live_dashboard/main.py`**

```python
"""Entry point: python3 -m live_dashboard.main --config /etc/live-dashboard.json"""
import argparse
import json
import logging
import os
import threading
import time

from . import collectors as C
from .server import make_server

log = logging.getLogger("live_dashboard")


def load_config(path):
    cfg = dict(C.DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def _every(period, fn, stop):
    def run():
        while not stop.is_set():
            t0 = time.time()
            try:
                fn(t0)
            except Exception:
                log.exception("poller %s failed", getattr(fn, "__name__", fn))
            stop.wait(max(0.0, period - (time.time() - t0)))
    threading.Thread(target=run, daemon=True, name=f"poll-{getattr(fn, '__name__', 'x')}").start()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/live-dashboard.json")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)
    os.makedirs(os.path.dirname(cfg["history_path"]), exist_ok=True)
    hub = C.Hub(cfg, C.default_http_get, C.default_read_text, C.default_run_cmd)
    stop = threading.Event()
    _every(1.0, hub.poll_fast, stop)
    _every(5.0, hub.poll_services, stop)
    _every(60.0, hub.poll_timer, stop)
    _every(30.0, hub.poll_escalation, stop)
    _every(86400.0, hub.history.prune, stop)
    follower = C.JournalFollower(cfg["journal_argv"], on_line=lambda l: hub.on_journal_line(l, time.time()),
                                 on_state=lambda up: hub.on_journal_state(up, time.time()))
    follower.start()
    srv = make_server(hub, cfg, os.path.join(os.path.dirname(__file__), "static"))
    log.info("live dashboard on %s:%s", cfg["bind_host"], cfg["bind_port"])
    try:
        srv.serve_forever()
    finally:
        stop.set()
        follower.stop()
        hub.history.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest scripts/files/tests/test_ld_server.py -q`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/files/live_dashboard/server.py scripts/files/live_dashboard/main.py scripts/files/tests/test_ld_server.py
git commit -m "feat(dashboard): HTTP server, SSE stream with viewer cap, entry point"
```

---

### Task 6: The page

**Files:**
- Create: `scripts/files/live_dashboard/static/index.html`
- Test: `scripts/files/tests/test_ld_page.py`

**Interfaces:**
- Consumes:
  - `GET /api/stream` (snapshot schema), `GET /api/series` (backfill on connect), `GET /api/requests?limit=20` (fills the table before the first event), `GET /api/history?days=7` (speed vs context, refreshed every 60 s);
  - the visual source of truth, `docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html`.

- [ ] **Step 1: Write the failing test** in `scripts/files/tests/test_ld_page.py`

```python
"""The page: self-contained (no external URLs), bound to the snapshot schema, renders stale sources."""
import os
import re

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "live_dashboard", "static", "index.html")


def page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def test_self_contained_no_external_urls():
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", page())
    assert "cdn" not in page().lower()


def test_uses_the_api_and_schema_fields():
    p = page()
    for needle in ('new EventSource("/api/stream")', '"/api/series"', '"/api/requests?limit=20"',
                   '"/api/history?days=7"', ".slots", ".speed", ".gpus", ".host", ".services", ".escalation",
                   ".recent", ".sources", "stale_since", "history_ok", "prefill_tok_s", "gen_tok_s",
                   "cached_tokens", "progress"):
        assert needle in p, needle


def test_no_text_fields_rendered():
    p = page()
    assert "prompt_text" not in p and "content" not in re.findall(r"\.(\w+)", p)
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `python -m pytest scripts/files/tests/test_ld_page.py -q`
Expected: FAIL, file not found.

- [ ] **Step 3: Build `static/index.html` from the mock-up**

Start from a copy of `docs/superpowers/specs/2026-10-16-live-dashboard-mockup.html` and keep its CSS, layout and sections exactly. Replace every hard-coded number with an element id, and add one inline `<script>`:

```html
<script>
"use strict";
const $ = id => document.getElementById(id);
const fmt = (v, d = 0) => v == null ? "—" : Number(v).toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: d});
const GB = b => b == null ? "—" : (b / 1e9).toFixed(1);
const series = {gen: [], prefill: [], cache: [], mtp: [], gpu0_load: [], gpu1_load: [], host_cpu: []};
const WINDOW_S = 900;

function spark(svgId, pts, color) {
  const svg = $(svgId); if (!svg) return;
  if (!pts.length) { svg.innerHTML = ""; return; }
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1, vs = pts.map(p => p[1]);
  const lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1;
  const xy = pts.map(([t, v]) => `${(100 * (t - t0) / ((t1 - t0) || 1)).toFixed(2)},${(28 - 26 * (v - lo) / span).toFixed(2)}`).join(" ");
  svg.innerHTML = `<polyline fill="none" stroke="${color}" stroke-width="1.6" points="${xy}"/>`;
}
function push(name, t, v) {
  if (v == null) return;
  const s = series[name]; s.push([t, v]);
  while (s.length && s[0][0] < t - WINDOW_S) s.shift();
}
function staleMark(id, src) {
  const el = $(id); if (!el) return;
  el.classList.toggle("stale", !src.ok);
  el.title = src.ok ? "" : `unavailable since ${new Date(src.stale_since * 1000).toLocaleTimeString()} — ${src.error || ""}`;
}
function slotCard(v) {
  // state tag, progress bar (reading: v.progress; generating: none), token counts, tok/s, cache reuse, context fill
  const pct = v.state === "reading" && v.progress != null ? Math.round(100 * v.progress) : null;
  $(`slot${v.id}-state`).textContent = {idle: "Idle", reading: "Reading prompt", generating: "Generating", restarting: "Restarting"}[v.state];
  $(`slot${v.id}-state`).dataset.state = v.state;
  $(`slot${v.id}-bar`).style.width = (pct ?? (v.state === "generating" ? 100 : 0)) + "%";
  $(`slot${v.id}-line1`).textContent = v.state === "reading" ? `${fmt(v.processed_tokens)} tokens · ${pct ?? "—"}%`
    : v.state === "generating" ? `${fmt(v.output_tokens)} tokens out` : v.last_done ? `last: ${Math.round(Date.now() / 1000 - v.last_done)} s ago` : "—";
  $(`slot${v.id}-tps`).textContent = v.tok_s == null ? "—" : `${fmt(v.tok_s, 1)} tok/s`;
  $(`slot${v.id}-line2`).textContent = v.cached_tokens != null ? `reused from cache: ${fmt(v.cached_tokens)}` : "";
  $(`slot${v.id}-ctx`).textContent = v.ctx_used != null && v.n_ctx ? `ctx ${Math.round(v.ctx_used / 1024)}K / ${Math.round(v.n_ctx / 1024)}K` : "";
}
function row(r) {
  const cls = r.status === "reading" ? "reading" : r.status === "generating" ? "generating" : r.slow ? "slow" : r.status;
  const t = new Date((r.finished || r.started) * 1000).toLocaleTimeString();
  return `<tr><td>${t}</td><td>${r.slot}</td><td><span class="tag ${cls}">${r.slow && r.status === "done" ? "slow" : r.status}</span></td>
    <td>${fmt(r.prompt_tokens)}</td><td>${fmt(r.cached_tokens)}</td><td>${fmt(r.output_tokens)}</td>
    <td>${fmt(r.prefill_tok_s)}</td><td>${fmt(r.gen_tok_s, 1)}</td><td>${r.duration_s == null ? "—" : fmt(r.duration_s, 1) + " s"}</td></tr>`;
}
function render(s) {
  const now = s.ts;
  for (const [id, key] of [["panel-slots", "slots"], ["card-gen", "metrics"], ["card-prefill", "metrics"],
                           ["card-cache", "metrics"], ["card-mtp", "metrics"], ["panel-gpu0", "gpu"], ["panel-gpu1", "gpu"],
                           ["panel-host", "host"], ["panel-escalation", "escalation"], ["svc-row", "services"]])
    staleMark(id, s.sources[key]);
  $("journal-banner").hidden = s.sources.journal.ok;
  $("history-banner").hidden = s.history_ok;
  s.slots.forEach(slotCard);
  $("queue").textContent = fmt(s.queue.deferred);
  $("header-pills").innerHTML = s.slots.map(v => `<span class="pill"><span class="dot ${v.state}"></span>slot ${v.id} ${v.state}${v.state === "reading" && v.progress != null ? " · " + Math.round(100 * v.progress) + "%" : v.tok_s != null ? " · " + fmt(v.tok_s, 1) + " tok/s" : ""}</span>`).join("");
  $("model-pill").textContent = `${s.model.profile || "—"} · ${s.model.slot_count} × ${s.model.n_ctx ? Math.round(s.model.n_ctx / 1024) + "K" : "—"}`;
  $("updated").textContent = `updated ${new Date(now * 1000).toLocaleTimeString()}`;
  const sp = s.speed;
  $("gen-now").textContent = fmt(sp.gen_tok_s, 1); $("gen-avg").textContent = fmt(sp.gen_avg15, 1);
  $("prefill-now").textContent = fmt(sp.prefill_tok_s); $("prefill-avg").textContent = fmt(sp.prefill_avg15);
  $("cache-now").textContent = fmt(sp.cache_reuse_pct15); $("mtp-now").textContent = sp.mtp_accept == null ? "—" : fmt(100 * sp.mtp_accept);
  $("mtp-pos").textContent = sp.mtp_pos.map((p, i) => `pos ${i + 1}: ${p == null ? "—" : fmt(100 * p) + "%"}`).join(" · ");
  push("gen", now, sp.gen_tok_s); push("prefill", now, sp.prefill_tok_s); push("cache", now, sp.cache_reuse_pct15); push("mtp", now, sp.mtp_accept);
  s.gpus.forEach((g, i) => {
    $(`gpu${i}-load`).textContent = fmt(g.load_pct); $(`gpu${i}-vram`).textContent = GB(g.vram_used);
    $(`gpu${i}-vramtot`).textContent = GB(g.vram_total); $(`gpu${i}-temp`).textContent = fmt(g.junction_c);
    $(`gpu${i}-power`).textContent = fmt(g.power_w); $(`gpu${i}-cap`).textContent = fmt(g.power_cap_w);
    $(`gpu${i}-link`).textContent = g.link || "—"; push(`gpu${i}_load`, now, g.load_pct);
  });
  $("fans").textContent = `fans ${fmt(s.fans.pwm5_pct)}% / ${fmt(s.fans.pwm6_pct)}%`;
  const h = s.host;
  $("host-cpu").textContent = fmt(h.cpu_pct); $("host-ram").textContent = GB(h.mem_used); $("host-ramtot").textContent = GB(h.mem_total);
  $("host-disk").textContent = fmt(h.disk_read_mbs, 1); $("host-arc").textContent = GB(h.arc_bytes); push("host_cpu", now, h.cpu_pct);
  const sv = s.services;
  $("svc-router").className = "dot " + (sv.router.ok ? "ok" : "bad");
  $("svc-embed").textContent = `embed · ${fmt(sv.embed.activity_per_min)}/min`; $("svc-embed-dot").className = "dot " + (sv.embed.ok ? "ok" : "bad");
  $("svc-rerank-dot").className = "dot " + (sv.rerank.ok ? "ok" : "bad");
  $("svc-restart").textContent = sv.restart.next_possible ? `next possible chat restart ≥ ${new Date(sv.restart.next_possible * 1000).toLocaleTimeString()} (idle only)` : "chat restart: —";
  const e = s.escalation;
  if (e) {
    $("esc-month").textContent = "$" + fmt(e.month.spent_usd, 2); $("esc-monthcap").textContent = "$" + fmt(e.month.cap_usd);
    $("esc-day").textContent = "$" + fmt(e.day.spent_usd, 2); $("esc-daycap").textContent = "$" + fmt(e.day.cap_usd, 2);
    $("esc-left").textContent = `ask ${e.calls_left["claude-ask"]} · auto ${e.calls_left["claude-auto"]} · hard ${e.calls_left["claude-hard"]}`;
    $("esc-ring").setAttribute("stroke-dasharray", `${Math.min(100, 100 * e.month.spent_usd / (e.month.cap_usd || 1)).toFixed(1)} 100`);
    const last = e.recent && e.recent[0];
    $("esc-last").textContent = last ? `last: ${last.alias} · ${fmt(last.input_tokens)} in / ${fmt(last.output_tokens)} out · $${fmt(last.cost_usd, 3)}` : "no calls yet";
  }
  $("recent-body").innerHTML = s.recent.map(row).join("");
  spark("spark-gen", series.gen, "#22c55e"); spark("spark-prefill", series.prefill, "#3b82f6");
  spark("spark-cache", series.cache, "#2dd4bf"); spark("spark-mtp", series.mtp, "#a78bfa");
  spark("spark-gpu0", series.gpu0_load, "#f59e0b"); spark("spark-gpu1", series.gpu1_load, "#f59e0b"); spark("spark-host", series.host_cpu, "#8a93a6");
}
async function loadHistory() {
  try {
    const h = await (await fetch("/api/history?days=7")).json();
    // draw two polylines (gen_tok_s green, prefill_tok_s blue) across the 5 bands into #ctx-chart, scaled to max
    const bands = ["0-8K", "8-32K", "32-64K", "64-96K", "96-128K"], by = Object.fromEntries(h.bands.map(b => [b.band, b]));
    const line = (key, color) => {
      const vals = bands.map(b => by[b] ? by[b][key] : null), max = Math.max(1, ...vals.filter(v => v != null));
      const pts = vals.map((v, i) => v == null ? null : `${30 + i * 45},${95 - 85 * v / max}`).filter(Boolean).join(" ");
      return `<polyline fill="none" stroke="${color}" stroke-width="1.8" points="${pts}"/>`;
    };
    $("ctx-lines").innerHTML = line("gen_tok_s", "#22c55e") + line("prefill_tok_s", "#3b82f6");
    $("ctx-n").textContent = h.bands.reduce((a, b) => a + b.n, 0);
  } catch (_) { /* keep last chart */ }
}
async function boot() {
  try { Object.assign(series, await (await fetch("/api/series")).json()); } catch (_) {}
  try { $("recent-body").innerHTML = (await (await fetch("/api/requests?limit=20")).json()).rows.map(row).join(""); } catch (_) {}
  loadHistory(); setInterval(loadHistory, 60000);
  const es = new EventSource("/api/stream");
  es.onmessage = m => { $("conn").hidden = true; render(JSON.parse(m.data)); };
  es.onerror = () => { $("conn").hidden = false; };
}
boot();
</script>
```

Markup requirements, matching the ids the script uses:
- **Header:** `header-pills`, `model-pill`, `updated`; a hidden `conn` banner ("reconnecting…"), a hidden `journal-banner` ("journal stream down — rows are approximate") and a hidden `history-banner` ("history paused").
- **Slots panel:** `panel-slots` and `queue`. For `slot0`…`slot2`: `-state`, `-bar`, `-line1`, `-tps`, `-line2`, `-ctx`.
- **Speed cards:** `card-gen`, `card-prefill`, `card-cache`, `card-mtp`. Values `gen-now`, `gen-avg`, `prefill-now`, `prefill-avg`, `cache-now`, `mtp-now`, `mtp-pos`. Sparkline `<svg>` ids `spark-gen`, `spark-prefill`, `spark-cache`, `spark-mtp` (viewBox `0 0 100 30`, `preserveAspectRatio="none"`).
- **GPU panels:** `panel-gpu0` and `panel-gpu1`, with `gpu{i}-load`, `-vram`, `-vramtot`, `-temp`, `-power`, `-cap`, `-link`, sparkline `spark-gpu{i}`, and `fans` in GPU 0's footer.
- **Host panel:** `panel-host`, with `host-cpu`, `host-ram`, `host-ramtot`, `host-disk`, `host-arc` and `spark-host`.
- **Services row:** `svc-row`, with dot ids `svc-router`, `svc-embed-dot`, `svc-rerank-dot` and text ids `svc-embed`, `svc-restart`.
- **Escalation panel:** `panel-escalation`, with the ring `<circle id="esc-ring">`, `esc-month`, `esc-monthcap`, `esc-day`, `esc-daycap`, `esc-left`, `esc-last`.
- **Speed vs context:** `<g id="ctx-lines">` inside the chart's `<svg>`, plus `ctx-n` (request count).
- **Table:** `<tbody id="recent-body">` with headers Time, Slot, Status, Prompt, Reused, Output, Prefill t/s, Gen t/s, Duration. Thinking % is deferred by the spec, so drop that column.

CSS additions:
- `.stale { opacity: .45; filter: grayscale(1); }`
- dot colours `.dot.reading` (blue), `.dot.generating` (green), `.dot.idle` (grey), `.dot.restarting` (amber), `.dot.ok` (green), `.dot.bad` (red)
- tag colours for `.tag.reading`, `.tag.generating`, `.tag.slow` (amber), `.tag.cancelled`, `.tag.interrupted` (red) and `.tag.approx` (grey)
- the banners: a full-width strip under the header, amber background

- [ ] **Step 4: Run the page tests and open the page against a fake hub**

Run: `python -m pytest scripts/files/tests/test_ld_page.py -q`
Expected: 3 passed.

Then view it, by either method:
- run `python -c "import sys; sys.path.insert(0,'scripts/files'); from tests.test_ld_server import *"`;
- or, simpler, start `live_dashboard.server.make_server` in a Python shell with a fake hub that returns the snapshot from `test_ld_collectors.World`, bound to `127.0.0.1:8899`, and open it in a browser.

Confirm the layout matches the mock-up and that blanking a source (`ok: false`) greys out its cards. Record what you checked in your report.

- [ ] **Step 5: Commit**

```bash
git add scripts/files/live_dashboard/static/index.html scripts/files/tests/test_ld_page.py
git commit -m "feat(dashboard): live page built from the approved mock-up"
```

---

### Task 7: Deploy script, unit and end-to-end test

**Files:**
- Create:
  - `scripts/files/live-dashboard.service`
  - `scripts/64-live-dashboard.sh`
  - `scripts/files/tests/test_ld_e2e.py`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: `bash scripts/64-live-dashboard.sh install|side-by-side|cutover|rollback|status`, run as root on the Proxmox host from a detached worktree. It:
  - installs the package to `/opt/live-dashboard/live_dashboard/`;
  - writes `/etc/live-dashboard.json` (only the port differs between side-by-side and cut-over);
  - writes `/var/lib/live-dashboard/` (0700);
  - writes `/root/live-dashboard-rollback.sh`.

- [ ] **Step 1: Write the failing end-to-end test** in `scripts/files/tests/test_ld_e2e.py`

```python
"""End to end: the real Hub + server against the fake cluster World from test_ld_collectors, over HTTP; plus the
deploy script's shape."""
import http.client
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

from live_dashboard import collectors as C  # noqa: E402
from live_dashboard import server as S  # noqa: E402
from test_ld_collectors import World, fx  # noqa: E402

ROOT = os.path.join(HERE, "..", "..")


def test_stream_over_http_with_journal_request(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(C, "find_llama_key", lambda *a, **k: "CHATKEY")
    monkeypatch.setattr(C, "find_router_key", lambda *a, **k: "ROUTERKEY")
    cfg = dict(C.DEFAULTS, history_path=str(tmp_path / "h.db"), drm_root=str(tmp_path / "x"),
               hwmon_root=str(tmp_path / "y"), bind_host="127.0.0.1", bind_port=0, stream_interval_s=0.05)
    hub = C.Hub(cfg, w.http_get, w.read_text, w.run_cmd)
    for i, line in enumerate(fx("journal_chat_last10min_raw.txt").splitlines()):
        hub.on_journal_line(line, 1000.0 + i)
    hub.poll_fast(1100.0)
    hub.poll_services(1100.0)
    srv = S.make_server(hub, cfg, os.path.join(ROOT, "files", "live_dashboard", "static"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/stream")
        r = c.getresponse()
        buf = b""
        while b"\n\n" not in buf:
            buf += r.read1(65536)
        snap = json.loads(buf.split(b"\n\n", 1)[0][6:])
        assert snap["recent"][0]["task"] == 4772 and snap["sources"]["slots"]["ok"] is True
        assert "CHATKEY" not in buf.decode() and "ROUTERKEY" not in buf.decode()
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/")
        assert c.getresponse().status == 200
    finally:
        srv.shutdown()
        hub.history.close()


def test_deploy_script_shape():
    s = open(os.path.join(ROOT, "64-live-dashboard.sh"), encoding="utf-8").read()
    for needle in ("install)", "side-by-side)", "cutover)", "rollback)", "status)",
                   "/opt/live-dashboard/live_dashboard", "/etc/live-dashboard.json", "/var/lib/live-dashboard",
                   "/root/live-dashboard-rollback.sh", "systemctl disable --now cluster-monitor",
                   "systemctl enable --now cluster-monitor", '"bind_port": 8889', '"bind_port": 8888'):
        assert needle in s, needle
    u = open(os.path.join(ROOT, "files", "live-dashboard.service"), encoding="utf-8").read()
    assert "python3 -m live_dashboard.main --config /etc/live-dashboard.json" in u
    assert "WorkingDirectory=/opt/live-dashboard" in u and "Restart=always" in u
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest scripts/files/tests/test_ld_e2e.py -q`
Expected: the e2e test may pass already. The deploy-shape test fails because the script is missing.

- [ ] **Step 3: Create `scripts/files/live-dashboard.service`**

```ini
[Unit]
Description=Live inference dashboard (replaces cluster-monitor)
After=network-online.target pve-container@151.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/live-dashboard
ExecStart=/usr/bin/python3 -m live_dashboard.main --config /etc/live-dashboard.json
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Create `scripts/64-live-dashboard.sh`**

```bash
#!/usr/bin/env bash
# 64-live-dashboard.sh -- install / side-by-side / cut-over / rollback for the live inference dashboard
# (spec docs/superpowers/specs/2026-10-16-live-dashboard-design.md). Run as root on the Proxmox host from a detached
# worktree of main (the host checkout lags main). Side-by-side runs on :8889 next to cluster-monitor on :8888;
# cut-over stops cluster-monitor (files kept) and moves the dashboard to :8888.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/files/live_dashboard"
UNIT_SRC="$HERE/files/live-dashboard.service"
DEST=/opt/live-dashboard
CFG=/etc/live-dashboard.json
ROLLBACK=/root/live-dashboard-rollback.sh

write_cfg() {
  local port="$1" tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
{"bind_host": "192.168.6.175", "bind_port": $port, "history_path": "/var/lib/live-dashboard/history.db"}
EOF
  install -m 0644 "$tmp" "$CFG"
  rm -f "$tmp"
}

install_files() {
  [[ -d "$SRC" && -f "$UNIT_SRC" ]] || { echo "missing $SRC or $UNIT_SRC" >&2; exit 1; }
  install -d -m 0755 "$DEST"
  rm -rf "$DEST/live_dashboard.new"
  cp -a "$SRC" "$DEST/live_dashboard.new"
  find "$DEST/live_dashboard.new" -name '__pycache__' -prune -exec rm -rf {} +
  rm -rf "$DEST/live_dashboard"
  mv "$DEST/live_dashboard.new" "$DEST/live_dashboard"
  install -d -m 0700 /var/lib/live-dashboard
  install -m 0644 "$UNIT_SRC" /etc/systemd/system/live-dashboard.service
  systemctl daemon-reload
  cat > "$ROLLBACK" <<'EOF'
#!/bin/bash
# Restore the old cluster monitor on :8888 and stop the live dashboard.
systemctl disable --now live-dashboard || true
systemctl enable --now cluster-monitor
systemctl --no-pager --lines=0 status cluster-monitor
EOF
  chmod 700 "$ROLLBACK"
}

case "${1:-}" in
  install)
    install_files
    [[ -f "$CFG" ]] || write_cfg 8889
    echo "installed; run '$0 side-by-side' to start on :8889" ;;
  side-by-side)
    install_files
    write_cfg 8889
    # "bind_port": 8889 -- next to cluster-monitor on :8888
    systemctl enable --now live-dashboard
    systemctl restart live-dashboard
    sleep 2
    curl -s -m 5 http://192.168.6.175:8889/healthz; echo ;;
  cutover)
    [[ -d "$DEST/live_dashboard" ]] || { echo "not installed" >&2; exit 1; }
    systemctl disable --now cluster-monitor
    write_cfg 8888
    # "bind_port": 8888 -- the dashboard takes over the monitor's port
    systemctl restart live-dashboard
    sleep 2
    curl -s -m 5 http://192.168.6.175:8888/healthz; echo
    echo "rollback: $ROLLBACK" ;;
  rollback)
    bash "$ROLLBACK" ;;
  status)
    systemctl --no-pager --lines=5 status live-dashboard cluster-monitor || true ;;
  *)
    echo "usage: $0 install|side-by-side|cutover|rollback|status" >&2; exit 2 ;;
esac
```

The test needs the literal strings `"bind_port": 8889` and `"bind_port": 8888`. They appear in the two comments above. Keep the comments; the real value is written by `write_cfg`.

- [ ] **Step 5: Run the tests and the syntax check**

Run: `python -m pytest scripts/files/tests -q` then `bash -n scripts/64-live-dashboard.sh`
Expected: all pass; `bash -n` exits 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/files/live-dashboard.service scripts/64-live-dashboard.sh scripts/files/tests/test_ld_e2e.py
git commit -m "feat(dashboard): deploy script (side-by-side, cut-over, rollback), unit, end-to-end test"
```

---

### Task 8: Live rollout (the controller runs this, with the user's go-ahead)

This task is not done by an implementer. It runs after Tasks 1–7 are merged.

- [ ] **Step 1: Side-by-side.**
  1. On the host: `git -C /root/local-gpu-cluster fetch origin && git -C /root/local-gpu-cluster worktree add --detach /root/wf/lgc-dash origin/main`
  2. Run `bash /root/wf/lgc-dash/scripts/64-live-dashboard.sh side-by-side`.
  3. Open `http://192.168.6.175:8889/` and check every panel is live.
- [ ] **Step 2: Validate for at least a day.**
  - Compare the dashboard's per-request rows with the chat journal for 5 sampled requests: prompt, eval tokens and ms. They should match exactly.
  - Compare its 15-min generation and prefill averages with llama-server `/metrics` deltas.
  - Restart chat once in a quiet moment, with the user's OK. Confirm the slots show "restarting" and an open row becomes "interrupted".
  - Check the service's CPU (`systemctl status`) and memory stay small.
- [ ] **Step 3: Cut over, with the user's OK.**
  1. Run `bash .../64-live-dashboard.sh cutover`.
  2. Check `http://192.168.6.175:8888/`.
  3. Rollback: `/root/live-dashboard-rollback.sh`.
- [ ] **Step 4: After a clean week, retire the old monitor in a follow-up PR.**
  - Remove `scripts/files/cluster_monitor.py`, its tests, `scripts/63-cluster-monitor.sh` and `scripts/files/cluster-monitor.service`.
  - Mark `docs/cluster-monitor-design.md` superseded.
  - On the host: `systemctl disable cluster-monitor`, then delete `/opt/cluster-monitor` and `/etc/cluster-monitor.json` (back them up first).
- [ ] **Step 5: Record the outcome** in memory: URL, unit name, rollback, data sources, and the journal-driven prefill-progress gotcha.
