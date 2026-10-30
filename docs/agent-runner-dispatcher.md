# Agent runner dispatcher (VM 178)

Implements section 6 of the agent runner queue spec (2026-10-30). The queue itself is plain GitHub
issues and labels in `DraconisCluster/SDG-RUST` and `DraconisCluster/SDG-ARMA3`; there is no CI.

| Piece | Where |
|---|---|
| Dispatcher | `scripts/files/agent_runner_dispatch.py` → VM `/usr/local/lib/agent-runner-dispatch/` |
| Units | `agent-runner-dispatch.service` (oneshot, user `runner`) + `.timer` (every 5 min, wall clock) |
| Installer | `scripts/80-agent-runner-dispatcher.sh install / enable / disable / run-once / status / uninstall` (Proxmox host, admin key) |
| Event logs | VM `~/runner-logs/<repo>-<issue>-<UTC>.jsonl` (+ `.stderr`), deleted after 30 days |
| Tests | `scripts/files/tests/test_agent_runner_dispatch.py` (fake `gh`, fake `agent-run`, real git) |

## One run

1. Takes `~/runner-logs/.dispatch.lock` (flock). Busy: exit quietly.
2. Reads the router's `/healthz`. If the loaded chat profile has no lane for scoped keys
   (only `rag-qwen3.6` and `qwen3.8` do), it claims nothing. This avoids one "lane closed" comment
   every 5 minutes while another profile is loaded.
3. Picks the **oldest** open `runner:ready` issue across both repos. An issue that also carries another
   `runner:*` label is skipped and logged, never touched.
4. Claims it: `runner:ready` → `runner:running`, plus a start comment.
5. Checks out the first line of **Branch / commit** (`branch` or `branch@sha`, backticks allowed):
   `fetch --prune`, `switch`, `pull --ff-only`, then verifies the sha. It refuses (BLOCKED) if tracked
   files are dirty, the ref is unreadable, or the sha does not match. It never discards local work.
6. Runs `agent-run ~/work/<repo> "<preamble + issue body>"` with `AGENT_RESULTS_DIR` set to
   `/var/lib/agent-results/<repo>-<issue>-<UTC>`. The agent runs in the sandbox described in
   [agent-runner.md](agent-runner.md#sandbox-hardening-2026-10-30): checkout read-only, that dir writable at
   `/results`, no GitHub credentials. The timeout is the issue's `Max wall time: N minutes|hours` from
   **Limits**: 60 min by default, 12 h at most.
7. Afterwards:
   - **Exit 75:** back to `runner:ready`, with a "lane closed" comment.
   - **The report:** the agent **prints** its report block. The dispatcher takes the last text event holding
     `task: N` and a verdict, normalises it to `### runner report` plus a yaml fence (a headerless block also
     counts), and posts it, naming the results dir.
   - **No valid block** (none at all, no valid verdict, or a timeout): it posts a BLOCKED report with the
     redacted last 50 event-log lines instead.
   - **Labels:** `runner:done`, or `runner:blocked` for BLOCKED. It also adds `needs-claude` unless the
     verdict is PASS, the deviations are `none`, and agent-run exited 0.

Only the dispatcher posts comments and edits labels; the agent has no GitHub access at all. Everything the
dispatcher posts goes through `redact()`, which removes:
- the **literal values** of `/etc/agent-runner/router.key` and `gh-token`;
- GitHub/API tokens and bearer values;
- `*KEY=`/`*TOKEN=`/`*PASSWORD=` assignments;
- 48+ hex strings. 40-hex git SHAs are kept.

## Operating

```
scripts/80-agent-runner-dispatcher.sh install                       # copy + daemon-reload (timer unchanged)
scripts/80-agent-runner-dispatcher.sh run-once DraconisCluster/SDG-ARMA3#N   # one dispatch, that issue only
scripts/80-agent-runner-dispatcher.sh enable | disable              # the 5-minute timer
scripts/80-agent-runner-dispatcher.sh status
```

To pause the queue, run `disable`. A task that is already running finishes; nothing new is claimed.

GitHub's label filter (`gh issue list -l runner:ready`) can lag a few seconds behind a label edit. A
`run-once` right after relabelling may find nothing; the next tick picks the issue up.

The agent cannot write into the clone (it is mounted read-only). Its artifacts go to the per-task results
dir, which the report names. Delete old ones by hand for now; there is no rotation yet.

**Self-test, 2026-10-30:** `DraconisCluster/SDG-ARMA3#2` (`kind:check`, `git log -1`) gave PASS in about
70 s on qwen3.6. The first attempt exposed a headerless report (fixed: headerless is accepted, and the
preamble insists on the heading).
