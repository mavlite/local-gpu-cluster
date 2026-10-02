# local-delegate Service

A localhost MCP server that queues agentic coding tasks and ask-local reads on a local LLM. Claude delegates work via `submit_task` and `ask_local` tools; a human reviews results via the `delegate-review` skill before merge.

## Environment Variables

**Required:**

- `LOCAL_DELEGATE_BEARER_TOKEN` — Bearer token for MCP client auth (e.g., Claude Code)
- `LOCAL_DELEGATE_ROUTER_TOKEN` — Bearer token for router calls to the local LLM

**Optional:**

- `LOCAL_DELEGATE_ALLOWED_ROOTS` — Semicolon-separated repo paths (Windows); empty = allow any repo in `LOCAL_DELEGATE_JOBS_DIR`
  - Example: `C:\Users\willi\Documents\GitHub\local-gpu-cluster;C:\work\other-repo`
- `LOCAL_DELEGATE_HOST` — Bind address (default: `127.0.0.1`)
- `LOCAL_DELEGATE_PORT` — Bind port (default: `3006`)
- `LOCAL_DELEGATE_ROUTER_URL` — Router base URL (default: `http://192.168.6.153:8000/v1`)
- `LOCAL_DELEGATE_JOBS_DIR` — Work directory for queued jobs (default: `%LOCALAPPDATA%\local-delegate\jobs`)
- `LOCAL_DELEGATE_LEDGER` — A/B measurement ledger path (default: `%LOCALAPPDATA%\local-delegate\ledger.jsonl`)
- `LOCAL_DELEGATE_LEASE` — GPU lease lock file (default: `%LOCALAPPDATA%\local-delegate\gpu.lock`)
- `LOCAL_DELEGATE_LOG` — Append service output to this file (default: unset on a console;
  `%LOCALAPPDATA%\local-delegate\service.log` under `pythonw`, i.e. the logon task)
- `LOCAL_DELEGATE_OPENCODE_EXE` — Path to `opencode.exe` binary for re-running checks in isolation (optional)
- `LOCAL_DELEGATE_OVERLAY` — Overlay work directory (optional; for advanced isolation)
- `LOCAL_DELEGATE_JOB_TIMEOUT_S` — Default per-job opencode timeout in seconds (default: `1800` = 30 min)
- `LOCAL_DELEGATE_ASK_TIMEOUT_S` — HTTP timeout for `ask_local` router calls in seconds (default: `600`)
- `LOCAL_DELEGATE_ASK_MAX_INPUT_BYTES` — Max combined `ask_local` input size in bytes (default: `400000`)

## Starting the Service

### Manual Start

```bash
python -m scripts.delegate.service
```

This runs `uvicorn` on the Starlette app and starts the server on `127.0.0.1:3006`
(or custom `LOCAL_DELEGATE_HOST:LOCAL_DELEGATE_PORT`). Equivalently:

```bash
LOCAL_DELEGATE_BEARER_TOKEN=... LOCAL_DELEGATE_ROUTER_TOKEN=... python -m scripts.delegate.service
# which is: uvicorn.run(build_app(cfg, deps, store), host=cfg.host, port=cfg.port)
```

### At Logon (Windows Scheduled Task)

Register it once, as you and **not** elevated. The task runs `pythonw.exe` directly (no console
window), so Task Scheduler owns the service process itself:

```powershell
$pyw = Join-Path (Split-Path (Get-Command python).Source) "pythonw.exe"
$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m scripts.delegate.service" `
  -WorkingDirectory "C:\Users\willi\Documents\GitHub\local-gpu-cluster"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "local-delegate" -Action $action -Trigger $trigger `
  -Settings $settings -RunLevel Limited -Force
```

Why each setting:

- **`pythonw.exe` directly, no wrapper.** A PowerShell/cmd wrapper is what the task would own:
  `Stop-ScheduledTask` then ends only the wrapper and leaves Python orphaned on port 3006, so the
  next start fails to bind. (Verified 2026-10-02.)
- **Logging.** `pythonw` has no console, so the service appends its output to
  `%LOCALAPPDATA%\local-delegate\service.log` (override with `LOCAL_DELEGATE_LOG`). A config
  error such as a missing token is logged there too.
- **`-ExecutionTimeLimit 0`** — the default is 72 hours, after which Task Scheduler kills the
  task; a long-running service must opt out.
- **`-RunLevel Limited`** — a `127.0.0.1` service needs no admin rights; jobs run with whatever
  rights the service has, so do not elevate it.
- **No tokens in the task.** `LOCAL_DELEGATE_BEARER_TOKEN` / `LOCAL_DELEGATE_ROUTER_TOKEN` are
  read from your User-level environment, which a logon task inherits.

Older revisions of this README used `schtasks /Create ... /RL HIGHEST` with a `cd ... && python`
action: `&&` is a parser error in Windows PowerShell 5.1, `HIGHEST` elevates for no reason, and
the 72-hour limit still applied.

Check, start or read the log:

```powershell
schtasks /Query /TN "local-delegate"
Start-ScheduledTask -TaskName "local-delegate"
Get-Content "$env:LOCALAPPDATA\local-delegate\service.log" -Tail 20
```

If you start it by hand (`python -m scripts.delegate.service`) while the task is running, the
second instance fails to bind 3006 — stop the task first.

### Stopping the Service

```powershell
Stop-ScheduledTask -TaskName "local-delegate"     # stops the service (port 3006 is freed)
Disable-ScheduledTask -TaskName "local-delegate"  # and keeps it from starting at logon
```

After a restart, Claude Code marks `local-delegate` as failed if it tried to connect while the
service was down; reconnect it with `/mcp`.

## MCP Integration (Claude Code)

The service is a **persistent localhost HTTP server** (not a stdio subprocess), so the
client connects over HTTP with a bearer token — it does not spawn the server. Start it
yourself (see *Starting the Service* above), then add to `.mcp.json`:

```json
{
  "mcpServers": {
    "local-delegate": {
      "type": "http",
      "url": "http://127.0.0.1:3006/mcp",
      "headers": {
        "Authorization": "Bearer ${LOCAL_DELEGATE_BEARER_TOKEN}"
      }
    }
  }
}
```

`${LOCAL_DELEGATE_BEARER_TOKEN}` is expanded from the environment, so the token lives
in your environment (and the server's), never in the committed file. The bearer token in
the header must equal the `LOCAL_DELEGATE_BEARER_TOKEN` the server was started with.

**Start Claude Code from a fresh shell.** Claude Code expands `${LOCAL_DELEGATE_BEARER_TOKEN}`
from its *own* process environment, which it inherits from the shell that launched it. A
shell opened before the variable was set (e.g. with `setx` or the User-level environment)
does not have it — and restarting `claude` from that same shell does not pick it up. The
header then goes out as a bare `Bearer `, the server logs `POST /mcp 401 Unauthorized`, and
Claude Code reports `local-delegate (AUTH_HEADER_REJECTED)`. Open a new terminal window, or
import the variable into the current shell before launching:

```powershell
$env:LOCAL_DELEGATE_BEARER_TOKEN = [Environment]::GetEnvironmentVariable("LOCAL_DELEGATE_BEARER_TOKEN", "User")
claude
```

The service exposes two tool categories:

- **ask_local** — Bulky reads (≥ ~20K tokens) on the local LLM; blocks until result
- **submit_task, result, list_jobs, record_review, record_direct** — Agentic task queue
  and A/B measurement; non-blocking

## Checking Logs

Results and reviews are recorded in the ledger (default: `%LOCALAPPDATA%\local-delegate\ledger.jsonl`):

```bash
# Print recent entries
Get-Content "$env:LOCALAPPDATA\local-delegate\ledger.jsonl" | Select-Object -Last 20
```

Job state is stored in `LOCAL_DELEGATE_JOBS_DIR`. For a job with id `<id>`:

- `<id>.json` — the single JSON state file: spec, status, diff, checks, tokens, gate
  report, timing. There is no separate `spec.json`/`result.json`.
- `<id>/work/` — the agent's working copy (tracked files only, **no `.git`**); the
  agent is confined here.
- `<id>/gitdir/` — the isolated git metadata (object store, refs) kept outside `work/`
  so the agent cannot reach it; diffs and the result patch are produced from here.

`record_review` removes the `<id>/` directory (work + gitdir) after a verdict is logged;
the `<id>.json` state file and the measurement row in the ledger remain.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "GPU busy" on ask_local | A job holds the lease; wait for it to finish or call `list_jobs()` to check status |
| Task times out | Increase `LOCAL_DELEGATE_JOB_TIMEOUT_S` (default: 1800s = 30 min) |
| Diff is empty | Task did not make changes; check the summary and error fields in `result()` |
| Result gate rejects diff | Read `gate_reasons`; common: protected paths, binaries, symlinks; fix and re-queue |
| Checks failed | Task output does not match expected; review `result.checks` and re-run manually to compare |

## Measuring the Phase-1 A/B experiment

The go/no-go is whether delegating saves Claude tokens. It accrues over real use:

- For the first ~30 eligible tasks, flip a coin (see the `delegate-review` skill):
  - **Heads** — delegate: run the task, then `record_review(job_id, verdict, claude_tokens, ...)` with the Claude tokens the task cost you (from the transcript).
  - **Tails** — do it yourself, then `record_direct(task_type, claude_tokens, note)` with what it cost you.
- Read the running tally any time:

  ```
  python -m scripts.delegate.cli report
  ```

  It prints mean Claude tokens/task for each arm, the savings %, and whether the >=20% gate is met. Keep is only if delegation is >=20% cheaper on some clear task band; otherwise stop at Phase 1.
