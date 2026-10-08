# Workforce experiment (Plan D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the pre-registered workforce experiment end to end: W1 (choose the worker configuration), W2
(freeze the real-repo task set), W3 (8 measured runs) and the build/no-build decision, with teardown.

**Architecture:** Plans A (router scoped keys + reserved lane, PR #7), C (harness, PR #8) and B (sandbox VM 176,
PRs #9/#11) are deployed. This plan adds the window machinery they lack. That machinery is already written and
tested on branch `feat/wf-experiment`; Task 1 merges it:
- host window control: 3 slots with RAG on, perturbers stopped, validity stamps;
- the user-lane probe;
- in-VM run control, with keys on stdin only;
- `76` run/harvest subcommands;
- run metadata and the W3 schedule;
- the transcript scan with task voiding;
- the W1 worker configurations C0–C3;
- the gate-task converter.

The rest of the plan is the experiment itself, run as live windows.

**Tech Stack:** Bash (host, guest, workers), Python 3 stdlib (harness, probe), opencode 1.18.34, llama.cpp
(mainline b11026 on LXC 151; ik_llama.cpp and mainline b11026 on the CPU workers), Proxmox `pct`/`qm`,
vSphere GuestOperations (`gate_guest.ps1`) for the workers.

**Spec:** `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md` (rev 2). The Plan C harness
README (`scripts/tools/workforce/README.md`) and the sandbox runbook (`docs/wf-sandbox-runbook.md`) are its
operating manuals.

## Global Constraints

**Waivers and decisions recorded 2026-10-07/08**
- User waivers: no host monitoring (spec §10 W0 item), no package proxy (the image is pre-warmed), no
  nested lab (ops tasks use fakes only).
- The sandbox SNAT address is `192.168.6.79`.
- Decision H measured that **3 × 128K chat fits with embed + rerank loaded**, so RAG stays on during
  windows.

**Roles and resources**
- The lead is `qwen3.8-nothink` through the router.
- Workers: Qwen3.6-35B-A3B (`/models/Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf`), 32 GB VMs `llmbench01-03`
  at `172.16.10.205-207:8090`, 64K context.
- opencode 1.18.34 is pinned.

**W1** (spec §7)
- 6 tasks, disjoint from W3: the gate's T1, T2, T4, plus 3 replayed tasks.
- 30 attempts per configuration (6 tasks × 5 runs).
- Configurations C0–C3, exactly the spec §7 table, presence penalty 0.
- Rule (`analysis.w1_choose`):
  - pick the lowest loop rate whose one-sided 95% upper bound is **≤ 10%**;
  - break ties on pass rate, then speed;
  - if no configuration qualifies, **stop and report: W3 does not run**.

**W2** (spec §8)
- About 16 tasks (the minimum is 14), **at least 3 ops scripts**.
- Each task's files and tests fit a 64K-token context.
- The set is validated: tests fail on the snapshot and pass with the reference, locally and in the
  VM's Docker grader.
- It is frozen and hashed before W3.

**W3** (spec §9)
- Arms G (lead + GPU implementer) and T (lead + 3 workers).
- **4 runs per arm, in the order `G T T G G T T G`**. No added runs and no sequential stopping.
- Every run processes the whole frozen set. Workers are cold-started once per window.
- A run is invalid **only** for an infrastructure failure, judged by `run-meta` and the harness.
- The decision is the three clauses of spec §9, computed by `cli.py w3`.
- **Clause 2 (throughput decides; GPU time is reported only) awaits the user's confirmation**, which
  must be given before the W3 pre-registration (Task 8).

**Keys** (spec §5.3)
- One scoped router key per window: aliases `qwen3.8-nothink`, TTL ≤ 336 h.
- One throwaway worker key per experiment.
- **Never on a command line, never printed.** Revoke and shred them at teardown.
- No production key enters the VM. Credentials for the lab live in `C:\Users\willi\.vcflab\credentials.env`.

**Operations**
- The workstation only opens and closes windows; nothing inside a run depends on it (spec §10).
- Budget: W1 6–10 h, W2 about 1 day, W3 8 × 1–2 h. **If a phase exceeds 150% of its budget, stop and
  report** (spec §11).
- Commits are conventional and made by explicit path, **with no `Co-Authored-By` trailer**. Merges and
  pushes need the user's approval each time.
- Production-impacting windows restart chat twice and need the user's go-ahead for each window.

## Review Focus

1. **A restart in the middle of a run** (the 04:00/16:00 UTC chat restart, a profile swap, a router
   deploy) must make the run invalid, not quietly score it.
   - Pinned by `test_any_restart_between_the_stamps_invalidates_the_run` (runmeta).
   - `wf-window.sh open` stops the restart timer and the swap webhook:
     `test_open_gives_three_slots_with_rag_and_stops_every_perturber`.
2. **A starved user lane** must raise the probe p50, not drop out of it.
   - `test_a_refused_probe_is_recorded_as_failed_not_dropped` (probe).
   - `test_probe_p50_counts_failures_at_the_timeout_value` (runmeta).
3. **A key leaking** onto a command line, into a log or onto disk for a run's whole duration.
   - `test_start_stores_keys_privately_and_never_in_argv`,
     `test_exec_deletes_the_key_file_then_runs_the_harness_with_the_keys_in_its_env`,
     `test_76_never_puts_a_key_on_a_qm_command_line`.
4. **A leaked answer deciding the outcome.**
   - `test_reading_the_answer_before_writing_it_taints_the_task` (scan).
   - `test_voiding_a_task_removes_it_from_quality_and_from_throughput` (cli).
5. **A worker serving a configuration other than the one the run is attributed to.**
   - `test_the_running_config_is_recorded`.
   - Task 6 records every worker's `gate-worker status` before each configuration's runs and refuses
     to start on a mismatch.

---

### Task 1: Merge and install the tested window machinery

**Files:** the branch `feat/wf-experiment`, 8 commits:

| Commit | What |
|---|---|
| `40e98cc` | worker configs C0–C3 in `worker_serve.sh start` |
| `e811b53` | `scripts/files/wf-window.sh` |
| `9a0466b` | `scripts/files/wf-user-probe.py` |
| `08cde5d` | `scripts/tools/workforce/runmeta.py`, `cli.py run-meta` / `w3-schedule` |
| `7bf289e` | `scripts/files/wf-run-control.sh` |
| `b468d52` | `76-vm-wf-sandbox.sh` run control |
| `7d99169` | `scripts/tools/workforce/scan.py`, `cli.py scan`, `w3 --void` |
| `7f374c0` | `scripts/tools/workforce/gate_tasks.py`, the W1 visible tests |

**Interfaces:**
- Produces:
  - `bash scripts/files/wf-window.sh open|close|stamp <file> <label>|status`;
  - `wf-user-probe.py run --out F [--interval S] [--until EPOCH]`;
  - `76-vm-wf-sandbox.sh push-control|push-bundles B|start-run ID T|G [--w1]|run-status ID|harvest ID DEST|clear-keys`;
  - `cli.py run-meta|w3-schedule|scan|w3 --void`;
  - `gate-worker start C0|C1|C2|C3`;
  - `gate_tasks.py <gate-task> <visible-dir> <id> <repo-out> <def-out>`.

- [ ] **Step 1:** Verify the branch:
  - `python3 -m pytest scripts/rag/tests scripts/files/tests scripts/tools/gate/tests -q`, expected 429 passed;
  - `python3 -m pytest scripts/tools/workforce/tests -q`, expected 150 passed, 1 skipped.

  Run the two sets as **separate invocations**: both trees have a `test_profiles.py` and a `profiles`
  module.
- [ ] **Step 2:** Push the branch, open the PR, and merge on the user's approval. Then fast-forward the
  host checkout: `ssh root@192.168.6.175 'cd /root/local-gpu-cluster && git fetch -q origin && git merge --ff-only -q origin/main'`.
- [ ] **Step 3:** Install into the VM:
  `ssh root@192.168.6.175 'cd /root/local-gpu-cluster && bash scripts/76-vm-wf-sandbox.sh push-control'`
  → `wf-run-control installed`.
- [ ] **Step 4:** Install the new `worker_serve.sh` on each worker from the workstation. Run this from
  `scripts/tools/gate` with `$env:VCFLAB_ROOT` set:

  ```powershell
  foreach ($w in 'llmbench01','llmbench02','llmbench03') {
    .\gate_guest.ps1 -VmName $w -Put ..\..\..\scripts\tools\gate\sh\worker_serve.sh -Remote /tmp/worker_serve.sh
    .\gate_guest.ps1 -VmName $w -Exec 'sudo install -m 0755 /tmp/worker_serve.sh /usr/local/bin/gate-worker && rm -f /tmp/worker_serve.sh && gate-worker status | head -3'
  }
  ```

  Expected: each prints `pid:` and `config:` lines. The workers are reachable only in the lab's
  Minimal mode (`vcf_lab_minimal_mode`).

### Task 2: C1 — mainline b11026 for the workers

VLAN 10 has no internet: the source goes in and builds natively.

- [ ] **Step 1:** Make a source tarball on the workstation:
  `git -C <llama.cpp clone> archive --format=tar.gz --prefix=llama.cpp/ b11026 > llama.cpp-b11026.tgz`.
  Use the same tag LXC 151 runs (`llamacpp_b11026_upgrade`).
- [ ] **Step 2:** Build it on each worker:
  1. `gate_guest.ps1 -Put llama.cpp-b11026.tgz -Remote /tmp/llama.cpp.tgz`.
  2. `-Exec 'sudo mkdir -p /opt/bench/src && sudo tar --no-same-owner -xzf /tmp/llama.cpp.tgz -C /opt/bench/src && sudo chown -R bench:bench /opt/bench/src/llama.cpp && cd /opt/bench/src/llama.cpp && cmake -B build -DGGML_NATIVE=ON -DLLAMA_CURL=OFF && cmake --build build -j 16 --target llama-server' -TimeoutSec 3600`.
     `gate_guest.ps1` runs as `bench`, so the tree must belong to `bench` before `cmake` writes into it.

  Expected: `/opt/bench/src/llama.cpp/build/bin/llama-server` exists.
- [ ] **Step 3:** Check the C1 flags against the binary.
  1. Run `-Exec '/opt/bench/src/llama.cpp/build/bin/llama-server --help'`.
  2. Grep the help for each flag:
     - `-cram` / `--cache-ram`;
     - `--ctx-checkpoints`;
     - `--spec-type` (and whether `draft-mtp` is listed);
     - `--spec-draft-n-max`.
  3. **If a flag name differs,** correct `ML_MEM` or `ML_MTP` in `worker_serve.sh` test-first:
     extend `test_c1_is_mainline_without_ik_only_flags`. Merge the fix before Task 5.
- [ ] **Step 4:** Decide MTP for C1:
  1. On one worker: `gate-worker install-key` with a throwaway key, then
     `GATE_C1_MTP=1 gate-worker start C1`.
  2. Read the server log.
     - If the model loads with MTP heads and `/health` turns ok, C1 is **MTP on** (`GATE_C1_MTP=1`).
     - If the load fails or reports MTP unsupported for this architecture, C1 is **MTP off**. Spec §7:
       "recorded, not dropped".
  3. `gate-worker stop`.
  4. Record the decision for the pre-registration.

### Task 3: Live pilot of the run machinery

Proves `76 start-run/harvest`, the window and the probe on real hardware, and measures the reviewer's
no-verdict rate. Uses the three gate-derived W1 bundles only: the pilot must not see W2. **Needs the
user's go-ahead: chat restarts twice.**

- [ ] **Step 1:** Build the three gate bundles on the workstation, from the repo root:

  ```bash
  W=scripts/tools/workforce; P=/tmp/wf-pilot; rm -rf $P
  for pair in T1:T1-ttl-cache T2:T2-semver T4:T4-ini-parser; do
    t=${pair%%:*}; d=${pair#*:}; id=w1-$(echo $t | tr A-Z a-z)
    python3 $W/gate_tasks.py docs/superpowers/gate/worker-tasks/$d docs/superpowers/workforce/w1/visible/$t $id $P/repo-$id $P/def-$id
    python3 $W/cli.py bundle-build --repo $P/repo-$id --taskdef $P/def-$id --out $P/bundles --refs $P/refs
  done
  python3 $W/cli.py bundle-validate --bundles $P/bundles --refs $P/refs --work $P/work
  ```

  Expected: `"problems": []` and manifest `b8b5a0fb859f4cd8374d328b4c8f58f301e8a7e468cc69906cf2ac426acc5e34`
  (the reproducible manifest measured 2026-10-08).
- [ ] **Step 2:** Open the window and issue the pilot key. On the host:
  1. `bash scripts/files/wf-window.sh open` → `open: 3 x 128K chat, embed + rerank loaded, perturbers stopped`.
  2. `pct exec 153 -- /usr/local/sbin/router-keys add --name wf-pilot --aliases qwen3.8-nothink --ttl-hours 24 --out /root/wf-pilot.key`
  3. `install -d -m 0700 /root/wf/keys && pct pull 153 /root/wf-pilot.key /root/wf/keys/router.key && chmod 600 /root/wf/keys/router.key && pct exec 153 -- shred -u /root/wf-pilot.key`.
- [ ] **Step 3:** Start the probe in LXC 153:
  1. `pct push 153 scripts/files/wf-user-probe.py /root/wf-user-probe.py --perms 0700`
  2. `pct exec 153 -- systemd-run --unit=wf-probe --collect /usr/bin/python3 /root/wf-user-probe.py run --out /root/wf-probe-pilot.jsonl --interval 60`

  It runs until Step 7 stops it. Never give it `--until`: a probe that ends before the window does
  leaves later runs without samples, and a T run without samples fails clause 3.
- [ ] **Step 4:** Run the arm-G pilot:
  1. Copy the bundles to the host, then `bash scripts/76-vm-wf-sandbox.sh push-bundles /tmp/wf-pilot/bundles`.
  2. Check the probe is alive (`pct exec 153 -- systemctl is-active wf-probe` → `active`), then
     `bash scripts/files/wf-window.sh stamp /root/wf/stamps.jsonl before-pilot-g`.
  3. `bash scripts/76-vm-wf-sandbox.sh start-run pilot-g G`.
  4. Poll `run-status pilot-g` every 5 minutes until `unit: inactive`.
  5. `stamp ... after-pilot-g`.
  6. `bash scripts/76-vm-wf-sandbox.sh harvest pilot-g /root/wf/runs`.
  7. `pct exec 151 -- journalctl -u llamacpp-chat --since "<before ts>" --until "<after ts>" -o cat > /root/wf/runs/pilot-g.journal`.
     Write each time in the form `2026-10-10 10:00:00 UTC`, converted from the stamp's
     `2026-10-10T10:00:00Z`: journalctl's ISO parsing varies by systemd version.

  Expected: `harvested pilot-g …` and `/root/wf/runs/pilot-g/run.json` with `"valid": true`.
- [ ] **Step 5:** Pull the probe file out of LXC 153
  (`pct pull 153 /root/wf-probe-pilot.jsonl /root/wf/wf-probe-pilot.jsonl`). Then check that the
  harvest holds no key: `grep -rlF -f /root/wf/keys/router.key /root/wf/runs` must print nothing. If it
  prints a file, stop: that is a key leak. Then compute the run metadata and run the scan on the
  workstation, after copying `/root/wf/runs`, the stamps and the probe output:
  - `python3 scripts/tools/workforce/cli.py run-meta --run-dir <runs>/pilot-g --stamps stamps.jsonl --label pilot-g --probe probe.jsonl --journal <runs>/pilot-g.journal`;
  - `python3 scripts/tools/workforce/cli.py scan --run <runs>/pilot-g --bundles /tmp/wf-pilot/bundles --refs /tmp/wf-pilot/refs`.

  Expected: `meta.json` with `valid: true`, a numeric `probe_p50` and `gpu_ms`; a scan with no tainted
  tasks.
- [ ] **Step 6:** Measure the no-verdict rate: the share of `reviews[].verdict == "NONE"` across
  `<runs>/pilot-g/tasks/*/record.json`.
  - **0–10%:** proceed.
  - **Above 10%:** stop. Bring the transcripts and a proposed `REVIEWER_PROMPT` change in
    `profiles.py` (test-first) to the user before any pre-registration. A prompt change is a harness
    change and must precede both pre-registrations.
- [ ] **Step 7:** Close the window and clean up:
  1. `pct exec 153 -- systemctl stop wf-probe`
  2. `bash scripts/76-vm-wf-sandbox.sh clear-keys`
  3. `pct exec 153 -- /usr/local/sbin/router-keys revoke --name wf-pilot && shred -u /root/wf/keys/router.key`
  4. `bash scripts/files/wf-window.sh close` → `closed: restored …`, and `/healthz` capacity 1 with
     all three upstreams ok.

### Task 4: W1 task set — three replayed tasks plus the gate tasks

**Files:**
- Create: `docs/superpowers/workforce/w1/tasks/w1-r{1,2,3}/` (each with `taskdef.json`, `request.md`,
  `hidden/test_*.py`).

Each replayed task gets the commit's own changed test file as its visible test. Its `files` are the
commit's changed non-test files, from `git show --numstat --format= <commit>`.

| ID | Commit | What it changed | Hidden check to author |
|---|---|---|---|
| w1-r1 | `3d75bd4` | the monitor emits fan-out check IDs on failure | a non-JSON `/healthz` body fails under exactly the three `router_*_upstream` IDs; with `gpu_card_count: 3`, a failed `rocm-smi` yields `gpu_vram_0..2` |
| w1-r2 | `9017c99` | RAG removes chunked like adds (`plan_embed_batches`) | 610 adds and 610 removes at batch 50: no call carries more than 50 removes, every remove appears exactly once, and a removes-only run still makes calls |
| w1-r3 | `ad6e575` | alert cooldown keyed by `(check, status)` | `fail` then `warn` for the same check within the cooldown both fire; a repeat of the same status within the cooldown does not |

- [ ] **Step 1:** Write `request.md` for each task in the style of an issue: the symptom and the
  wanted behaviour, taken from the commit message's *problem* paragraphs. **Never the code, a function
  name the fix introduces, or the fix's wording.** For `plan_embed_batches`, ask for the behaviour;
  the visible test names the function, so naming it is not a leak.
- [ ] **Step 2:** Write each hidden test. It must:
  - import only what the visible test imports;
  - **fail on the parent commit and pass on the commit**.

  Check that directly: `git worktree add /tmp/p <commit>~1`, copy the hidden test into the visible
  test's directory, `pytest` it (expect FAIL); repeat at `<commit>` (expect PASS).
- [ ] **Step 3:** Build all six W1 bundles into one directory (`/tmp/wf-w1/bundles`):
  - the three gate tasks exactly as in Task 3 Step 1;
  - `cli.py bundle-build --repo . --taskdef docs/superpowers/workforce/w1/tasks/<id>` for each replay.

  Then validate them, first locally and then in the VM:
  - `cli.py bundle-validate --bundles /tmp/wf-w1/bundles --refs /tmp/wf-w1/refs --work /tmp/wf-w1/work`
    → `"problems": []`;
  - `bash scripts/76-vm-wf-sandbox.sh validate /tmp/wf-w1/bundles /tmp/wf-w1/refs` → `"problems": []`
    and the same manifest. **A difference between the two is a parity failure: stop.**
- [ ] **Step 4:** Commit the task definitions:
  `git add docs/superpowers/workforce/w1/tasks && git commit -m "test(workforce): W1 replayed tasks w1-r1..r3"`.

### Task 5: W1 pre-registration

**Files:** Create `docs/superpowers/workforce/preregistration-w1.json`.

- [ ] **Step 1:** Write the file, filling in each `<…>` from this session's real outputs:

  ```json
  {"phase": "W1", "date": "<UTC date>", "spec": "docs/superpowers/specs/2026-10-07-distributed-workforce-design.md#7",
   "tasks": {"ids": ["w1-r1", "w1-r2", "w1-r3", "w1-t1", "w1-t2", "w1-t4"], "manifest": "<Task 4 manifest>"},
   "configs": {"C0": "<gate-worker status config line>", "C1": "<... with the Task 2 MTP decision>",
               "C2": "<...>", "C3": "<...>"},
   "config_order": ["C0", "C2", "C3", "C1"], "runs_per_config": 5, "attempts_per_config": 30,
   "metric": "scripts/tools/workforce/loops.py: repeat = 3+ identical consecutive tool calls; cycle = a sequence of up to 4 calls repeated 3+ times; a test re-run counts only if no file changed between",
   "rule": "analysis.w1_choose: lowest loop rate with one-sided 95% Clopper-Pearson upper bound <= 0.10; ties by pass rate then speed; none qualifies -> stop, no W3",
   "harness_commit": "<git rev-parse HEAD>", "budget_h": 10, "stop_at_h": 15}
  ```

  To get each config line:
  1. Start each configuration once on one worker.
  2. Copy the `config:` line that `gate-worker status` prints.
  3. Stop the worker.

  The line is the exact flag string the runs will use.
- [ ] **Step 2:** Commit it alone, by path: `git commit -m "docs(workforce): W1 pre-registration"`. Push
  it on the user's approval **before** Task 6 starts.

### Task 6: W1 window — the loop check

**Needs the user's go-ahead** (chat restarts twice; workers busy for up to 10 h).

- [ ] **Step 1:** Open the window and issue the keys.
  1. Open the window as in Task 3 Step 2, issuing key name `wf-w1` with `--ttl-hours 24`.
  2. On the workstation, generate the throwaway worker key: 32 random bytes as hex, into a file the user
     alone can read.
  3. Install it on every worker: `gate_guest.ps1 -Put <key> -Remote /tmp/worker.key`, then
     `-Exec 'gate-worker install-key /tmp/worker.key'` (it shreds the upload).
  4. Copy it to the host as `/root/wf/keys/worker.key` (mode 0600) and shred the workstation copy.
  5. Lock every worker to the sandbox: `-Exec 'gate-worker quiet && gate-worker lock 192.168.6.79'`.
- [ ] **Step 2:** Push the bundles: `bash scripts/76-vm-wf-sandbox.sh push-bundles /tmp/wf-w1/bundles`.
  Then run `WF_PROOF_WORKERS= bash scripts/76-vm-wf-sandbox.sh proof`, the full proof with the workers
  included. It must exit 0. **Any failure: no window.**
- [ ] **Step 3:** For each configuration in the pre-registered order `C0 C2 C3 C1`:
  1. Start the workers with this configuration: on every worker, `gate-worker stop`, then
     `gate-worker start <cfg>` (for C1, prefix `GATE_C1_MTP=<decision>`).
  2. Record every worker's `gate-worker status` into `/root/wf/w1-workers.txt`. **Refuse to continue
     if any `config:` line differs from the pre-registration, or any `health:` line is not ok**
     (Review Focus 5). `stop` clears the label, so a failed start shows `config: none`.
  3. Run 5 times, for n = 1..5:
     1. `bash scripts/76-vm-wf-sandbox.sh start-run w1-<cfg>-<n> T --w1`;
     2. poll `run-status` every 5 minutes until the unit is inactive;
     3. `harvest w1-<cfg>-<n> /root/wf/runs`.
  4. Check the clock against the budget: past **15 h** in total, stop and report (spec §11).
- [ ] **Step 4:** Clean up:
  - stop the workers;
  - on every worker, run `gate-worker teardown` (it shreds the worker key);
  - `clear-keys`;
  - revoke `wf-w1`;
  - `wf-window.sh close`.
- [ ] **Step 5:** Decide, on the workstation, after copying `/root/wf/runs`:

  ```bash
  python3 scripts/tools/workforce/cli.py w1 \
    --config C0=<runs>/w1-C0-1,<runs>/w1-C0-2,<runs>/w1-C0-3,<runs>/w1-C0-4,<runs>/w1-C0-5 \
    --config C1=... --config C2=... --config C3=...
  ```

  Expected: one JSON choice.
  - If **no configuration qualifies**, write the result to
    `docs/superpowers/workforce/results-w1.json`, commit it, report it to the user, and **stop the
    plan: W3 does not run.**
  - Otherwise commit the result and carry the chosen configuration into Task 8.

### Task 7: W2 — the real-repo task set

**Files:** Create `docs/superpowers/workforce/w2/tasks/<id>/` (each with `taskdef.json`, `request.md`,
`hidden/test_*.py`).

**Primary set:** 16 tasks, 3 of them ops scripts. None is a W1 commit.

| ID | Commit | Kind | Hidden check to author |
|---|---|---|---|
| w2-01 | `79a2f69` | ops | Phase 4.8 enables `zfs-import@tank.service` and does not depend on the zpool cache |
| w2-02 | `1f51fa5` | ops | the `lock` ruleset closes every brace on its own line, also with three IPs |
| w2-03 | `e553446` | ops | `start` runs the server in its own memory-capped unit, outside open-vm-tools |
| w2-04 | `c4d8403` | router | `qwen3.8-nothink` resolves; model-card sampling is injected only when the request omits it |
| w2-05 | `bc85434` | router | a scoped request whose slot arrives after the layout shrank is refused, and the slot is released |
| w2-06 | `abfa787` | router | `web_fetch` checks every redirect hop's host, not only the first |
| w2-07 | `d547fbc` | router | a repeated Tavily search within 6 h is served from cache; after 6 h it is fetched again |
| w2-08 | `3259c5a` | rag | an unchanged document still advances `last_fetched`; markers split on any whitespace |
| w2-09 | `f0eb0ac` | rag | a Sphinx site is discovered through `searchindex.js`; requests carry a User-Agent |
| w2-10 | `19da0b2` | rag | every chunk of a long document carries a citable URL |
| w2-11 | `8b107e4` | monitor | GPU checks run `rocm-smi` through `pct exec 151` |
| w2-12 | `5265c8d` | monitor | concurrent collector and HTTP access to the store do not corrupt it |
| w2-13 | `3828f7f` | monitor | VRAM *total* and *used* keys are not confused |
| w2-14 | `467b5e6` | monitor | the store keeps last-ok and a bounded sample window |
| w2-15 | `f5ae5b6` | monitor | the alert engine fires on transitions only and respects the cooldown |
| w2-16 | `2f51899` | sandbox | unanswered multicast passes; answered multicast and unanswered unicast fail |

**Reserves**, used in order **within the same kind** when a primary fails a check:
- ops: `817a624`, `0085146`;
- router: `f18bb47`, `5d942d7`;
- rag: `2cd3ca5`;
- monitor: `e8cd1d8`, `81ebf59`, `c63a3f6`, `d7e2c38`, `e4a93e3`.

Record every replacement and its reason in `docs/superpowers/workforce/w2/selection.md`.

- [ ] **Step 1:** Run the 64K check for each task. Use the sum of bytes, divided by 3.5, of the
  declared files and the visible tests at the parent commit:

  ```bash
  python3 - <commit> <files...> <<'EOF'
  import subprocess, sys
  c, files = sys.argv[1], sys.argv[2:]
  n = sum(len(subprocess.run(["git", "show", f"{c}~1:{p}"], capture_output=True).stdout) for p in files)
  print(round(n / 3.5), "tokens", "OK" if n / 3.5 <= 64000 else "TOO BIG")
  EOF
  ```

  If it prints **TOO BIG**, use a reserve.
- [ ] **Step 2:** Author `request.md` and the hidden tests exactly as in Task 4 Steps 1–2. The same
  rules apply: no fix wording, and fail-then-pass checked at `<commit>~1` and `<commit>`.
- [ ] **Step 3:** Build and validate:
  1. Build all 16 into `/tmp/wf-w2/bundles`.
  2. `cli.py bundle-validate` → `"problems": []`.
  3. `76 validate` → `"problems": []` with the same manifest.
  4. If a task fails only in Docker (a missing dependency), replace it with a reserve. **Never change
     the grader image after W1.**
- [ ] **Step 4:** Freeze the set:
  - `cli.py manifest --bundles /tmp/wf-w2/bundles`;
  - commit `docs/superpowers/workforce/w2/`;
  - archive `/tmp/wf-w2` to `/root/wf/w2-frozen.tgz` on the host, recording its sha256 next to the
    manifest.

### Task 8: W3 pre-registration

- [ ] **Step 1:** Ask the user to confirm decision-rule clause 2. Throughput (accepted tasks per hour,
  with a 95% CI excluding 0) decides; lead GPU time per accepted task is reported only. **No W3 without
  the answer.** If they choose "throughput **or** GPU time", change `analysis.w3_decide` test-first
  before writing the pre-registration.
- [ ] **Step 2:** Create `docs/superpowers/workforce/preregistration-w3.json`:

  ```json
  {"phase": "W3", "date": "<UTC date>", "spec": "docs/superpowers/specs/2026-10-07-distributed-workforce-design.md#9",
   "tasks": {"manifest": "<Task 7 manifest>", "archive_sha256": "<w2-frozen.tgz sha256>", "n": 16},
   "worker_config": "<Task 6 choice and its config line>",
   "arms": {"G": "lead + 1 GPU implementer (qwen3.8-nothink)", "T": "lead + 3 CPU workers"},
   "schedule": ["G", "T", "T", "G", "G", "T", "T", "G"], "runs_per_arm": 4,
   "no_added_runs": true, "no_sequential_stopping": true,
   "validity": "runmeta.validity + harness run.json valid; nothing else invalidates a run. A T run with no probe samples is valid but fails clause 3",
   "rule": {"quality": "paired task bootstrap, n_boot 10000, seed 20261007, lower95(T-G) >= -1/n",
            "win": "Welch 95% CI of T-G accepted/hour excludes 0 (lo > 0)",
            "user": "every valid T run's time-weighted probe p50 <= 1.5 x its window's idle baseline p50; failed probes count as 120 s",
            "clause2_confirmed_by_user": "<date and answer>"},
   "void": "tasks the transcript scan finds tainted, applied to every run of both arms",
   "harness_commit": "<git rev-parse HEAD>", "budget_h": 16, "stop_at_h": 24}
  ```

- [ ] **Step 3:** Commit it alone, by path, and push on the user's approval before Task 9.

### Task 9: W3 window(s) — the measured runs

**Needs the user's go-ahead.** Each window: chat restarts twice, workers busy, and the user's own chat
on the reserved lane.

- [ ] **Step 1:** Open the window exactly as Task 6 Step 1, with the router key named `wf-w3`. Then:
  1. Check the sha256 of `/root/wf/w2-frozen.tgz` against the pre-registration. Unpack it with
     `rm -rf /root/wf/w2 && mkdir -p /root/wf/w2 && tar -xzf /root/wf/w2-frozen.tgz -C /root/wf/w2`,
     then `bash scripts/76-vm-wf-sandbox.sh push-bundles /root/wf/w2/bundles`.
  2. Run the full proof with the workers included.
  3. Start the workers with the chosen configuration and record their status (Review Focus 5).
- [ ] **Step 2:** Measure the idle baseline: 30 minutes of probes with the window open and no run.

  ```bash
  pct exec 153 -- python3 /root/wf-user-probe.py run --out /root/wf-baseline-<window>.jsonl --interval 60 --count 30
  ```

  `<window>` is `w1` for the first window, `w2` for a second, and so on. Each window gets its own
  baseline file, and each run is compared with its own window's baseline.

  Then start the run-long probe. Use no `--until`; Step 4 stops it:

  ```bash
  pct exec 153 -- systemd-run --unit=wf-probe --collect /usr/bin/python3 /root/wf-user-probe.py run --out /root/wf-probe-w3.jsonl --interval 60
  ```

- [ ] **Step 3:** Run the eight runs in the pre-registered order, `G T T G G T T G`, named
  `w3-1-G … w3-8-G`. For each run:
  1. check `pct exec 153 -- systemctl is-active wf-probe` → `active` (restart it with the same command
     if not), then `wf-window.sh stamp /root/wf/stamps.jsonl before-<run>`;
  2. `76 start-run <run> <arm>`;
  3. poll every 5 minutes until the unit is inactive;
  4. `stamp after-<run>`;
  5. `76 harvest <run> /root/wf/runs`;
  6. capture the chat journal for the run's window, as in Task 3 Step 4.

  If a run is invalid, it stays in the schedule as invalid. **Do not repeat it** (spec §9: no added
  runs). If the window must close before all eight runs:
  1. close it as in Task 6 Step 4;
  2. reopen it later with fresh stamps and a fresh baseline;
  3. continue with the next run number. The order never changes.
- [ ] **Step 4:** Close the window as in Task 6 Step 4, adding `systemctl stop wf-probe` in LXC 153.
  Pull the probe and baseline files out of LXC 153 (`pct pull 153 /root/wf-probe-w3.jsonl …` and each
  `/root/wf-baseline-<window>.jsonl`) into `/root/wf`. Check that no key reached a harvest:
  `grep -rlF -f /root/wf/keys/router.key /root/wf/runs` and the same for `worker.key` print nothing.
  Then copy `/root/wf` (runs, stamps, probe files) to the workstation.
- [ ] **Step 5:** Analyse:
  1. For each run: `cli.py run-meta --run-dir <runs>/<run> --stamps stamps.jsonl --label <run> --probe wf-probe-w3.jsonl --journal <runs>/<run>.journal`.
  2. For each run: `cli.py scan --run <runs>/<run> --bundles /tmp/wf-w2/bundles --refs /tmp/wf-w2/refs`.
     Gather every tainted task ID.
  3. `cli.py w3-schedule --baseline-probe wf-baseline-w1.jsonl --run G=<runs>/w3-1-G --run T=<runs>/w3-2-T … --out schedule.json`.
     A run measured in a later window names its window's baseline:
     `--run T=<runs>/w3-6-T@wf-baseline-w2.jsonl`.
  4. `cli.py w3 --schedule schedule.json --tasks /tmp/wf-w2/bundles [--void <id> …]`.

  Expected: one JSON decision, with `build`, `failed` clauses and `voided`.
- [ ] **Step 6:** Read every **suspect** task's transcript by hand. Report each one, with what it
  wrote, in the results.

### Task 10: Decision, teardown and record

**Files:**
- Create: `docs/superpowers/workforce/results-w3.json` and `docs/superpowers/workforce/decision.md`.
- Modify: the spec's §13 decision record.

- [ ] **Step 1:** Write `results-w3.json` (the `w3` output plus every run's `meta.json`) and
  `decision.md`. The decision document gives:
  - the verdict;
  - which clause failed, if any;
  - lead GPU time per accepted task, per arm (reported);
  - rework rounds, loops, cap hits;
  - voided tasks;
  - suspect transcripts;
  - every limit the spec names: snapshot tasks are easier than live work; one repository; the 64K bias;
  - the GPU-time limit: `gpu_ms` is all chat-server time in the run minus the probe's, so it includes
    arm G's implementer and any chat of the user's own. Lead and implementer share an alias, so
    spec §9's per-alias attribution was not possible. It is reported, not decisive.

  Commit both files by path.
- [ ] **Step 2:** Teardown (spec §11):
  1. Revoke every scoped key: `router-keys revoke --all`, after listing to confirm that only `wf-*`
     keys exist.
  2. Make sure no worker key remains anywhere: workers torn down, host copy shredded.
  3. Confirm the chat layout and timers are restored: `wf-window.sh status` shows the window closed,
     and `/healthz` shows capacity 1.
  4. Confirm the worker servers are stopped.
  5. Harvest everything still in the VM: `76 run-status` and `harvest` for every run, plus
     `/srv/wf/runs/*.log`.
  6. **Ask the user** before destroying the VM: `docs/wf-sandbox-runbook.md` § Teardown.
  7. **Ask the user** whether to keep the router change (Plan A). If not, roll back to the kept
     `/opt/llm-router` tree and verify the aliases.
- [ ] **Step 3:** Update memory with the decision and any new footguns. The exported
  `workforce/*` branches stay for the user to merge or delete.
