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

To start the service automatically when you log in, create a Scheduled Task:

```powershell
$token = "your-bearer-token-here"
$routerToken = "your-router-token-here"

schtasks /Create `
  /TN "local-delegate" `
  /SC ONLOGON `
  /TR "powershell -NoProfile -Command `"cd 'C:\Users\willi\Documents\GitHub\local-gpu-cluster' && python -m scripts.delegate.service`"" `
  /RL HIGHEST `
  /F
```

**Before running the command:**

1. Replace `your-bearer-token-here` and `your-router-token-here` with actual tokens
2. Ensure the Python interpreter path is correct (or add Python to PATH)
3. Set environment variables on the task:
   - Edit the task in Task Scheduler
   - Go to **Conditions** → ensure "Power" settings allow the task to run on battery
   - Go to **Actions** → **Edit** the action
   - In the action, prepend `env:` commands or set them at the system level

Alternatively, set environment variables **before** creating the task:

```powershell
$env:LOCAL_DELEGATE_BEARER_TOKEN = "your-bearer-token"
$env:LOCAL_DELEGATE_ROUTER_TOKEN = "your-router-token"

schtasks /Create `
  /TN "local-delegate" `
  /SC ONLOGON `
  /TR "powershell -NoProfile -Command `"cd 'C:\Users\willi\Documents\GitHub\local-gpu-cluster' && python -m scripts.delegate.service`"" `
  /RL HIGHEST `
  /F
```

To verify the task was created:

```powershell
schtasks /Query /TN "local-delegate"
schtasks /Run /TN "local-delegate"  # Test run
```

### Stopping the Service

```bash
# Kill the Python process
Get-Process python | Where-Object { $_.CommandLine -like "*scripts.delegate.service*" } | Stop-Process

# Or disable the Scheduled Task
schtasks /Change /TN "local-delegate" /DISABLE
```

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
