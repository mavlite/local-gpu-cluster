# Minisforum Phase 1 — Measurement Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the pre-registered Phase 1 gate (spec §5) end to end. The output is a committed
decision, with confidence intervals, on whether the three CPU workers add capacity without hurting
quality or the coordinator.

**Architecture:** A stdlib-only Python harness in `scripts/tools/gate/` drives two separate rulers.
- **Quality ruler:** Aider Polyglot on LXC 158, aimed at the coordinator alias or straight at one
  CPU worker.
- **Capacity probe:** an opencode coordinator fanning a frozen 6-task set out to three `task`
  subagents. Those subagents run on the GPU in arm A and on the CPU workers in arm B. A sidecar
  probes coordinator latency throughout.

Three shell scripts and one PowerShell script handle the environment:
- `gate_env.sh` pins and restores the Proxmox host;
- `worker_serve.sh` runs llama-server inside each worker VM, delivered over GuestOperations by
  `gate_guest.ps1`;
- `quality_batch.sh` / `quality_pack.sh` run and collect Polyglot on LXC 158.

`gate.py` turns raw runs into JSON and applies §5.7 mechanically.

**Tech Stack:** Python 3 (stdlib plus pytest), Bash, PowerShell 5.1 with PowerCLI, opencode 1.18.34,
aider at commit 5dc9490 with polyglot-benchmark 7e0611e, llama.cpp b11026 (coordinator),
ik_llama.cpp (workers).

**Spec:** `docs/superpowers/specs/2026-10-05-minisforum-cluster-integration-design.md` (rev 3.1,
approved 2026-10-05). Only §5 is in scope. Phases 2 and 3 and the §5.9 3-slot mode are not.

**How this plan carries its code.** All code is already written and was **executed before this plan
was written**: 66 tests pass, including real opencode runs against a scripted model server and
mutation checks on the security and restore assertions. It lives in the plan appendix
`docs/superpowers/plans/2026-10-05-minisforum-phase1-gate/`, committed together with this plan, so
the plan commit pins every byte. Tasks **move** those files into place (`git mv`) and re-run their
tests; they never retype them.

This is deliberate. Code transcribed from a plan is a defect source (repo memory: implementers copy
plan bugs faithfully). A rename with 100% similarity in `git diff -M` proves nothing was retyped.
Reviewers should still read the moved code: the plan is not evidence that it is right.

**Facts established by running (2026-10-05), which the tasks rely on:**
- **Concurrency.** opencode runs three `task` subagents concurrently: the stub workers' first calls
  overlapped completely, 0.6 s → 4.6 s with a 4 s sleep each.
- **Isolation.** It loads only the isolated `HOME` config plus the workspace `opencode.json`
  (opencode log, `loading path=` lines).
- **Agent tools.** Worker agents are offered exactly `edit, glob, grep, read, write`. The coordinator
  is offered exactly `glob, grep, read, task, todowrite`. `external_directory: deny` refused a write
  to `../escaped.txt`.
- **stdin footgun.** `opencode run` hangs forever when stdin is a non-TTY pipe, so stdin must be
  DEVNULL. The npm `opencode.cmd` shim survives a timeout kill, so launch `opencode.exe` itself.
- **Log formats.** llama-server timing lines: mainline prints `slot print_timing: id N | task T |
  prompt eval time = … ms / N tokens`. ik prints the same block unprefixed, on the line after the
  `slot … task T |` prefix. Mainline's `stop processing: n_tokens = X` gives
  `cache_n = X − prompt_n − gen_n`; ik has no such line.
- **Chat server.** The live chat runs `--parallel 1 --ctx-size 262144`. Router `/healthz` exposes
  `chat_admission.capacity` (1 now).
- **LXC 158.** It is stopped (onboot 0). The Python subset is 34 exercises. The baseline
  `qwen3.8-nothink` runs at ~78 s per case.
- **Reachability.** The workstation (`192.168.6.226`, OpenVPN `10.60.59.2`) got an immediate
  connection failure to `172.16.10.205-207:8090`, with workers not serving. Reachability is
  unproven until Task 5.

## Global Constraints

**Worker runtime and models**
- **Worker flags, verbatim (spec §3):** `-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0
  -ctv q8_0 -rtr --spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs --min-p 0`, plus mandatory
  `-cram 256 -ctx-ckpt 8` and non-thinking sampling.
- **Thinking state.** Workers are non-thinking only. The coordinator alias is `qwen3.8-nothink`,
  fixed and identical in both arms (§5.0.5).
- **Coordinator build:** llama.cpp `b11026`. Record opencode version, config SHAs and router SHA
  (§5.0.5).

**Pinned environment (§5.0.5-6, §5.4)**
- `RATE_LIMIT_CHAT` is `1000/minute` for the window and is restored afterwards (default
  `60/minute`).
- Stop for the window and restore in §5.8:
  - `llamacpp-chat-restart.timer`;
  - `redteam-mode-watch`, `redteam-mode-idle.timer` and `redteam-mode-precreate`;
  - worker apt timers, with needrestart list-only;
  - in arm A also `llamacpp-fast`;
  - `rag-refresh.timer` (user ruling 2026-10-05: embed/rerank are down, so the nightly refresh must not run;
    `gate_env.sh` stops and restores it with the rest).
- Embed/rerank state is identical in both arms (stopped). RAG is unavailable for the whole window.
- No run overlaps the 03:15 CDT `rag-refresh` window. Record `/healthz` before and after each run.

**Gate rules (§5.0.7, §5.1, §5.7)**
- **The gate commit precedes the first timed run.** Changing the task set, thresholds or
  environment afterwards voids the gate. The only exception is the §5.1 pre-registration
  amendment, made before any cross-arm data exists.
- **§5.1 variance:** if pass@2 SD > 1 task or wall-clock CV > 15%, raise the runs per arm (≥ 5)
  and/or use the full Polyglot set, before cross-arm data.
- **§5.7 decision:**
  - **Quality veto:** the worker's pass@2 CI lower bound is more than one task (1/34) below the
    coordinator.
  - **Build only if, in every run:**
    - the B−A tasks/hour 95% CI excludes 0;
    - B's probe p50 is ≤ 1.5× its own §5.3 baseline;
    - B's passed tasks ≥ A's − 1.

**Security minimums (§5.0.3)**
- Worker agents are deny-by-default: no bash, no webfetch, and `edit` only inside the scratch
  workspace.
- Each worker VM accepts `:8090` only from the benchmark clients.
- Keys are throwaway.
- llama-server is stopped and the keys removed at teardown.
- The coordinator runs in a scratch directory, never a real repo.

**Lab state**
- The lab is in **Minimal** mode.
- Memory tiering stays enabled; DRS stays partially automated; the NSX reservation stays disabled.

**Repo and secrets**
- Conventional commits, with **no `Co-Authored-By` trailer**. Run `git commit` as its own command
  (a pre-commit hook false-positives on chained commands).
- **Never push.** The user pushes.
- Secrets (router key, worker key, guest password, ESXi/SSO passwords) are passed only by
  environment variable or file. They never appear on argv, are never echoed, and never appear in
  chat or logs. Check a secret's presence by its length only.
- **Do not touch the CRS309 switch.** If a routing rule is needed, stop and ask the user to add it.
- Checks before declaring a code task done:
  `python3 -m pytest scripts/rag/tests scripts/files/tests scripts/tools/gate/tests -q`, plus
  `bash -n` on every edited `.sh`.

## Review Focus

These five failure modes are what would most likely corrupt the decision without anyone noticing,
most likely first. Each has a test or a live check in the task that owns it.

1. **An invalid capacity run is counted.** That covers a timeout, a nonzero opencode exit, a failed
   latency probe (a failure is the slowest sample, so dropping it understates latency) and a run
   made in the wrong layout. Expected behaviour: `gate.py collect` refuses the run and names why,
   and the run is redone. Pinned by `test_collect_rejects_invalid_runs` (5 cases) and
   `test_run_capacity_refuses_wrong_layout_before_starting`, both in Task 1.
2. **The task set is edited after the gate commit.** Expected behaviour: every gate command that
   reads the config refuses with "the gate is void". Pinned by
   `test_config_refuses_changed_task_set` (Task 1); the live config is checked in Task 3.
3. **A worker escapes its folder or grades itself.** Expected behaviour: writes outside the
   workspace are refused, and worker-supplied `test_*.py` files are ignored by the grader. Pinned
   by `test_mechanism_probe_parallel_and_contained` (real opencode) and
   `test_worker_supplied_tests_are_ignored` (Task 1), then re-run live in Task 2.
4. **Teardown leaves the cluster different from how it was found**, for example with RAG down or
   the restart timer off. Expected behaviour: every unit returns to its pre-pin state. Pinned by
   `test_arm_a_then_b_then_restore_returns_exact_prior_state` (Task 1), and checked live against
   the saved pre-pin record in Task 12.
5. **A client's source IP is not what the firewall allowlist assumes** (NAT, or VPN routing).
   Expected behaviour: the allowlist is learned from observed traffic, and a host outside it is
   proven blocked. No unit test can reach this; Task 5 Step 6 is a live negative check that must
   fail closed.

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/tools/gate/gate.py` | CLI: tasks, probe, baseline, capacity, scrape, polyglot, variance, decide; frozen-config loader; run validity |
| `scripts/tools/gate/gate_stats.py` | t-intervals, Welch CI, and `decide()` implementing §5.7 in order |
| `scripts/tools/gate/task_set.py` | load, hash (manifest), lay out the workspace (TASK.md + seed only), grade on hidden tests |
| `scripts/tools/gate/profiles.py` | per-run opencode config and agents (deny-by-default), isolated child env |
| `scripts/tools/gate/fanout.py` | one capacity run: layout guard, opencode launch, latency sidecar, event parse, grade |
| `scripts/tools/gate/probe.py` + `stub_llm.py` | §5.2 mechanism probe: real opencode against a scripted model server |
| `scripts/tools/gate/llama_log.py` | per-request prompt/gen tokens, `cache_n`, large-prefill count from llama-server logs |
| `scripts/tools/gate/polyglot.py` | pass@1 and pass@2 over the frozen exercise list; a missing result counts as a fail |
| `scripts/tools/gate/gate_guest.ps1` | GuestOperations exec, put and fetch into `llmbench0[1-3]` (vCenter or host-direct) |
| `scripts/tools/gate/sh/gate_env.sh` | host: preflight, pin, arm-a, arm-b, record, restore |
| `scripts/tools/gate/sh/worker_serve.sh` | worker VM: quiet, install-key, learn, sources, lock, start, stop, status, teardown |
| `scripts/tools/gate/sh/polyglot_run.sh`, `quality_batch.sh`, `quality_pack.sh` | LXC 158: one run, N runs, pack results |
| `scripts/tools/gate/tests/*` | 66 tests (fake host for `gate_env.sh`, stub server for opencode) |
| `scripts/tools/gate/README.md` | how to run the gate; footguns |
| `docs/superpowers/gate/worker-tasks/T1..T6/` | frozen capacity task set (TASK.md, seed, hidden, reference) |
| `docs/superpowers/gate/gate-config.json` | frozen thresholds, endpoints, exercise list, task manifest SHA |
| `docs/superpowers/gate/environment.jsonl`, `workers-status.txt` | §5.0.5 environment record (the gate commit) |
| `docs/superpowers/gate/results/` + `RESULTS.md` | §5.8 raw per-run JSON and the decision |

Run workspaces (each with an opencode `HOME` that holds `node_modules`) go to
`%LOCALAPPDATA%\gate-runs`, **outside the repo**. Only JSON is copied in.

**Time budget.** All of the long steps are unattended.
- Setup (Tasks 1-7): ~3 h, interactive.
- §5.1: 5 coordinator runs (~45 min each). Three worker runs go concurrently (~3-5 h each).
- Baselines: 2 × 10 min.
- Capacity: 6 runs at ~0.5-2 h each.

Plan for **~1.5-2 days of wall-clock with RAG off**, and agree the window with the user before
Task 6.

---

### Task 1: Install the harness and the frozen task set

**Files:**
- Move: `docs/superpowers/plans/2026-10-05-minisforum-phase1-gate/harness/**` → `scripts/tools/gate/**`
- Move: `docs/superpowers/plans/2026-10-05-minisforum-phase1-gate/worker-tasks/**` → `docs/superpowers/gate/worker-tasks/**`
- Create: `scripts/tools/gate/README.md`
- Modify: `AGENTS.md` (the "Python tests" line)

**Interfaces:**
- Produces: `python scripts/tools/gate/gate.py <cmd>` (subcommands as in the File structure table).
  - `task_set.manifest(root) -> str` (sha256);
  - `fanout.ARM_CAPACITY == {"A": 3, "B": 1}`;
  - `gate.load_config(path, task_root)` raises `ValueError` on a missing key or a changed task set;
  - `gate.collect(results_root, max_probe_failed) -> (q, cap)` raises `ValueError` on an invalid
    run.
- Test discovery: `scripts/tools/gate/tests` must stay **without** `__init__.py`.
  `scripts/files/tests` is a package named `tests`, so a second `tests` package would collide. This
  was verified: 231 tests pass together.

- [ ] **Step 1: Move the files**

```bash
cd /c/Users/willi/Documents/GitHub/local-gpu-cluster
P=docs/superpowers/plans/2026-10-05-minisforum-phase1-gate
mkdir -p scripts/tools/gate docs/superpowers/gate
git mv "$P/harness"/* scripts/tools/gate/
git mv "$P/worker-tasks" docs/superpowers/gate/worker-tasks
git status --short | head -60
```

Expected: 25 harness renames (9 Python modules, 1 `.ps1`, 5 `.sh`, 10 test files counting
`fake_host.py`), and 24 renames under `worker-tasks/`. The appendix directory is now empty.

- [ ] **Step 2: Run the moved tests against the committed task set (no GATE_TASK_ROOT)**

```bash
unset GATE_TASK_ROOT
python -m pytest scripts/tools/gate/tests -q -p no:cacheprovider
```

Expected: `66 passed` on this workstation (opencode 1.18.34 and bash are present). On a machine
without opencode, 3 tests skip. That is acceptable only off-workstation.

- [ ] **Step 3: Prove the security and restore tests are live (mutation, matched replace, then revert)**

Never `git checkout`/`git restore` to revert: replace the exact string back.

```bash
cat > /tmp/mut.py <<'EOF'
import sys
p, a, b = sys.argv[1:]
s = open(p, encoding="utf-8").read()
assert s.count(a) == 1, (p, s.count(a))
open(p, "w", encoding="utf-8", newline="\n").write(s.replace(a, b))
EOF
G=scripts/tools/gate
A='_WORKER_PERMS = {"edit": "allow", "bash": {"*": "deny"}'
B='_WORKER_PERMS = {"edit": "allow", "bash": {"*": "allow"}'
python /tmp/mut.py $G/profiles.py "$A" "$B"
python -m pytest $G/tests/test_profiles.py $G/tests/test_fanout.py -q -p no:cacheprovider | tail -3
python /tmp/mut.py $G/profiles.py "$B" "$A"
A='    if [ "${!var:-}" = "active" ]; then pct exec "$AMD" -- systemctl start "$u"'
B='    if false; then pct exec "$AMD" -- systemctl start "$u"'
python /tmp/mut.py $G/sh/gate_env.sh "$A" "$B"
python -m pytest $G/tests/test_gate_env.py -q -p no:cacheprovider | tail -3
python /tmp/mut.py $G/sh/gate_env.sh "$B" "$A"
git diff --stat -- $G      # must show nothing beyond the renames
```

Expected:
- The first mutant fails `test_deny_by_default_permissions` **and**
  `test_mechanism_probe_parallel_and_contained`.
- The second mutant fails `test_arm_a_then_b_then_restore_returns_exact_prior_state`.
- After the reverts, `git diff` shows no content change.

- [ ] **Step 4: Write `scripts/tools/gate/README.md`**

````markdown
# Phase-1 measurement gate harness

Implements spec `docs/superpowers/specs/2026-10-05-minisforum-cluster-integration-design.md` §5.
The runbook is the plan `docs/superpowers/plans/2026-10-05-minisforum-phase1-gate.md`; this file
is the reference.

## Pieces
| Runs on | File | Purpose |
|---|---|---|
| workstation | `gate.py` | CLI; every subcommand writes JSON. `decide` applies §5.7 mechanically |
| workstation | `gate_guest.ps1` | GuestOperations exec/put/fetch into `llmbench0[1-3]` (no SSH in workers) |
| Proxmox host | `sh/gate_env.sh` | `preflight`, `pin`, `arm-a`, `arm-b`, `record <f>`, `restore` |
| worker VM | `sh/worker_serve.sh` | installed as `/usr/local/bin/gate-worker`; frozen flags, keyed, firewall |
| LXC 158 | `sh/polyglot_run.sh`, `sh/quality_batch.sh`, `sh/quality_pack.sh` | Aider Polyglot quality ruler |

## Footguns (each found by running)
- `opencode run` waits for EOF on a non-TTY stdin forever: always `stdin=DEVNULL`.
- The npm `opencode.cmd` shim survives a timeout kill; `fanout.resolve_opencode()` launches
  `opencode.exe` itself (override with `GATE_OPENCODE`).
- Every run gets an isolated `HOME`/`USERPROFILE`/`XDG_*`; the user's global opencode config
  (bash/edit/webfetch: allow) must never load. Only `GATE_ROUTER_KEY`/`GATE_WORKER_KEY` pass through.
- Keys come from the environment or root-only files, never argv; the workers' key file is
  `root:bench 0640` because llama-server runs as the non-root `bench` user.
- ik llama-server prints its timing block unprefixed; `llama_log` takes the task id from the line before.
- `gate_env.sh restore` replays the state saved by `pin`; never hand-restart units in between.

## Tests
`python3 -m pytest scripts/tools/gate/tests -q` — the opencode tests drive the real binary against
`stub_llm.py` (no cluster needed); `test_gate_env.py` drives `gate_env.sh` against fake
`pct`/`systemctl`/`curl`.
````

- [ ] **Step 5: Update the AGENTS.md test command and run the full suite**

In `AGENTS.md`, replace
``- **Python tests:** `python3 -m pytest scripts/rag/tests scripts/files/tests -q` ``
with
``- **Python tests:** `python3 -m pytest scripts/rag/tests scripts/files/tests scripts/tools/gate/tests -q` ``

```bash
python -m pytest scripts/rag/tests scripts/files/tests scripts/tools/gate/tests -q -p no:cacheprovider | tail -2
for f in scripts/tools/gate/sh/*.sh; do bash -n "$f" && echo "ok $f"; done
```

Expected: `240 passed` (174 existing plus 66 gate), and `ok` for all 5 scripts.

- [ ] **Step 6: Confirm pure renames, then commit**

```bash
git add -A scripts/tools/gate docs/superpowers/gate AGENTS.md
git status --short | grep -v '^R ' | grep -v '^A  scripts/tools/gate/README.md' | grep -v '^M  AGENTS.md'
git diff --cached -M100% --stat | tail -5
```

Expected:
- the `grep` pipeline prints nothing, meaning only renames, the README and AGENTS.md are staged;
- every moved file is listed as a rename `{… => …}` with no `+`/`-` counts.

The only new content is `README.md` and the `AGENTS.md` line.

```bash
git commit -m "feat(gate): phase-1 measurement harness and frozen worker-task set"
```

---

### Task 2: Offline preconditions — task-set validity and the §5.2 mechanism probe

**Files:**
- Create: `docs/superpowers/gate/tasks-validation.json` (written by `gate.py tasks`)
- Create: `docs/superpowers/gate/probe-mechanism.json`

**Interfaces:**
- Consumes: `gate.py tasks` and `gate.py probe` (Task 1).
- Produces: manifest SHA `9415ec687a73e8858226ba4a7423c4098d4f2023fe05727a3b5fd3474cf21e3f`
  (expected value; Task 3 freezes whatever this prints), and a §5.2 verdict.

- [ ] **Step 1: Validate the task set**

```bash
python scripts/tools/gate/gate.py tasks
```

Expected:
- `"problems": []`;
- six task ids `T1-ttl-cache … T6-path-router`;
- `manifest_sha256` equal to the value above.

If it differs, the files changed in the move: stop and diff against the plan commit.

- [ ] **Step 2: Run the opencode concurrency and containment probe**

```bash
python scripts/tools/gate/gate.py probe > docs/superpowers/gate/probe-mechanism.json; echo "rc=$?"
cat docs/superpowers/gate/probe-mechanism.json
```

Expected:
- `rc=0`, `"verdict": "parallel"`, `"span_s"` ≈ 4 (well under `"serial_s": 12`);
- `"escaped": false`;
- `worker_tools` without `bash`/`webfetch`/`task`, and `coordinator_tools` without
  `bash`/`edit`/`write`.

**Stop condition (§5.2):** if `"verdict": "serial"`, Phase 1 halts. Record that as the gate
outcome in `docs/superpowers/gate/RESULTS.md`, close Phase 2, skip to Task 12 Step 5, and tell the
user.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/gate/tasks-validation.json docs/superpowers/gate/probe-mechanism.json
git commit -m "test(gate): task-set validation and opencode concurrency probe (parallel)"
```

---

### Task 3: Write the frozen gate config

**Files:**
- Create: `docs/superpowers/gate/gate-config.json`

**Interfaces:**
- Consumes: `gate.load_config()` and its required keys `thresholds, runs_per_arm, router_url,
  coordinator_alias, worker_urls, probe, polyglot, worker_flags, task_manifest_sha256,
  large_prefill_min`.
- Produces: the config every later `gate.py` subcommand loads. Thresholds are read by
  `gate_stats.decide` (`one_task_fraction`, `latency_factor`, `quality_task_margin`) and by the
  `variance` subcommand.

- [ ] **Step 1: Write the config**

Use the manifest printed in Task 2 Step 1 if it differs from the value below; it should not.

```json
{
 "spec": "docs/superpowers/specs/2026-10-05-minisforum-cluster-integration-design.md rev 3.1 §5",
 "thresholds": {
  "one_task_fraction": 0.029411764705882353,
  "latency_factor": 1.5,
  "quality_task_margin": 1,
  "variance_sd_tasks": 1,
  "variance_cv": 0.15
 },
 "runs_per_arm": 3,
 "run_order": ["A", "B", "B", "A", "A", "B"],
 "router_url": "http://192.168.6.153:8000/v1",
 "coordinator_alias": "qwen3.8-nothink",
 "worker_alias": "qwen3.6",
 "worker_urls": ["http://172.16.10.205:8090/v1", "http://172.16.10.206:8090/v1", "http://172.16.10.207:8090/v1"],
 "worker_vms": ["llmbench01", "llmbench02", "llmbench03"],
 "probe": {"interval_s": 20, "baseline_samples": 30, "max_failed": 0, "prompt": "Reply with the single word: ready"},
 "capacity_timeout_s": 14400,
 "large_prefill_min": 4096,
 "polyglot": {
  "languages": "python", "threads": 1, "edit_format": "whole",
  "aider_commit": "5dc9490", "benchmark_commit": "7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f",
  "exercises": ["affine-cipher", "beer-song", "book-store", "bottle-song", "bowling", "connect",
   "dominoes", "dot-dsl", "food-chain", "forth", "go-counting", "grade-school", "grep", "hangman",
   "list-ops", "paasio", "phone-number", "pig-latin", "poker", "pov", "proverb", "react",
   "rest-api", "robot-name", "scale-generator", "sgf-parsing", "simple-linked-list", "transpose",
   "tree-building", "two-bucket", "variable-length-quantity", "wordy", "zebra-puzzle", "zipper"]
 },
 "worker_flags": "-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr --spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs --min-p 0",
 "worker_extra": "-cram 256 -ctx-ckpt 8 --temp 0.7 --top-p 0.8 --top-k 20 --jinja -np 1 --alias qwen3.6 --chat-template-kwargs {\"enable_thinking\":false}",
 "opencode_version": "1.18.34",
 "contexts": {"arm_a_coordinator_and_workers": 120000, "arm_b_coordinator": 200000, "arm_b_workers": 57344},
 "task_manifest_sha256": "9415ec687a73e8858226ba4a7423c4098d4f2023fe05727a3b5fd3474cf21e3f"
}
```

`run_order` interleaves the arms because `decide()` pairs run *i* of A with run *i* of B. ABBA
ordering spreads any drift evenly. `runs_per_arm` 3 is the floor; §5.1 may raise it (Task 8).

- [ ] **Step 2: Verify it loads, and that the worker flags match the script byte for byte**

```bash
python - <<'EOF'
import json, re, sys
sys.path.insert(0, "scripts/tools/gate")
import gate
cfg = gate.load_config()
sh = open("scripts/tools/gate/sh/worker_serve.sh").read()
assert re.search(r'^WORKER_FLAGS="([^"]*)"$', sh, re.M).group(1) == cfg["worker_flags"]
assert len(cfg["polyglot"]["exercises"]) == 34 and abs(cfg["thresholds"]["one_task_fraction"] - 1/34) < 1e-12
print("config OK", cfg["task_manifest_sha256"][:12])
EOF
```

Expected: `config OK 9415ec687a73`.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/gate/gate-config.json
git commit -m "chore(gate): frozen gate config (thresholds, endpoints, exercise list, task manifest)"
```

---

### Task 4: Operator prerequisites on the workstation (no repo changes)

**Files:** none in the repo.
- `%USERPROFILE%\.vcflab\credentials.env` (append one key)
- `%USERPROFILE%\.vcflab\gate-worker.key` (new, owner-only)
- a vcf-lab worktree

**Interfaces:**
- Produces, for this PowerShell session and every later task:
  - `$env:VCFLAB_ROOT`;
  - `$env:GATE_ROUTER_KEY` and `$env:GATE_WORKER_KEY` (set, never printed);
  - `LLM_BENCH_GUEST_PASS` in the credentials file.

**Shells.** If the executor's tool calls do not share a PowerShell session, put Step 4's lines in a
session script outside the repo and dot-source it at the top of every call. Blocks marked `powershell` run
in PowerShell from the repo root. Blocks marked `bash`
run in Git Bash. Re-run Step 4 in any new PowerShell session: it sets `VCFLAB_ROOT`, both keys,
`$R` (the results root) and `$G` (the guest helper).

- [ ] **Step 1: Check out the vcf-lab scripts durably**

They live only on the unmerged branch `feat/9-1-1-right-sized-capacity`.

```powershell
git -C C:\Users\willi\Documents\GitHub\local-gpu-cluster worktree add ..\lgc-vcflab feat/9-1-1-right-sized-capacity
$env:VCFLAB_ROOT = (Resolve-Path C:\Users\willi\Documents\GitHub\lgc-vcflab\scripts\vcf-lab).Path
Test-Path (Join-Path $env:VCFLAB_ROOT 'VCFLab.Common.ps1')
```

Expected: `True`.

- [ ] **Step 2: Move the guest password into the credentials file**

The bench guest password currently sits in a session scratch file. Move it without printing it:

```powershell
$src = 'C:\Users\willi\AppData\Local\Temp\claude\c--Users-willi-Documents-GitHub-local-gpu-cluster\4bc0295a-452b-4bdf-ba9b-786ecd9a37c2\scratchpad\bench\guest.pass'
$cf = Join-Path $env:USERPROFILE '.vcflab\credentials.env'
if (-not (Select-String -Path $cf -Pattern '^LLM_BENCH_GUEST_PASS=' -Quiet)) {
    $p = (Get-Content -Raw $src).Trim()
    Add-Content -Path $cf -Value ("LLM_BENCH_GUEST_PASS=" + $p)
    Remove-Variable p
}
"guest pass line present: " + [bool](Select-String -Path $cf -Pattern '^LLM_BENCH_GUEST_PASS=.+' -Quiet)
```

Expected: `guest pass line present: True`. If the scratch file is gone, ask the user for the
password and have them append the line themselves.

- [ ] **Step 3: Generate the throwaway worker key (owner-only)**

```powershell
$kf = Join-Path $env:USERPROFILE '.vcflab\gate-worker.key'
$k = (python -c "import secrets; print(secrets.token_hex(32))").Trim()
[IO.File]::WriteAllText($kf, $k + "`n"); Remove-Variable k
icacls $kf /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
"key bytes: " + (Get-Item $kf).Length
```

Expected: `key bytes: 65`.

- [ ] **Step 4: Load both keys into the session**

```powershell
$env:VCFLAB_ROOT = (Resolve-Path C:\Users\willi\Documents\GitHub\lgc-vcflab\scripts\vcf-lab).Path
$env:GATE_WORKER_KEY = (Get-Content -Raw (Join-Path $env:USERPROFILE '.vcflab\gate-worker.key')).Trim()
$env:GATE_ROUTER_KEY = (ssh root@192.168.6.175 "pct exec 153 -- sed -n 's/^ROUTER_API_KEY=//p' /etc/router.env").Trim()
$R = Join-Path $env:LOCALAPPDATA 'gate-runs'; New-Item -ItemType Directory -Force $R | Out-Null
$G = 'scripts\tools\gate\gate_guest.ps1'
"router key length: $($env:GATE_ROUTER_KEY.Length); worker key length: $($env:GATE_WORKER_KEY.Length); results: $R"
```

Expected: `router key length: 64; worker key length: 64`.

- [ ] **Step 5: Smoke-test the guest channel on one worker**

```powershell
powershell -NoProfile -File scripts\tools\gate\gate_guest.ps1 -VmName llmbench01 -Exec 'uname -n; nproc; free -g | head -2'
```

Expected: `llmbench01`, `16`, and a memory line of ~23 GB total. If vCenter is off, the script
connects host-direct.

---

### Task 5: Worker serving, firewall and reachability (§5.0.3, §5.0.4, §5.0.6 workers, §5.5.1-3)

**Files:** none in the repo. Output: `docs/superpowers/gate/workers-status.txt` (Step 8).

**Interfaces:**
- Consumes:
  - `gate_guest.ps1 -VmName <vm> -Exec/-Put/-Fetch`;
  - `worker_serve.sh` subcommands;
  - the `$env:GATE_*` values from Task 4.
- Produces: three keyed, firewalled, non-thinking workers on `:8090` with alias `qwen3.6`, and the
  verified client source IPs.

- [ ] **Step 1: Confirm Minimal mode with the workers powered on, and start LXC 158**

```powershell
foreach ($vm in 'llmbench01','llmbench02','llmbench03') { powershell -NoProfile -File scripts\tools\gate\gate_guest.ps1 -VmName $vm -Exec 'uptime' }
ssh root@192.168.6.175 "pct start 158; sleep 5; pct status 158"
```

Expected:
- three `uptime` lines;
- `status: running`.

If a worker is off, start the lab with `& "$env:VCFLAB_ROOT\Start-VCFLab.ps1" -Mode Minimal` (it
owns the workers) and repeat.

- [ ] **Step 2: Install the control script, the key and the quiet window on each worker**

```powershell
$key = Join-Path $env:USERPROFILE '.vcflab\gate-worker.key'
foreach ($vm in 'llmbench01','llmbench02','llmbench03') {
  powershell -NoProfile -File $G -VmName $vm -Put scripts\tools\gate\sh\worker_serve.sh -Remote /tmp/worker_serve.sh
  powershell -NoProfile -File $G -VmName $vm -Exec 'sudo install -m 0755 /tmp/worker_serve.sh /usr/local/bin/gate-worker && rm -f /tmp/worker_serve.sh'
  powershell -NoProfile -File $G -VmName $vm -Put $key -Remote /tmp/gate.key.upload
  powershell -NoProfile -File $G -VmName $vm -Exec 'gate-worker install-key /tmp/gate.key.upload && gate-worker quiet'
}
```

Expected, per VM:
- two `uploaded … bytes` lines;
- `key installed at /etc/gate/worker.key (root:bench 0640)`;
- `apt timers stopped, needrestart list-only`.

- [ ] **Step 3: Start llama-server with the frozen flags, plus connection logging**

```powershell
foreach ($vm in 'llmbench01','llmbench02','llmbench03') {
  powershell -NoProfile -File $G -VmName $vm -TimeoutSec 700 -Exec 'gate-worker start && gate-worker learn'
}
```

Expected, per VM:
- a log path `/var/log/gate/server-<UTC>.log`;
- `logging new :8090 connections`.

If `start` dies with `invalid argument: --api-key-file`, ik does not support the flag. **Stop.**
The fallback (`--api-key` from an env file) changes the security posture, so it is the user's call.

- [ ] **Step 4: Authenticated, non-thinking completion from the workstation (§5.0.4)**

```powershell
$h = @{ Authorization = "Bearer $env:GATE_WORKER_KEY" }
$body = @{ model = 'qwen3.6'; max_tokens = 64; messages = @(@{ role = 'user'; content = 'Reply with the single word: ready' }) } | ConvertTo-Json -Depth 5
foreach ($ip in '172.16.10.205','172.16.10.206','172.16.10.207') {
  $m = (Invoke-RestMethod -Uri "http://${ip}:8090/v1/chat/completions" -Method Post -Headers $h -Body $body -ContentType 'application/json' -TimeoutSec 600).choices[0].message
  $rc = ($m.PSObject.Properties.Name -contains 'reasoning_content') -and [bool]$m.reasoning_content
  "{0}: content='{1}' reasoning={2} think_tag={3}" -f $ip, $m.content.Trim(), $rc, ($m.content -match '<think>')
  # /health and /v1/models are key-exempt by design in llama-server; test an inference call.
  try { Invoke-WebRequest -Uri "http://${ip}:8090/v1/chat/completions" -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 60 | Out-Null; "${ip}: UNAUTHENTICATED ACCESS ALLOWED" }
  catch { "${ip}: unauthenticated -> " + [int]$_.Exception.Response.StatusCode }
}
```

Expected, per IP:
- `content='ready'` (any casing or punctuation);
- `reasoning=False think_tag=False`;
- `unauthenticated -> 401`.

**Stop if:**
- **The connection fails:** the workstation cannot route to VLAN 10. Tell the user which IPs and
  ports fail; do not change network configuration.
- **There is any reasoning, or `UNAUTHENTICATED ACCESS ALLOWED`:** the worker is not as specified;
  stop it (`gate-worker stop`) and report.

- [ ] **Step 5: The same check from LXC 158, with the key in files only**

```powershell
scp (Join-Path $env:USERPROFILE '.vcflab\gate-worker.key') root@192.168.6.175:/root/gate-worker.key.tmp
ssh root@192.168.6.175 "mkdir -p /root/gate && chmod 700 /root/gate && mv /root/gate-worker.key.tmp /root/gate/worker.key && chmod 600 /root/gate/worker.key && { printf 'Authorization: Bearer '; cat /root/gate/worker.key; } > /root/gate/worker.hdr && chmod 600 /root/gate/worker.hdr"
'{"model":"qwen3.6","max_tokens":64,"messages":[{"role":"user","content":"Reply with the single word: ready"}]}' | Set-Content -NoNewline -Encoding ascii "$R\probe.json"
scp "$R\probe.json" root@192.168.6.175:/root/gate/probe.json
ssh root@192.168.6.175 "pct exec 158 -- mkdir -p /root/gate; pct push 158 /root/gate/worker.key /root/.gate-worker.key --perms 0600; pct push 158 /root/gate/worker.hdr /root/.gate-worker.hdr --perms 0600; pct push 158 /root/gate/probe.json /root/gate/probe.json"
foreach ($ip in '172.16.10.205','172.16.10.206','172.16.10.207') {
  ssh root@192.168.6.175 "pct exec 158 -- curl -s -m 600 -H @/root/.gate-worker.hdr -H 'Content-Type: application/json' -d @/root/gate/probe.json http://${ip}:8090/v1/chat/completions"
}
```

Expected: three JSON completions whose `message.content` is `ready`, with no `reasoning_content`.

LXC 158 has no route to VLAN 10 (the workstation and host carry `172.16.0.0/16 via 192.168.6.11`; 158
only has its default gateway). Before the curls, add the same route, non-persistent (it disappears when 158
stops): `ssh root@192.168.6.175 "pct exec 158 -- ip route replace 172.16.10.0/24 via 192.168.6.11"`.

**If LXC 158 still cannot connect** (curl prints nothing, or rc 7 or 28), stop and ask the user to allow
`192.168.6.158 → 172.16.10.205-207 tcp/8090` on their network. **Do not touch the CRS309.** The
quality sub-gate needs this path.

- [ ] **Step 6: Learn the real client source IPs, lock the firewall, then prove an outsider is blocked**

```powershell
foreach ($vm in 'llmbench01','llmbench02','llmbench03') { "$vm sees:"; powershell -NoProfile -File $G -VmName $vm -Exec 'gate-worker sources' }
```

Expected: the same source IPs on every worker: the workstation's address as VLAN 10 sees it,
LXC 158's, and `127.0.0.1` (the worker's own health checks from `gate-worker start`/`status`). Keep
`127.0.0.1` in the allowlist, or the cold restarts in Task 10 fail their own health wait. Any other address
means something else connected: stop and report it.

```powershell
$ips = '<workstation-src> <lxc158-src> 127.0.0.1'      # paste the addresses printed above
foreach ($vm in 'llmbench01','llmbench02','llmbench03') { powershell -NoProfile -File $G -VmName $vm -Exec "gate-worker lock $ips" }
ssh root@192.168.6.175 "curl -s -m 8 -o /dev/null -w '%{http_code}\n' http://172.16.10.205:8090/health; echo rc=`$?"
```

Expected:
- `:8090 accepted only from: …` on each worker;
- from the Proxmox host (`192.168.6.175`, not in the list): `000` and `rc=28` (timeout).

Then re-run Steps 4 and 5; both must still succeed.

**If the host is not blocked:** its traffic reaches the workers under the same source address as an
allowed client (NAT), so the allowlist does not protect the workers. Stop and report to the user.

- [ ] **Step 7: Note the timing for the record**

Prefill on CPU is ~230 t/s. A 15K-token worker turn therefore prefills in ~65 s, which is expected
and not a hang.

- [ ] **Step 8: Capture worker status**

```powershell
& { foreach ($vm in 'llmbench01','llmbench02','llmbench03') { "== $vm"; powershell -NoProfile -File $G -VmName $vm -Exec 'gate-worker status; /opt/bench/src/ik/build/bin/llama-server --version 2>&1 | head -3; sha256sum /models/Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf' } } | Set-Content -Encoding utf8 docs\superpowers\gate\workers-status.txt
Select-String -Path docs\superpowers\gate\workers-status.txt -Pattern 'df27a780|health|accept'
```

Expected:
- three model hash lines starting `df27a780`;
- three `health: {"status":"ok"}`;
- three nft `ip saddr { … } accept` rules.

The status output prints the key file's owner and mode, never its content.

---

### Task 6: Pin the host and prepare LXC 158 (§5.0.5, §5.0.6)

**Files:** host `/root/gate/gate_env.sh`; LXC 158 `/root/gate/*.sh`. Output:
`docs/superpowers/gate/environment.jsonl`.

**Interfaces:**
- Consumes: `gate_env.sh preflight|pin|record`, and `quality_batch.sh` / `quality_pack.sh` /
  `polyglot_run.sh`.
- Produces:
  - a pinned host (1 slot, perturbers stopped, embed/rerank stopped, `RATE_LIMIT_CHAT=1000/minute`);
  - `/root/gate/state.env` holding the pre-pin state that Task 12 restores.

- [ ] **Step 1: Confirm the window with the user**

RAG (embed and rerank) and normal chat use are unavailable until Task 12. The window must avoid
03:15 CDT for each run. Proceed only after the user agrees to the window.

- [ ] **Step 2: Copy the scripts and run preflight**

```powershell
scp scripts/tools/gate/sh/gate_env.sh root@192.168.6.175:/root/gate/gate_env.sh
ssh root@192.168.6.175 "chmod 700 /root/gate/gate_env.sh && bash -n /root/gate/gate_env.sh && /root/gate/gate_env.sh preflight"
```

Expected:
- the `rag-refresh.timer` next-run line;
- `preflight OK`.

**Stop if:**
- **`redteam mode is active`:** an engagement may be running. Ask the user.
- **`state.env exists`:** a previous window was never restored. Run `restore` only after asking.

- [ ] **Step 3: Pin, and record the pre-run environment**

```powershell
ssh root@192.168.6.175 "/root/gate/gate_env.sh pin && /root/gate/gate_env.sh record /root/gate/environment.jsonl && cat /root/gate/state.env"
```

Expected:
- `saved prior state`, then `chat_admission.capacity=1`, then `pinned: …`;
- `state.env` showing `PRIOR_RATE_LIMIT_CHAT='60/minute'` and each unit's prior state.

`state.env` contains no secrets.

- [ ] **Step 4: Install the quality scripts on LXC 158**

```powershell
foreach ($f in 'polyglot_run.sh','quality_batch.sh','quality_pack.sh') { scp "scripts/tools/gate/sh/$f" "root@192.168.6.175:/root/gate/$f" }
ssh root@192.168.6.175 "for f in polyglot_run.sh quality_batch.sh quality_pack.sh; do pct push 158 /root/gate/`$f /root/gate/`$f --perms 0755; done; pct exec 158 -- bash -n /root/gate/quality_batch.sh && pct exec 158 -- ls -l /root/gate /root/.router.key /root/.gate-worker.key"
```

Expected:
- the three scripts with mode `-rwxr-xr-x`;
- both key files with `-rw-------`.

- [ ] **Step 5: Copy the environment record into the repo**

```powershell
scp root@192.168.6.175:/root/gate/environment.jsonl docs/superpowers/gate/environment.jsonl
python -c "import json; r=[json.loads(l) for l in open('docs/superpowers/gate/environment.jsonl')][-1]; print(r['rate_limit_chat'], r['healthz']['chat_admission'], r['llama_server_version'].splitlines()[0], r['units'], r['amd_units'])"
```

Expected:
- `RATE_LIMIT_CHAT=1000/minute`;
- `{'capacity': 1, …}`;
- `version: … (build 11026 …)`;
- every listed unit `inactive` or `failed`, except that the chat unit is not listed.

---

### Task 7: The gate commit (§5.0.7)

**Files:**
- Add: `docs/superpowers/gate/environment.jsonl`, `docs/superpowers/gate/workers-status.txt`

**Interfaces:**
- Produces: `GATE_SHA`, the commit every result references. After it, only the §5.1 amendment may
  change `gate-config.json`.

- [ ] **Step 1: Verify nothing in the frozen inputs moved, then commit**

```bash
python scripts/tools/gate/gate.py tasks | grep -E '"problems"|manifest'
git add docs/superpowers/gate/environment.jsonl docs/superpowers/gate/workers-status.txt
git commit -m "chore(gate): gate commit - environment record and worker status (pre-registration)"
mkdir -p "$LOCALAPPDATA/gate-runs" && git rev-parse HEAD | tee "$LOCALAPPDATA/gate-runs/GATE_SHA"
```

Expected: `"problems": []`, the manifest unchanged, and a SHA printed. Every later commit message
quotes it as `gate: <sha>`:
- Git Bash: `$(cat "$LOCALAPPDATA/gate-runs/GATE_SHA")`;
- PowerShell: `$(Get-Content "$R\GATE_SHA")`.

---

### Task 8: §5.1 variance check, plus the coordinator and worker quality runs

**Files:**
- Output (outside the repo): `%LOCALAPPDATA%\gate-runs\quality\{coordinator,worker}\NN.json`
- Possibly modify: `docs/superpowers/gate/gate-config.json` (the amendment)

**Interfaces:**
- Consumes: `quality_batch.sh <label> <alias> <base> <keyfile> <runs> [first]`,
  `quality_pack.sh <out.tgz>`, `gate.py polyglot`, and `gate.py variance`.
- Produces:
  - 5 coordinator pass@2 files and ≥ 3 worker pass@2 files;
  - the final `runs_per_arm`.

The coordinator's five §5.1 runs are also its quality-sub-gate runs, on the same ruler and alias.
The workers are independent machines with throughput within 1% of each other, so one run per
worker, running concurrently, gives three worker replicates. That is pre-registered here.

- [ ] **Step 1: Start the four batches detached on LXC 158**

```powershell
$launch = 'setsid nohup /root/gate/quality_batch.sh {0} {1} {2} {3} {4} > /dev/null 2>&1 < /dev/null &'
ssh root@192.168.6.175 ("pct exec 158 -- bash -c '" + ($launch -f 'coord','qwen3.8-nothink','http://192.168.6.153:8000/v1','/root/.router.key',5) + "'")
$n = 1
foreach ($ip in '172.16.10.205','172.16.10.206','172.16.10.207') {
  ssh root@192.168.6.175 ("pct exec 158 -- bash -c '" + ($launch -f "w$n",'qwen3.6',"http://${ip}:8090/v1",'/root/.gate-worker.key',1) + "'")
  $n++
}
ssh root@192.168.6.175 "sleep 60; pct exec 158 -- tail -n 2 /root/gate/logs/batch-coord.log /root/gate/logs/batch-w1.log /root/gate/logs/batch-w2.log /root/gate/logs/batch-w3.log"
```

Expected: `start gate-coord-01`, `start gate-w1-01`, `start gate-w2-01` and `start gate-w3-01`.

Monitor with
`ssh root@192.168.6.175 "pct exec 158 -- tail -n 3 /root/gate/logs/batch-*.log"` about once an
hour. Each batch ends with `batch <label> complete`. Expect ~4 h for the coordinator batch and
~3-5 h for each worker.

- [ ] **Step 2: When all four batches are complete, pack and fetch the results**

```powershell
$R = Join-Path $env:LOCALAPPDATA 'gate-runs'; New-Item -ItemType Directory -Force "$R\raw" | Out-Null
ssh root@192.168.6.175 "pct exec 158 -- /root/gate/quality_pack.sh /root/gate/quality.tgz && pct pull 158 /root/gate/quality.tgz /root/gate/quality.tgz"
scp root@192.168.6.175:/root/gate/quality.tgz "$R\raw\quality.tgz"
tar -xzf "$R\raw\quality.tgz" -C "$R\raw"
Get-ChildItem "$R\raw" -Directory | Select-Object -ExpandProperty Name
```

Expected: 8 directories, `<timestamp>--gate-coord-01..05` and `<timestamp>--gate-w1-01`, `w2-01`,
`w3-01`. A FAILED line in a batch log means a run died. Its directory may be partial; the parser
counts missing exercises as fails. Report it, and rerun only if the cause was infrastructure, not
the model.

- [ ] **Step 3: Score every run**

```powershell
foreach ($d in Get-ChildItem "$R\raw" -Directory) {
  $kind = if ($d.Name -match 'gate-coord-') { 'coordinator' } else { 'worker' }
  $tag = ($d.Name -split '--gate-')[1]
  python scripts/tools/gate/gate.py polyglot --run-dir $d.FullName --out "$R\quality\$kind\$tag.json" | Select-String '"pass2"|"missing"'
}
```

Expected: one `pass2` per run. Coordinator values should sit near the 2026-09 baseline
(`qwen3.8-nothink` py34). Each `missing` list should be `[]`.

- [ ] **Step 4: Apply the §5.1 sizing rule**

```powershell
python scripts/tools/gate/gate.py variance --runs (Get-ChildItem "$R\quality\coordinator\*.json").FullName
```

Expected: JSON with `pass2_sd_tasks`, `wall_cv`, `ci_half_width` and `ok`.

- **If `"ok": true`:** keep `runs_per_arm: 3`. Go to Step 6.
- **If `"ok": false`:** do Step 5 before any capacity run.

- [ ] **Step 5 (only if not ok): the pre-registration amendment**

Raise `runs_per_arm` to 5, and extend `run_order` to `["A","B","B","A","A","B","B","A","A","B"]`.
If `ci_half_width` is still larger than `one_task_fraction` with 5 runs, the spec's other lever is
the full Polyglot set: tell the user, because it multiplies run time.

Then run 2 more worker runs:
`quality_batch.sh w1 qwen3.6 http://172.16.10.205:8090/v1 /root/.gate-worker.key 1 2` (index 2),
and the same for w2 (index 2), with the matching IPs. That brings the workers to 5 replicates; repeat
Steps 2-3 for those runs.

```bash
git add docs/superpowers/gate/gate-config.json
git commit -m "chore(gate): pre-registration amendment - runs_per_arm 5 after variance check (gate: $(cat "$LOCALAPPDATA/gate-runs/GATE_SHA"))"
```

- [ ] **Step 6: Record the quality numbers now, before any capacity data**

```powershell
New-Item -ItemType Directory -Force docs\superpowers\gate\results | Out-Null
Copy-Item -Recurse "$R\quality" docs\superpowers\gate\results\ -Force
git add docs/superpowers/gate/results/quality
git commit -m "test(gate): polyglot quality runs, coordinator and CPU worker (gate: $(Get-Content "$R\GATE_SHA"))"
```

---

### Task 9: §5.3 idle baselines, one per layout

**Files:** output `%LOCALAPPDATA%\gate-runs\{A,B}\baseline.json`.

**Interfaces:**
- Consumes: `gate_env.sh arm-a|arm-b`, and `gate.py baseline --arm X --out F` (30 probes, 20 s
  apart, ~10 min).
- Produces: the `baseline.json` files that `gate.collect` reads for each arm.

- [ ] **Step 1: Arm B layout baseline (1 slot)**

Nothing else may use the chat during this step: no quality batch running, no other client.

```powershell
ssh root@192.168.6.175 "/root/gate/gate_env.sh arm-b"
python scripts/tools/gate/gate.py baseline --arm B --out "$R\B\baseline.json"; "rc=$LASTEXITCODE"
```

Expected:
- `chat_admission.capacity=1`;
- `rc=0`, with `"failed": 0` and a `p50` of a few seconds or less.

- [ ] **Step 2: Arm A layout baseline (3 × 128K)**

```powershell
ssh root@192.168.6.175 "/root/gate/gate_env.sh arm-a"
python scripts/tools/gate/gate.py baseline --arm A --out "$R\A\baseline.json"; "rc=$LASTEXITCODE"
```

Expected:
- `chat_admission.capacity=3` (the chat restart takes a few minutes);
- `rc=0` with `"failed": 0`.

---

### Task 10: §5.6 capacity runs, interleaved in `run_order`

**Files:** output `%LOCALAPPDATA%\gate-runs\{A,B}\run-NN\{result.json,events.jsonl,chat.log,chat-scrape.json,w1..w3.log,w*-scrape.json}`.

**Interfaces:**
- Consumes:
  - `gate.py capacity --arm X --run k --out-root R` (prints `VALID` or `INVALID -- rerun: …`, rc 0/1);
  - `gate.py scrape --log F --out F`;
  - `gate_env.sh arm-a|arm-b|record`;
  - `gate-worker stop|start`.
- Produces: `runs_per_arm` valid runs per arm, each with its log scrape.

For each entry of `run_order` (A, B, B, A, A, B for 3 runs), with `k` counting that arm's runs
from 1, do Steps 1-4.

- [ ] **Step 1: Set the layout. For arm B, also restart the workers cold**

Each B run starts from cold caches and fresh logs.

```powershell
$arm = 'B'; $k = 1      # set per run_order entry
ssh root@192.168.6.175 ("/root/gate/gate_env.sh arm-" + $arm.ToLower() + " && /root/gate/gate_env.sh record /root/gate/environment.jsonl")
if ($arm -eq 'B') {
  $wlog = @{}
  foreach ($vm in 'llmbench01','llmbench02','llmbench03') {
    $out = powershell -NoProfile -File $G -VmName $vm -TimeoutSec 700 -Exec 'gate-worker stop >/dev/null; gate-worker start'
    $wlog[$vm] = (($out -join "`n") -split "`n" | Where-Object { $_ -match '^/var/log/gate/server-' } | Select-Object -Last 1).Trim()
    if (-not $wlog[$vm]) { throw "$vm did not report a server log path: $out" }
  }
  $wlog
}
```

Expected:
- the capacity line (`=3` for A, `=1` for B);
- `recorded … capacity`;
- for B, three log paths.

Do not start a run that would overlap 03:15 CDT. Arm B runs can take ~2 h.

- [ ] **Step 2: Run it**

```powershell
python scripts/tools/gate/gate.py capacity --arm $arm --run $k --out-root $R; "rc=$LASTEXITCODE"
```

Expected:
- a summary showing `completed` 6, the `max_overlap` reached (≥ 2 means the coordinator really fanned
  out), `probe.failed` 0;
- `VALID`, `rc=0`.

On `INVALID -- rerun: <reason>`, rename the directory to `run-NN.invalid-<reason-slug>`, which
`collect` ignores because it only globs `run-*/result.json`. Fix the cause, then rerun the same `k`.
Never delete an invalid run.

- [ ] **Step 3: Scrape the llama-server logs for this run's window**

```powershell
$res = Get-Content -Raw "$R\$arm\run-$('{0:D2}' -f $k)\result.json" | ConvertFrom-Json
$t0 = [int][math]::Floor($res.t_start); $t1 = [int][math]::Ceiling($res.t_end)
$rd = "$R\$arm\run-$('{0:D2}' -f $k)"
ssh root@192.168.6.175 "pct exec 151 -- journalctl -u llamacpp-chat --since @$t0 --until @$t1 -o cat --no-pager" | Set-Content -Encoding utf8 "$rd\chat.log"
python scripts/tools/gate/gate.py scrape --log "$rd\chat.log" --out "$rd\chat-scrape.json"
if ($arm -eq 'B') {
  $i = 1
  foreach ($vm in 'llmbench01','llmbench02','llmbench03') {
    powershell -NoProfile -File $G -VmName $vm -Fetch $wlog[$vm] -OutFile "$rd\w$i.log"
    python scripts/tools/gate/gate.py scrape --log "$rd\w$i.log" --out "$rd\w$i-scrape.json"; $i++
  }
}
ssh root@192.168.6.175 "/root/gate/gate_env.sh record /root/gate/environment.jsonl"
```

Expected:
- each scrape shows `requests` > 0;
- `large_prefills` is about 1 + the sessions on that server (the coordinator 1; each worker 2, one
  per task);
- noticeably more means re-prefill, which is reported, not judged.

- [ ] **Step 4: Repeat Steps 1-3 for the next `run_order` entry until every arm has `runs_per_arm` valid runs**

If the §5.1 amendment raised the worker quality runs, the remaining worker Polyglot runs may run
**during arm A runs**. The workers are idle in arm A and the runs never touch the GPU. They must not
run during arm B runs or during baselines.

---

### Task 11: §5.7 decision

**Files:** output `%LOCALAPPDATA%\gate-runs\decision.json`.

**Interfaces:**
- Consumes: `gate.py decide --results R`. It reads `quality/*/*.json`, `{A,B}/baseline.json` and
  `{A,B}/run-*/result.json`, refuses invalid or unpaired runs, and applies `gate_stats.decide`.
- Produces: `{"build": bool, "reason": str, "details": {…}}`.

- [ ] **Step 1: Copy the quality files into the results root, then decide**

```powershell
Copy-Item -Recurse docs\superpowers\gate\results\quality "$R\quality" -Force
python scripts/tools/gate/gate.py decide --results $R
```

Expected: a `decision.json` whose `reason` is one of:
- `quality veto`;
- `no additive capacity (CI includes 0)`;
- `coordinator latency over limit in arm B`;
- `arm B task quality worse than A - 1`;
- `all §5.7 conditions hold`.

Do not reinterpret it; the rule was committed before the data.

---

### Task 12: §5.8 teardown, results commit and follow-through

**Files:**
- Create: `docs/superpowers/gate/results/{A,B}/…` (JSON only), `docs/superpowers/gate/results/decision.json`,
  `docs/superpowers/gate/RESULTS.md`
- Modify: `docs/superpowers/gate/environment.jsonl` (the final records)

**Interfaces:**
- Consumes: `gate_env.sh restore`, `gate-worker teardown`, and Task 11's decision.

- [ ] **Step 1: Restore the host exactly**

```bash
ssh root@192.168.6.175 "/root/gate/gate_env.sh restore && /root/gate/gate_env.sh record /root/gate/environment.jsonl && cat /root/gate/state.env.restored.*"
scp root@192.168.6.175:/root/gate/environment.jsonl docs/superpowers/gate/environment.jsonl
python - <<'EOF'
import json
last = [json.loads(l) for l in open("docs/superpowers/gate/environment.jsonl")][-1]
print("rate:", last["rate_limit_chat"], "capacity:", last["healthz"]["chat_admission"]["capacity"])
print("amd units:", last["amd_units"])
print("host units:", last["units"])
EOF
```

Expected:
- `restored to the state saved at …`;
- `rate: RATE_LIMIT_CHAT=60/minute capacity: 1`;
- `llamacpp-chat-restart.timer`, `llamacpp-embed.service` and `llamacpp-rerank.service` all
  `active`;
- `redteam-mode-watch.service` and `redteam-mode-idle.timer` matching their pre-pin values in
  `state.env.restored.*`.

Then make one embedding call, to prove RAG is back:

```powershell
$b = @{ model = 'embed'; input = 'gate teardown check' } | ConvertTo-Json
(Invoke-RestMethod -Uri http://192.168.6.153:8000/v1/embeddings -Method Post -Headers @{ Authorization = "Bearer $env:GATE_ROUTER_KEY" } -Body $b -ContentType 'application/json').data[0].embedding.Count
```

Expected: a positive vector length. If the router's embedding alias is not `embed`, use the alias
listed by `GET /v1/models`.

- [ ] **Step 2: Tear the workers down and remove every copy of the key**

```powershell
foreach ($vm in 'llmbench01','llmbench02','llmbench03') {
  powershell -NoProfile -File $G -VmName $vm -Exec 'gate-worker teardown && sudo rm -f /usr/local/bin/gate-worker && test ! -e /etc/gate && echo clean'
}
ssh root@192.168.6.175 "pct exec 158 -- rm -f /root/.gate-worker.key /root/.gate-worker.hdr /root/gate/probe.json; shred -u /root/gate/worker.key /root/gate/worker.hdr; rm -f /root/gate/probe.json /root/gate/quality.tgz; pct stop 158; pct status 158"
Remove-Item (Join-Path $env:USERPROFILE '.vcflab\gate-worker.key'); Remove-Item Env:GATE_WORKER_KEY
```

Expected:
- `teardown complete …` and `clean` per VM;
- `status: stopped` (158 was stopped before the gate).

- [ ] **Step 3: Copy results (JSON and scrapes only) into the repo and write RESULTS.md**

```powershell
foreach ($arm in 'A','B') {
  Copy-Item "$R\$arm\baseline.json" "docs\superpowers\gate\results\$arm\baseline.json" -Force
  foreach ($d in Get-ChildItem "$R\$arm" -Directory) {
    $dst = "docs\superpowers\gate\results\$arm\$($d.Name)"; New-Item -ItemType Directory -Force $dst | Out-Null
    Get-ChildItem $d.FullName -File -Include result.json, events.jsonl, *-scrape.json | Copy-Item -Destination $dst
  }
}
Copy-Item "$R\decision.json" docs\superpowers\gate\results\decision.json
```

`docs/superpowers/gate/RESULTS.md` must state:
- the gate SHA, and the amendment SHA if any;
- the decision and its reason, quoting `details` (the quality CI, the B−A tasks/hour CI, each B
  run's probe p50 against its baseline, and per-pair passed counts);
- a per-run table with arm, wall-clock, tasks/hour, passed/6, `max_overlap`, probe p50,
  `large_prefills`, coordinator `cache_n_total` and worker gen tokens;
- the §5.4.7 asymmetry: in arm A, 3 slots serve 4 consumers (the coordinator plus 3 workers), and
  the router queues the fourth;
- the §5.7 wall-clock note: a ≥ 30% B win is supporting evidence only;
- any invalid runs, with their reasons.

Invalid runs stay in `%LOCALAPPDATA%`; their reasons are listed, not their files.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/gate/results docs/superpowers/gate/RESULTS.md docs/superpowers/gate/environment.jsonl
git commit -m "test(gate): phase-1 gate results and decision (gate: $(cat "$LOCALAPPDATA/gate-runs/GATE_SHA"))"
```

- [ ] **Step 5: Follow-through per §5.7, reported to the user rather than executed**

These change lab config or open new work.
- **`build: true`:** Phase 2 is next. Its design is written from the §6 requirements, reviewed,
  then planned.
- **`build: false`:**
  - close Phase 2;
  - recommend powering the workers off, so Minimal means hosts only (D9). That edits
    `VCFLab.Config.psd1` on the unmerged `feat/9-1-1-right-sized-capacity`, which is the user's
    call;
  - if arm A's capacity and latency numbers justify it, propose §5.9 (a selectable 3-slot mode) as
    its own spec.

---

## Self-review (done while writing)

- **Spec coverage:**

  | Spec item | Where the plan covers it |
  |---|---|
  | 5.0.1 | Tasks 1-3 |
  | 5.0.2 | `profiles.py` (Task 1). The spec's `~/.config/opencode/config.bench.json` becomes a per-run generated config under an isolated `HOME`: never the default, and stronger than a named file. That is a **ruling**. |
  | 5.0.3 | Task 5 Steps 2-6, plus the deny-by-default agents proven in Task 2 |
  | 5.0.4 | Task 5 Steps 4-5 |
  | 5.0.5 | Tasks 5 Step 8, 6 Step 5 and 7 |
  | 5.0.6 | Task 6 Step 3 and Task 5 Step 2 |
  | 5.0.7 | Task 7 |
  | 5.1 | Task 8 |
  | 5.2 | Task 2. Workers have no bash under 5.0.3, so the "sleep" runs server-side in a scripted model; live fan-out is recorded per run as `max_overlap`. |
  | 5.3 | Task 9 |
  | 5.4 | `gate_env.sh arm-a` |
  | 5.5 | Task 5 and `arm-b` |
  | 5.6 | Tasks 8 and 10 |
  | 5.7 | Task 11 |
  | 5.8 | Task 12 |

  **Ruling on the key file:** the spec says "root-only file per VM". It is `root:bench 0640`,
  because a root-only key would force llama-server to run as root, which is worse. The key is
  still never world-readable and is shredded at teardown.
- **Placeholder scan:** the only operator-filled value is `$ips` in Task 5 Step 6. It is observed at
  run time by design (Review Focus 5) and cannot be known in advance.
- **Type consistency:** these were checked against the moved code:
  - `ARM_CAPACITY`;
  - `run_problems(r, arm, max_probe_failed)`;
  - `collect(results_root, max_probe_failed)`;
  - the `probe.max_failed` config key;
  - `worker_alias` `qwen3.6` (the same in `profiles.WORKER_MODEL`, `worker_serve.sh --alias` and the
    config);
  - `coordinator_alias` `qwen3.8-nothink` (the same as `profiles.ROUTER_MODEL`).
- **Review Focus:** all five lines have a test or live check, as noted against each.
