# local-delegate — Claude Code offloads work to the local Qwen3.8 cluster

**Status:** draft for review (rev 2, post-adversarial-review) · **Date:** 2026-10-02
**Path:** architectural (brainstorming → spec → plan)

> Rev 2 reshapes rev 1 after four adversarial reviews (feasibility, security, architecture,
> economics). Rev 1's success criteria "no job can modify the user's tree / run outside the
> allow-list" were **proven false by execution**, so the boundary is rebuilt here. Rev 1's learning
> loop is **deferred** until delegation is measured to save tokens. Approach A (per-session worker)
> is replaced by approach B (one persistent localhost service). The redteam integration is a
> separate later track (§10).

## 1. Intent

**Outcome:** Claude Code can hand work to the cluster's Qwen3.8-27B so that (a) bulky reading and
well-specified implementation stop costing Claude tokens, (b) Claude keeps working while a local job
runs, (c) the work can use local skills/MCPs. **Before** any of that is elaborated, Phase 1 must
answer one question with data: *does delegation actually reduce Claude's token use, and for which
tasks?* Everything expensive waits behind that answer.

**User decisions (2026-10-02):**

| # | Decision | Choice |
|---|---|---|
| D1 | Task shape | Both tiers: single-shot `ask_local` + agentic jobs |
| D2 | Where agentic runs | Workstation; isolated per-job code copy; writes confined |
| D3 | Trigger | Claude decides, guided by tool descriptions + a CLAUDE.md rule |
| D4 | Trust gate | Claude reviews every result before anything lands |
| D5 | Build shape | **Approach B** — one persistent localhost MCP service (revised from A) |
| D6 | Reach of changes | Delegate-only; the user's interactive opencode setup is untouched |
| D7 | Learning loop | **Deferred** to Phase 3, gated on Phase-1 measurement |
| D8 | Isolation | **Both, staged**: capability-neutering + isolated git store in Phase 1; OS sandbox before the agentic tier is trusted with real repos |
| D9 | Redteam integration | Separate later track (§10), reuses this service |

**Phase-1 success criteria (the experiment):**
- The A/B ledger shows Claude-token cost per task for delegated vs. self-done work on ≥30 randomized
  eligible tasks. **Go/no-go:** delegation is kept only if it is ≥20% cheaper in Claude tokens on
  some clearly-defined task band; otherwise the project stops at Phase 1.
- `ask_local` keeps bulky input out of Claude's context (measured: Claude tokens for the call ≪ the
  input size).
- No delegated job modifies anything outside its isolated code copy — enforced by the boundary in
  §5, **verified by the mutation/escape tests in §7**, not asserted.

**Non-goals (Phase 1):** the learning loop (lessons, vault, canaries, promotion, overlay stats);
raising `--parallel`; a cross-session multi-user service; editing the user's interactive
`RULES.md`/`TASK-LOOP.md`/`LESSONS.md`/skills; the redteam track.

## 2. Ground truth (verified by execution 2026-10-02 unless marked)

From the feasibility probe (scripted fake upstream + one live 2-token router call) and host reads:

- **Live router:** `CHAT_CONCURRENCY=1` (`/etc/router.env`, LXC 153 — the code default of 2 is not
  what runs); chat llama-server `--parallel 1 --ctx-size 262144 --cache-reuse 1024 --cache-ram 16384`
  (read-only `ps` in LXC 151). `/healthz` returns `active_chat_profile: "qwen3.8"` and is
  unauthenticated.
- **Boundary facts (these drive §5):**
  - opencode's bash permission splits on `;`, `&&`, `|`, newline, backticks, `$()` and checks each
    part — but **does not check redirect targets or path arguments**. With `git *` allowed,
    `git -C ../other …` and `git --git-dir=…` mutated a different repo; with `echo *` allowed,
    `echo x > ../outside.txt` escaped `--dir`. **The allow-list is not a write boundary.**
  - A `git worktree` shares `.git` (config, refs, hooks, stash) with the source repo;
    `git config core.hooksPath` / `git tag` from inside it changed the source repo and the source
    `git status --porcelain` stayed empty. **A worktree is not an isolation boundary.**
  - Headless `opencode run` **auto-rejects any `ask` permission and exits 0** (silent no-op). Built-in
    defaults include `external_directory: ask`, `doom_loop: ask`, `read *.env: ask`. An explicit
    `deny` instead raises an error and the session continues.
  - opencode **reads stdin and appends it to the prompt**; left attached it hangs (exit 124). The
    binary is `node_modules/opencode-ai/bin/opencode.exe`; `opencode` on PATH is a `.CMD` shim.
  - A custom agent **can** restrict skills (`skill: {"*": deny, "delegate-*": allow}` worked) — but
    **inherits global `instructions` and global MCP servers**: `searxng_*` (an arbitrary web fetch)
    and `context7_*` (sends an auth header off-box) were present despite `webfetch: deny`.
  - Per request with tools enabled, fixed overhead is ~11K tokens (~30.6K chars of tool schemas),
    plus RULES.md+TASK-LOOP.md. Rev 1's "always-loaded ≤1.5K" was wrong.
  - `opencode run --format json` emits per-step `step_finish.part.tokens {input,output,reasoning,
    cache}` and `text` parts; **no session-total or final-message event** — the server must sum and
    concatenate. Errors arrive as `{"type":"error"}` with exit 1.
  - `git stash create` captures staged+unstaged **tracked** edits, excludes untracked, prints `''`
    on a clean tree, and does not touch the user's tree. `merge --squash` of that back into a dirty
    tree **fails**; a job's result must be applied as a **patch** (`git diff <base>..<result>`),
    not merged.
- **Memory vault** (live, v0.4.0, `all-MiniLM-L6-v2`): `/api/search` filters by `spaces[]` only —
  **no metadata filter**. `remember` returns a `chunk_id` that `forget` accepts, but a long entry
  splits into several chunks under one id (**UNVERIFIED**; would complicate any id-based sync — a
  Phase-3 concern only).
- **Open conflict to resolve in Phase 1:** architecture review reads `router-app.py:1770-1795` as
  releasing `chat_sem` *before* the stream body runs (so `CHAT_CONCURRENCY` would not serialize
  opencode streams); feasibility read it as serializing. **Resolve with a direct two-concurrent-
  stream test before relying on either.**
- **Model quality:** qwen3.8 ~60% on hidden-test agentic coding; expect ~40% of agentic results to
  need Claude's correction. `qwen3.8-think` for code, `qwen3.8-nothink`/`rag-qwen3.8` for summaries.

## 3. Scope: Phase 1 only

Phase 1 builds the smallest thing that can answer the go/no-go question and is safe to run.

**In:** the persistent service; `ask_local`; agentic `submit`/`result`/`list_jobs`/`record_review`;
the capability-neutered agent + isolated git store (§5, stage 1); the A/B token ledger (§6);
background-Bash completion; the CLAUDE.md eligibility rule; `delegate-review` skill.

**Out (later phases, each its own spec):** OS sandbox (Phase 2, §5 stage 2 — before the agentic
tier is trusted with real/sensitive repos); the learning loop (Phase 3); redteam (§10).

## 4. Architecture

```
Claude Code ──HTTP+bearer (127.0.0.1)──▶ local-delegate service (persistent, logon-started)
  │                                        ├─ GPU lease (one holder: a job OR an ask_local call)
  │                                        ├─ ask_local ───▶ router /v1/chat/completions
  │                                        ├─ jobs: single worker, ULID ids, state on disk
  │                                        ├─ isolated git store per job (§5)
  │                                        ├─ opencode runner (detached stdin, direct .exe, Job Object)
  │                                        └─ ledger.jsonl (single writer = the service)
  └─ background Bash: `local-delegate wait <id>` ──▶ harness notifies Claude on exit
```

**Why approach B:** architecture review showed rev 1's per-session worker makes N Claude sessions
into N workers fighting one GPU slot, with N writers of one ledger and cross-session id collisions.
A single persistent service (bound `127.0.0.1`, bearer token, started at logon) is the only GPU
lease holder and the only ledger writer, and jobs survive a session ending. Claude Code speaks HTTP
MCP natively.

**Components** (`scripts/delegate/`, small modules, deps injected for tests):
`service.py` (HTTP MCP surface + bearer auth), `config.py`, `lease.py` (cross-process GPU lease),
`router_client.py`, `jobs.py` (queue, one worker, ULID, owner-PID liveness), `gitstore.py` (isolated
copy + patch extraction), `runner.py` (spawn opencode), `checks.py`, `ledger.py`, `cli.py`
(`wait`, `reap`). Overlay in `clients/opencode-delegate/` (agent def + `allowlist.toml`), pointed at
by env at spawn — **not** copied into `~/.config/opencode` (avoids the repo-vs-live drift that has
bitten this cluster before).

**Tools:** `ask_local(prompt, content?|files?, mode)`; `submit_task(task, repo, base_ref, checks,
task_type, allow_web=false, timeout_s)`; `result(job_id)`; `list_jobs(status?)`;
`record_review(job_id, verdict, fix_lines, cause)`. No `wait` tool (it would block the turn); waiting
is the background CLI. No `stats`/lessons tools in Phase 1.

## 5. The boundary (D8 staged)

**Stage 1 — Phase 1 (capability-neutering + isolated git store):**
- **Isolated git store, not a worktree.** The service prepares each job as an independent copy with
  its own fresh `.git` (export of `base_ref` via `git archive` into a dir + `git init`, or a plain
  local clone), under `%LOCALAPPDATA%\local-delegate\jobs\<ulid>`. The job cannot reach the user's
  `.git`. The result is extracted as a patch (`git diff`), never merged from a shared ref.
  `base_ref="WORKTREE"` exports the `git stash create` snapshot (handle the clean-tree `''` case;
  include untracked via a temp-index `git add -A` + `write-tree`).
- **Capabilities neutered in the agent def:** every permission explicit (`edit: allow` only in-dir,
  everything else `deny` — never `ask`, which silently no-ops); **no `git` writes and no interpreters
  in the agent's bash allow-list** (only fixed argv forms for inspection); `searxng_*`/`context7_*`
  and all non-essential MCPs **denied unless `allow_web`**; the `docs`/memory MCPs the agent needs
  are added explicitly in the overlay rather than inherited.
- **Server-side, not agent-side:** the service runs `checks` itself as argv lists (not via shell) and
  computes the diff; it does not trust the agent's claims. Checks run with a **scrubbed env** (no
  `LOCAL_DELEGATE_ROUTER_TOKEN`, no SSH/GH tokens) and `python -I -p no:cacheprovider` so a planted
  `conftest.py`/`sitecustomize.py` does not execute with the user's rights/secrets.
- **Spawn hygiene:** `stdin=DEVNULL`, spawn the `.exe` directly, wrap in a Windows **Job Object**
  (`KILL_ON_JOB_CLOSE`) so node children die with the job.
- **Result gate before Claude sees it:** reject diffs containing gitlinks/submodules, symlinks, mode
  changes, binaries, or `.git*`/CI/hook paths; flag diffs touching dependency/CI/config files for
  mandatory attention.

**Stage 2 — before the agentic tier is trusted with real/sensitive repos (Phase 2 spec):** run the
job + checks as a **separate low-privilege Windows user** (or WSL/container) with no access to the
user's home, SSH keys or tokens, and restricted egress. Stage 1's software controls remain; the OS
sandbox is defense against an unknown opencode escape.

**Residual risk accepted for Phase 1:** stage-1 jobs run as the user. Mitigation during the
experiment: delegate only non-sensitive repos/tasks; the result gate + Claude review catch tampering
after the fact; nothing merges without Claude. This is acceptable *because* Phase 1 is a measurement,
not production use — stage 2 lands before that changes.

## 6. Measurement (the point of Phase 1)

- **Ledger** (`ledger.jsonl`, single writer): per task — `ulid, ts, mode, task_type, delegated(bool),
  claude_tokens, local_tokens, duration_s, verdict, fix_lines, cause`.
- **Claude's own tokens** are the metric, derived from the Claude Code transcript JSONL between
  `submit_task`/`ask_local` and `record_review` (the local model's tokens are not the question).
- **Randomized A/B:** for the first ~30 eligible tasks, the eligibility rule flips a coin between
  delegating and Claude doing it directly; the ledger records both arms. Report Claude-IET per task
  by arm and task band.
- **Eligibility (keeps the experiment in the band where a win is plausible):** `ask_local` for inputs
  ≥ ~20K tokens; agentic for output-heavy, well-specified tasks with Claude-written checks and an
  expected diff ≥ ~200 lines. Small/medium tasks are not delegated (economics showed them net-
  negative once review is counted).

## 7. Testing

- **Unit** (`scripts/delegate/tests`, added to the AGENTS.md pytest command): fakes for router,
  process runner, clock, filesystem. **Assert on recorded calls, not elapsed time** (per the repo's
  broad-except lesson). Cover: one-worker invariant + cross-process lease; ULID; owner-PID liveness
  (a job is `abandoned` only if its owner process is dead, never because another session started);
  single-writer ledger; alias selection; profile guard re-checked at dequeue; restart/`profile_changed`
  classification; size caps; patch extraction incl. untracked and clean-tree cases.
- **Boundary/escape tests (must fail before a fix, pass after):** the exact escapes the probe found —
  `echo > ../x`, `git -C ../other`, `git --git-dir=`, `core.hooksPath` from the job dir — must be
  blocked or impossible in the isolated store; the scrubbed-env check must not expose the router
  token; `conftest.py` must not run with user rights; the result gate must reject symlink/gitlink/
  mode/binary/`.git*` diffs; `searxng_*`/`context7_*` absent unless `allow_web`.
- **Spawn tests:** detached stdin (no hang), Job-Object kill of a child process, direct-`.exe` spawn.
- **E2E with a stand-in opencode** (edits a file, emits JSON events, exits 0/non-zero/hangs, spawns a
  child) through submit → isolated store → run → server-side checks → result gate → result →
  review. No GPU.
- **Live (`-m live`, opt-in):** one `ask_local` round trip; one tiny real agentic job; **the
  two-concurrent-stream router test** that resolves the §2 `chat_sem` conflict.
- **Mutation checks** on the lease, one-worker invariant and result gate via a matched Edit (never
  `git checkout`, per the repo lesson), reverted after confirming a test fails. The code-review
  subagent **runs** the tests rather than reading them, and is told this spec's own examples may be
  wrong.

## 8. Failure handling

| Situation | Behaviour |
|---|---|
| Another profile loaded (coder/redteam) | Guard re-checked at dequeue; fail fast naming the active profile. Never auto-swap (would cut off another workload). |
| Profile swap / redteam-mode / 04:00–16:00 restart mid-job | `ask_local` retries once; agentic job → `profile_changed` or `upstream_restart`; resubmittable. |
| Router unreachable / opencode exit 1 / timeout / `ask`-autoreject | Job `failed` with stderr tail + event-log path and reason (incl. `permission-blocked` on an "auto-rejecting" line). No retry loops. |
| Session ends / `/clear` / sleep | Jobs persist in the service; `list_jobs` recovers ids; a job whose owner process is gone and that never completed → `abandoned`; a job that woke from sleep past its deadline → `interrupted`. |
| `ask_local` while a job holds the lease | Fail fast with queue position (don't evict the job's KV cache). |
| Secrets | Router token from env `LOCAL_DELEGATE_ROUTER_TOKEN`; scrubbed from job env, prompts, logs. |

## 9. Rollout

1. Resolve the §2 `chat_sem` conflict (two-stream test); fold the answer in.
2. Service skeleton: HTTP+bearer, lease, `ask_local`, ledger. Register in Claude Code; live smoke.
3. Isolated git store + runner + server-side checks + result gate; stand-in E2E; escape tests.
4. `submit`/`result`/`list_jobs`/`record_review`; background `wait`/`reap` CLI; CLAUDE.md eligibility
   rule; `delegate-review` skill.
5. Run the randomized A/B for ≥30 tasks. **Go/no-go.**

## 10. Redteam integration — separate later track (summary only)

User goal: integrate Claude + the cluster with the RedteamAgent (LXC 157, qwen3.8-redteam) for
authorized pentest + hardening of the Space Engineers colo services. **Decision taken:** Claude
receives **findings & metadata only** — a redaction gate on 157 (the trusted side) strips raw
payloads/loot/PII before anything crosses to the cloud. **Shape:** Claude plans + triages + writes
hardening (verified on the SE QA VM 170 for crash-class findings, or a re-engagement for infra);
qwen3.8-redteam executes locally inside the egress lock; trust is one-directional (Claude → an MCP →
`157:18000`; 157 never calls out). **Prerequisites:** authenticate the `157:18000` orchestrator
(currently LAN-open, no auth); make the GPU arbitration mode-aware so engagements and delegate jobs
don't fight the slot; test the scrubber with seeded fake secrets. **Sequencing:** builds on the
Phase-1/2 service + boundary; gets its own spec after Phase-1 go/no-go.

## 11. Open items carried into planning

- The §2 `chat_sem` behaviour (test, don't assume).
- Whether a per-agent opt-out of global `instructions` exists, or the overlay must suppress them
  another way (UNVERIFIED).
- Long-entry vault chunking vs. `forget`-by-id (Phase-3 only; UNVERIFIED).
