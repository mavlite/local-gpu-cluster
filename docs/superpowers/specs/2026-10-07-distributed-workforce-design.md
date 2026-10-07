# Distributed workforce: GPU lead + CPU worker agents — design (experiment)

**Status:** rev 2, 2026-10-07. It folds in four adversarial reviews (security, experiment validity, ops,
agent architecture), kept in `.superpowers/sdd/workforce-review/`, and the user's decisions A–H recorded in
§13. **Not yet approved.**

**Supersedes:** the capacity part of `2026-10-05-minisforum-cluster-integration-design.md` §5. Its workers
could not execute code. The Phase-1 gate's Polyglot quality data stays valid.

**Decision this produces:** whether to build the workforce (Phase 2) or not.

## 1. Goal

The GPU model (**lead**) reviews and directs work done by CPU worker **agents** on the three Minisforum
nodes. Workers are real agents: they read code, edit it, **run tests** and iterate. The lead reviews each
result, then accepts it or sends it back with feedback.

**We measure first.** Phase 2 is built only if the team beats the best GPU-only setup on real tasks from
this repository, by the rule in §9.

## 2. Evidence that shaped this design

- **Coding quality is a tie** on Aider Polyglot Python: workers scored 62.7% pass@2 (n=6), the coordinator
  63.5% (n=5).
- **The gate's no-execution harness made workers loop.**
  - Workers rewrote an identical `verify.py` 16–37 times, because they could not run their tests.
  - Presence penalty 1.5 did not stop the loops. Its look-back is 64 tokens; the repeats were ~1K tokens.
    It also cost 3.9 points of pass@2.
  - opencode's `doom_loop` cannot see one-call-per-turn loops (upstream #51965). opencode's `steps` limit is
    honoured but only advisory.
- **Workers need 32 GB VMs.** At 24 GB the RAM prompt cache got them OOM-killed.
- **In the only clean pair**, the 3-slot GPU ran 99 tasks/h at 6/6 passed; the CPU workers ran 48 tasks/h at
  4/6.
- **The lead never looped** under the same restrictions. That rules out the restrictions alone; the model or
  the engine is still a candidate.

## 3. Scope

**In scope:**
- a **sandbox VM** that executes every agent action;
- a **harness pipeline** that dispatches tasks, drives the review and rework loop, and enforces time caps;
- the **lead review protocol**;
- **router changes**: per-run scoped keys and a reserved user lane. The `web_fetch` SSRF fix ships separately
  and first (decision G);
- **W1**: a loop check that chooses the worker configuration;
- **W2**: a set of ~16 real tasks replayed from this repository's history;
- **W3**: measurement against a pre-registered rule;
- **host monitoring** for the duration of the runs.

**Out of scope until a build decision (roadmap):**
- entry points: an opencode session, Claude Code hand-off, a task queue;
- the lead doing its own planning and decomposition (see §12);
- research and analysis work types;
- internet access for agents (only measured runs are tested, and they are offline);
- persistent services;
- running alongside Full lab mode. Workers exist only in **Minimal** mode.

## 4. Roles and resources

| Role | Model / place | Notes |
|---|---|---|
| Lead (reviewer) | `qwen3.8-nothink` via router, LXC 151 | Reviews and gives feedback. After 2 failed rounds it fixes the task itself, and that is credited to the lead (§9). |
| GPU implementer (baseline arm G) | same model, a second GPU slot | Implements tasks in the GPU-only arm. |
| Workers ×3 (arm T) | Qwen3.6-35B-A3B, llmbench01-03, 32 GB | Engine and flags chosen by W1. |
| User | the user's own chat/RAG | A **reserved router lane** (§5.4). RAG stays on if VRAM allows (§10). |
| Harness | Python, runs **on the sandbox VM** | Not on the workstation; see §10 for worker control. |

**Chat layout during runs:** 3 slots at ≤128K each — lead, implementer/spare, user.

The GPU-only baseline (arm G) uses the lead plus one GPU implementer. That is the best the GPU can do alone
in the same layout (decision C).

## 5. Sandbox and boundaries

### 5.1 Sandbox VM (decision F)

- A **dedicated Proxmox VM** built on the tester-VM pattern (`72-vm-tester.sh` + `73-vm-tester-firewall.sh`).
  Agent code never runs on the host kernel or in an LXC.
- The PVE firewall enforces egress (§5.2). Docker's rules are **not** used for it.
- opencode 1.18.34 is pinned, installed and **pre-warmed in the image**: plugins installed, `--pure`, no
  autoupdate, so no install happens at run time.
- The image also carries Python 3, pytest, shellcheck, git and the repository's test dependencies. A
  **package proxy** (PyPI and npm allowlist only) covers the rest.
- **Each task gets its own isolated workspace:**
  - a `git archive` single-commit snapshot of the task's parent commit;
  - plans, ledgers, `.superpowers/` and `docs/superpowers/plans|specs` stripped;
  - no remotes and no history.
  - W0 proves the answer blobs are absent.
- **Agent configs live outside the writable workspace**, read-only, so agents cannot rewrite their own
  permissions.
- opencode's built-in `general` and `explore` subagents are **disabled**.
- Session DBs and logs live on a **persistent volume** and are harvested before any teardown.

### 5.2 Egress (decisions A and E), enforced by the PVE firewall on the VM's NIC

| Destination | Allowed |
|---|---|
| Router `192.168.6.153:8000` | yes, with a per-run scoped key |
| Workers `172.16.10.205-207:8090` | yes, with the worker key |
| Package proxy (PyPI, npm registries) | yes |
| Nested lab **guests** `10.50.10.2-254` | yes — ops tasks |
| `10.50.10.1` (the Proxmox host on vmbrlab) | **no**, explicitly |
| The internet | **no** in measured runs: the repo is public and its answers are one fetch away |
| User LAN, workstation, Proxmox host, production LXCs, VCF VLANs, CRS309, `192.168.6.1` admin UI | **no** |
| IPv6 | disabled on the VM |
| DNS | a resolver that answers only the proxy and allowlisted names |

The nested lab's outbound NAT is closed for its guests as well, so the lab cannot be used as a pivot.

### 5.3 Keys

- **Per-run scoped router keys** carry an alias allowlist (the lead and implementer aliases only), an
  expiry, and **no server-side tools** (`tool_execution=server` is refused). They are revoked at teardown.
- **The worker key** is throwaway: rotated per experiment, shredded at teardown.
- **No production key enters the VM.** The grader holds no keys.

### 5.4 Router changes (decision D), production-impacting

- **Scoped keys**, as in §5.3.
- **A reserved user lane.** Admission is split by key class, so workforce keys can hold at most
  `total_slots − 1` concurrently. The user's key always has a slot.
- **The profile-swap webhook** is disabled for scoped keys: a workforce request can never swap the GPU
  profile.
- **Process for each change:** test-first, a security review, a staged deploy with a **rollback** (keep the
  previous `/opt/llm-router` tree), and verification of the live aliases after deploy (`qwen3.8-nothink`
  was lost once before).

### 5.5 Write-back and grading

- **Accepted work leaves the VM as patches only.** Each patch is limited to the task's declared files.
- **Rejected paths:** `.mcp.json`, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `opencode.json`, `.opencode/`,
  `conftest.py`, `pytest.ini`, `setup.cfg`, `pyproject.toml`, `.gitattributes`, `.lfsconfig`, `.gitmodules`,
  hooks, and deployed `scripts/*.sh` outside the task's file list.
- **Import:** on the workstation, `git apply --check`, then a branch `workforce/<run>/<task>`. **The user
  merges.**
- **Grading** runs in a fresh, network-less container: the task's original tests plus hidden checks, with
  a harness-owned pytest config, `--noconftest`, and `-p no:cacheprovider`. Hidden tests never enter an
  agent workspace.

## 6. Review protocol (decision B: harness-driven)

For each task:
1. **The harness dispatches it** to a worker: `opencode run`, with the worker agent as the primary agent,
   in that task's workspace. The prompt carries the task text and acceptance checks.
2. **The worker iterates** — edit, run tests — under a per-task wall-clock cap and step limit.
3. **The harness hands the lead the diff and test output** in a fresh, task-scoped session. The lead answers
   `ACCEPT` or `REVISE: <feedback>`. It may run tests in the sandbox. It has **no edit rights in the review
   step**.
4. **On `REVISE`**, the harness resumes the **same worker session** with the feedback (opencode `task_id` or
   session continuation). At most 2 rounds.
5. **After 2 failed rounds**, the harness gives the lead a separate fix session with edit rights. The result
   is recorded as `lead-fixed`.
6. **The harness grades the task**, then exports the patch.

A per-task session for the lead removes context overflow and compaction. Per-task dispatch removes the
straggler blocking. Every lead edit happens in the explicit fix step, so attribution is mechanical.

Arm G uses the same steps, with the GPU implementer in place of the worker.

## 7. W1 — loop check (choose the worker configuration; reliability first)

**Tasks.** 6 loop-prone tasks, **disjoint from W3's set**: the gate's T1, T2 and T4, plus 3 more
replayed tasks.

**Configurations:**

| ID | Engine | MTP | Thinking | Presence |
|---|---|---|---|---|
| C0 | ik_llama.cpp | on | off | 0 |
| C1 | mainline llama.cpp b11026 on CPU | per mainline support | off | 0 |
| C2 | ik_llama.cpp | off | off | 0 |
| C3 | ik_llama.cpp | on | on, `preserve_thinking: true` | 0 |

C1 needs mainline b11026 built for the workers and copied in, because VLAN 10 has no internet. If mainline
MTP is unsupported for this model, C1 runs without MTP; that is recorded, not dropped.

**Runs.** Each configuration gets **30 attempts** (6 tasks × 5), with bash available in the sandbox.

**Loop metric**, computed from the session DB:
- a **repeat**: 3 or more consecutive identical tool calls (same tool, same arguments), **or**
- a **cycle**: a sequence of up to 4 calls repeated 3 or more times.

Legitimate test runs are excluded only when the files changed between them.

**Rule.**
- Pick the configuration with the **lowest loop rate**, with an upper 95% bound **≤ 10%**.
- Break ties on pass rate, then on speed.
- If no configuration meets 10%, stop and report. W3 does not run.

Frozen before W1: the tasks, the configurations, the metric and the rule.

## 8. W2 — real-repo task set (historical replay)

- **About 16 tasks** from this repository's commits that came with tests. The minimum is 14, so the paired
  analysis has power.
- Each task's relevant files and tests must fit a **64K-token worker context**. This biases the set toward
  smaller tasks; that limit is recorded.
- **At least 3 are ops scripts**, tested with fakes or against nested-lab guests.
- **Each task provides:**
  - the snapshot (§5.1);
  - an issue-style request;
  - the visible original tests;
  - hidden checks.
- **The set is validated:** each reverted snapshot fails its tests, and the original commit passes them.
- **Frozen and hashed** before W3.
- Transcripts are scanned for leaked solutions. A tainted task is voided and reported.

## 9. W3 — measurement and pre-registered rule

**Arms**
- **G:** the lead plus a GPU implementer.
- **T:** the lead plus 3 CPU workers.

Both arms use the same protocol, sandbox, layout and reviewer.

**Schedule**
- **4 runs per arm**, fixed in advance, interleaved `G T T G G T T G`.
- **No added runs and no sequential stopping.**
- A run processes the whole frozen set. Workers are cold-started once per window, not per run.

**Measured per run**
- accepted tasks, split into **worker-accepted** and **lead-fixed**;
- wall-clock time;
- **accepted tasks per hour**, counting worker-accepted only for T and implementer-accepted only for G;
- lead GPU time: chat-server prompt+eval ms, attributed per request by slot and alias;
- user-lane probe p50 against its idle baseline;
- rework rounds, loop episodes, step-limit and time-cap hits.

**Validity.** A run is invalid **only** for an infrastructure failure. The tests are mechanical:
- the monitored host rebooted or hung;
- the router or chat server restarted, detected from `systemctl` timestamps;
- worker unreachability lasted 5 minutes or more;
- the harness threw an exception.

Loops, caps and failed tasks are **outcomes**. Every timer that can restart chat, and the swap webhook, are
stopped for the window.

**Decision rule.** Build Phase 2 only if all of these hold:
1. **Quality.** Over the 16 tasks × 4 runs, T's per-task acceptance rate is not lower than G's by more than
   1 task per run. This is tested with a paired task-level bootstrap; the 95% lower bound of (T − G) must be
   ≥ −1/16.
2. **Win.** T's accepted tasks per hour exceeds G's. The 95% CI of the per-run difference must exclude 0.
   Lead GPU time per accepted task is **reported**, not decisive.
3. **User protection.** The user-lane probe p50 is ≤ 1.5× its idle baseline in every T run.

**If it isn't built**, record which clause failed. Clause 2 is the proposed change to the user's earlier
"either throughput or GPU time" (§13, item 11), pending the user's confirmation.

## 10. W0 — preconditions

**Host monitoring**
- Remote syslog and netconsole to a listener **off the Proxmox host**.
- pstore where the board supports it, and the `sp5100_tco` watchdog **if present**; both are verified, not
  assumed.
- `hung_task_panic` stays **off**: External-Tank's long I/O stalls would trigger it.

**Routes**
- Make the sandbox VM's path to `172.16.10.0/24` (via `192.168.6.11`) persistent.
- Route the VM to nested-lab guests through PVE, with the `.1` and outbound-NAT blocks in place.

**Boundary proof.** From inside the VM, run every row of §5.2's table, including `10.50.10.1`, IPv6, the
host and the workstation. Record the results.

**VRAM check (decision H)**
- Measure 3 × 128K chat plus embed and rerank on the V620s.
- If it doesn't fit, bring the options back to the user before W3.

**Router changes** (§5.4): deployed with rollback, and live aliases verified afterwards.

**Worker control**
- Worker servers run as persistent `gate-llama` systemd units started once per window.
- The workstation (Wi-Fi/VPN) is used only to start and stop the window, never inside a run.

**Lab** in Minimal mode, with workers at 32 GB.

**Pre-registration commit** before W1, and again before W3:
- task hashes;
- W1 configurations and rule;
- W3 arms, run count, rule and validity tests;
- time budget.

## 11. Time budget and teardown

**Budget**
- W1: 4 configurations × 30 attempts ≈ 6–10 h on CPU, run across the 3 workers in parallel.
- W2: authoring ≈ 1 day.
- W3: 8 runs × ~1–2 h.

If any phase exceeds 150% of its budget, stop and report.

**Teardown**
1. Revoke scoped keys and shred the worker key.
2. Restore the chat layout and the timers.
3. Stop the worker servers.
4. Harvest the session DBs and logs.
5. Destroy the sandbox VM.
6. Roll the router back if the change isn't kept.
7. Commit results and the decision.

The exported `workforce/*` branches stay for the user to merge or delete.

## 12. Risks and open questions

- **The lead's review is the bottleneck.** That is acceptable for the decision: clause 2 measures the
  team's net throughput.
- **The harness pipeline replaces the lead's own planning.** If the workforce is built, a lead-planning
  layer — opencode `task` with session resume, or experimental background subagents — must be validated
  separately before it ships.
- **Snapshot tasks are easier than live repo work**: no merges and no concurrent edits. That limit is
  recorded.
- **Host stability is unknown.** Monitoring makes a recurrence diagnosable; it doesn't prevent one.
- **The task set is small and from one repository.** Generalising beyond it is not established.

## 13. Decision record (user, 2026-10-07)

| # | Question | Answer | Status |
|---|---|---|---|
| 1 | End state | Measure, then decide | — |
| 2 | Entry points | opencode, Claude Code and a queue | Roadmap |
| 3 | Work types | All four | First slice is real-repo changes |
| 4 | Execution | Full shell with internet | **Revised by A**: no internet in measured runs |
| 5 | First slice | Real-repo changes | — |
| 6 | Write-back | Branch that the user merges | **Hardened**: path-restricted patches |
| 7 | GPU sharing | 3-slot mode while running | **Revised by D**: a real reserved lane |
| 8 | Keys | Scoped per run | — |
| 9 | Rework | 2 rounds, then the lead fixes it | Credited to the lead |
| 10 | Baseline | Lead does it all | **Revised by C**: lead plus GPU implementer |
| 11 | Success | Throughput **or** GPU time | **Proposed**: throughput decides, GPU time is reported |
| 12 | Ops boundary | Nested lab | **Fenced by E** |
| 13 | Task source | Historical replay | Clean snapshots |
| 14 | Host | Proceed with monitoring | — |
| 15 | Trade-off | Reliability first | — |
| 16 | Availability | Minimal mode only | — |
| A | Leak vs internet | No internet in measured runs | — |
| B | Orchestration | Harness pipeline | — |
| C | Baseline | Lead plus 1 GPU subagent | — |
| D | User GPU use | Build a real reservation | — |
| E | Nested lab | Keep, fenced | — |
| F | Sandbox | Dedicated VM | — |
| G | `web_fetch` SSRF | Fix now, separately | Fix written and proven live (`abfa787`); deploy pending |
| H | VRAM shortfall | Measure first | — |
