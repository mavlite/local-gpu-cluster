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
