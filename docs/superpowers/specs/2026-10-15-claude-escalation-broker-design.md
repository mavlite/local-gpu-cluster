# Claude escalation broker — design

**Date:** 2026-10-15
**Status:** approved in conversation, awaiting written-spec review
**Scope:** sub-project B of "better daytime qwen3.8 sessions". Sub-project A, a faster and better local session, gets its own design later.

## 1. Purpose

The user codes interactively during the day in opencode on local qwen3.8. When a session is stuck, the local model or the user should be able to ask Claude for help. The user's Anthropic API credit is $200/month, so every call must be:

- **necessary:** user-initiated, or model-initiated under strict rules;
- **small:** a bounded brief, never the transcript or whole files;
- **capped:** hard limits that the client cannot bypass.

**Success criteria:**

1. No path exists from opencode to Claude except through the broker.
2. Spend never exceeds the configured caps.
3. A typical call costs about $0.05 (Sonnet).
4. The user can see the spend at any time.

## 2. Decisions (from the 2026-10-15 conversation)

| Topic | Decision |
|---|---|
| Who escalates | Both: the user via a command, and the model on its own within a small allowance |
| Budget | $200/month plan credit; broker caps at **$150/month** and **$7/day** |
| What Claude sees | A compact brief only. One answer per call, no tools, no follow-up rounds |
| Where enforcement lives | The router (LXC 153), as a broker module. The Anthropic key lives only there |
| Models | Sonnet by default (`claude-sonnet-5-5`); Opus (`claude-opus-5-5`) only on explicit user request (`hard`) |
| Budget visibility | A footer on every answer, a `/claude-budget` command, and a cluster-monitor panel |

## 3. Architecture

```
opencode (workstation)                          router LXC 153                       Anthropic API
  /ask-claude [hard]  ─┐
  qwen3.8 + escalate   ├─ task → subagent ─→ POST /v1/chat/completions  ──→ escalation.py ──→ messages API
  skill (auto)        ─┘    claude | claude-auto   model=claude-ask|-hard|-auto   budget, caps,
  /claude-budget ───────────────────────────→ GET /escalation/budget            ledger, footer
```

### 3.1 Router: `scripts/files/escalation.py`

The broker is a new module beside `tavily_cache.py` and `web_fetch_guard.py`, following the Tavily pattern of a paid upstream behind the router. `router-app.py` gains only the dispatch to it and the budget endpoint.

**Aliases** are handled by the broker and never sent to llama.cpp:

| Alias | Origin | Model | Max brief | Max answer | Calls/day |
|---|---|---|---|---|---|
| `claude-ask` | user | Sonnet | 8K tokens | 2K | 15 (shared with auto) |
| `claude-auto` | model | Sonnet | 8K | 2K | 5 (counts toward the 15) |
| `claude-hard` | user | Opus | 16K | 4K | 3 |

**Access:** only the owner key (`ROUTER_API_KEY`) may call these aliases. Scoped keys are refused, whatever their allowlist says.

**Message stripping** is the biggest token saving. The broker discards:
- every client `system` message;
- every turn except the final `user` message;
- all `tools`, `tool_choice` and client sampling parameters.

The upstream request is then exactly: the broker's fixed system prompt, plus the brief as the single user message, plus the alias's `max_tokens`.

**Brief validation**, before any spend:
- The final user message must contain all seven headings of the §4 template (`## Goal`, `## Context`, `## Reproduce`, `## Exact error`, `## Code`, `## What was tried`, `## Question`), each with content. A section may be `n/a: <reason>`, but `## Goal`, `## Exact error` and `## Question` may not. Otherwise the call gets 422 `brief: missing or empty section <name>`.
- This is a structural guard only. Sufficiency is the job of the local checklist in §4, which the broker cannot judge.
- The brief size is estimated as `ceil(len(utf8_bytes)/3)` tokens, a deliberate over-estimate. Over the alias limit, the call gets 413 `brief: <n> tokens over the <limit> limit -- trim it`, and nothing is sent.

**Budget check**, before any spend:
- The worst case is `max_brief_tokens_estimate × input_price + max_tokens × output_price`.
- The call is refused with 429 and a clear reason if it would breach any of these:
  - the monthly cap;
  - the daily cap;
  - the alias's calls-per-day;
  - the shared 15 calls/day.
- Example reason: `budget: daily cap reached ($7.00); resets 00:00 <tz>`.
- The day boundary uses `ESCALATION_TZ` (an IANA name). It defaults to the router host's local timezone, and the month is the calendar month in that timezone.
- `claude-hard` has its own count of 3 per day. `claude-ask` and `claude-auto` share 15 per day, of which at most 5 may be `claude-auto`.

**The upstream call:**
- It goes to the Anthropic Messages API with `httpx`, with a 120 s timeout.
- One retry with backoff on 429, 5xx and transport errors. Never retry on other 4xx.
- Pricing uses the `usage` the API returns, priced from a table in config (USD per million input and output tokens per model). The table is verified against current Anthropic prices at build time, never hard-coded from memory.

**Answer:** the response goes back in OpenAI chat-completions shape (non-streaming is fine; streaming clients get one chunk plus `[DONE]`). A footer is appended:

```
---
Claude <model>: $0.054 this call · $1.84 today of $7.00 · $23.10 this month of $150.00 · auto 3/5 left today
```

**Ledger:**
- File: `/var/lib/llm-router/escalation-ledger.jsonl`, owned `router`, mode 0600, append-only.
- One line per attempt: `ts`, `alias`, `model`, `origin`, `input_tokens`, `output_tokens`, `cost_usd`, `brief_sha256`, `brief_tokens_est`, `outcome` (`ok`, `refused:<reason>` or `error:<class>`).
- Budgets are computed from the ledger on each request. It is small, and an in-memory sum can be cached, keyed by file size and mtime. A router restart therefore loses nothing.
- Brief text is not stored. Optionally, `ESCALATION_KEEP_BRIEFS=N` keeps the last N briefs in `/var/lib/llm-router/escalation-briefs/` (0600) for audit.

**Budget endpoint:** `GET /escalation/budget` returns JSON with:
- month and day spend and caps;
- calls used and left per alias;
- the last 10 ledger lines, without hashes;
- the reset times.

It is open to the owner key and to `METRICS_ALLOWED_IPS`, the same rule the dashboard already uses.

**Credentials:**
- The key file is `ANTHROPIC_API_KEY_FILE` (default `/etc/router-anthropic.key`, `root:router` 0640).
- It is read at use, so a rotation needs no restart.
- If the key is missing or invalid, the call fails closed with 503 `escalation: no Anthropic key configured`.
- The key is never logged, never echoed, and never included in an error text.

**Fixed system prompt** (on the broker, not editable by the client):

> You are a senior engineer helping a local coding model that is stuck. Answer the Question using only the brief. Be concise: diagnosis in a few sentences, then the minimal fix as a diff or a short snippet, plus how to verify it (the command or test to run). If the brief lacks information you need, say exactly what is missing and stop — do not guess.

The answer is short, but the brief carries full context (§4). Spend goes where it buys the fix: on a complete brief, not on a long answer.

### 3.2 opencode (workstation `~/.config/opencode/config.json`)

The config is backed up before editing (`config.json.bak-<date>-escalation`).

- **Router provider models:**
  - `claude-ask`, `claude-auto` and `claude-hard`, all with `tools: false`.
  - Each `limit.output` matches the broker's answer cap. Each `limit.context` is 16384, which is small on purpose.
- **Agents:**
  - `claude`: subagent, model `router/claude-ask`, all tools disabled. Its prompt: "Pass the brief through unchanged. Return the answer verbatim."
  - `claude-hard`: subagent, model `router/claude-hard`, set up the same way.
  - `claude-auto`: subagent, model `router/claude-auto`, set up the same way.
- **Command `/ask-claude [hard] [note]`:**
  1. qwen3.8 writes the brief from the current session using the template in §4, including the user's note, and runs the sufficiency check.
  2. It shows the brief and its estimated size.
  3. It sends the brief through the `claude` subagent, or `claude-hard` with `hard`.
  4. It applies or explains the answer.
- **Command `/claude-budget`:** prints `GET /escalation/budget` in readable form, using opencode's command shell-injection. If 1.18.x does not support injection, the command tells the model to run one `curl` through bash. This costs no Claude tokens.
- **Skill `escalate`** (in `~/.config/opencode/skills/escalate/`): the brief template, the sufficiency check, the size discipline, the auto-escalation rules in §4, and how to apply an answer.
- **RULES.md** gets two lines pointing at the skill and at the "carry on or tell the user" fallback when the broker refuses.
- **Removing the direct path:** the `anthropic` entry is removed from opencode's `auth.json`, after a backup, once the key is on the router. This leaves no path to Claude that bypasses the broker.

### 3.3 Cluster monitor

A budget panel (month-to-date, today, calls left, last 10 calls) reads `GET /escalation/budget`. It is lowest priority and may be deferred if the monitor code makes it costly.

## 4. Behaviour rules (the `escalate` skill)

**The principle (user requirement, 2026-10-15): a brief must be sufficient before it is small.**
- An escalation succeeds only if Claude can get us past the blocker from the brief alone. A cheap brief that leaves Claude guessing is the most expensive outcome: it wastes the call and a follow-up.
- The size caps are a ceiling, not a target. The Sonnet cap of 8K tokens is roughly 600 lines, which is room for everything below.
- Brevity comes from leaving out *irrelevant* material (unrelated files, passing tests, chat history), never from cutting relevant context.

**Brief template.** Every section is required. A section that truly does not apply says `n/a: <why>`.

```
## Goal            1-3 sentences: what we are trying to achieve and why
## Context         repo purpose in one line; language, framework and the versions that matter;
                   OS/runtime; any constraint on the fix (can't change API X, must stay on lib Y)
## Reproduce       the exact command(s) that show the problem; expected vs actual behaviour
## Exact error     verbatim: the full relevant traceback / compiler / test output
                   (first cause through to the failing line, not just the last line)
## Code            every excerpt needed to reason about the failure, each headed path:line-range:
                   the failing function, its callers/callees on the failing path, the relevant
                   types/config, and the test that fails
## What was tried  bullets "attempt -> what happened", including hypotheses ruled out and why
## Question        one specific question, and what a useful answer looks like
                   (e.g. "the root cause and a minimal diff")
```

**Sufficiency check before sending.** qwen3.8 runs this checklist and fixes every "no":
1. Could a senior engineer who has never seen this repo reproduce or reason about the problem from this brief alone?
2. Is the full causal chain of the error present: the code on the failing path, not just the line that throws?
3. Are versions and constraints stated wherever the fix could depend on them?
4. Is every attempted fix listed with its result, so Claude won't suggest it again?
5. Is the question answerable, and does it say what form of answer we need?

For `/ask-claude`, the brief and its checklist answers are shown to the user before sending. The user can add context or say "send". For auto-escalation, the checklist must pass. A brief the model can't make sufficient within the cap means it should ask the user rather than send a weak brief.

**Discipline:**
- Never paste the transcript or unrelated files. Whole files are allowed only when the file is short and wholly relevant.
- Prefer the code on the failing path over code merely nearby.
- Keep secrets out of the brief. Redact anything that looks like a key or password, which the broker cannot do reliably.

**Auto-escalation** is allowed only when one of these holds:
1. The same error survives 3 genuinely different fix attempts.
2. The session is looping (the same edit-and-test cycle repeating).
3. Required knowledge was not found after trying the repo, context7 and searxng.

Auto-escalation is never allowed:
- for something a search or reading the code would answer;
- on a first failure;
- for the same problem twice. The exception: Claude's previous answer named specific missing information. Then one follow-up carrying exactly that information is allowed.

**On a broker refusal** (budget, caps): stop escalating, carry on without Claude where possible, otherwise tell the user it is stuck and what it tried. Never work around a refusal.

## 5. Testing

**Router unit tests** (`scripts/files/tests/test_escalation.py`) against a fake Anthropic upstream that records requests:
- Stripping: the upstream request carries only the fixed system prompt plus the final user message. No client system messages, earlier turns, tools or sampling.
- Validation: missing sections give 422; an oversized brief gives 413 and nothing upstream.
- Budget: the worst-case check, monthly and daily caps, the per-alias and shared call counts, and the day rollover in the configured timezone.
- Access: scoped keys get 403, even with the aliases in their allowlist. The owner key is allowed.
- Ledger: one line per attempt, correct cost from `usage` and the price table, recomputed after a "restart" (a new broker instance on the same file), file mode 0600.
- Footer content. The budget endpoint's shape and access rule.
- Retries: one retry on 5xx and 429, none on 400. Transport errors are recorded as `error:*` with a cost of 0 unless the API reported usage.
- The key never appears in logs, errors or the ledger (assert on captured logs with a sentinel key value).

**Live smoke test** (needs the user's go-ahead): one `claude-ask` call with a tiny brief, about $0.01. Compare the ledger's cost with the console's usage for that call, and check the footer and `/claude-budget`.

**opencode check:** run `/ask-claude` on a staged stuck problem, check that the brief follows the template and stays inside the cap, and check that the answer returns with the footer. Separately, confirm that removing `auth.json`'s anthropic entry leaves no direct Claude model selectable.

## 6. Rollout and rollback

1. Deploy the router with the existing router-deploy procedure: a backup of `/opt/llm-router`, a rollback script, and health checked after the restart. The broker is inert until the key file exists.
2. Move the Anthropic key from `auth.json` to `/etc/router-anthropic.key` over stdin from the workstation. It is never printed or put on argv. Back up `auth.json`, then remove its anthropic entry.
3. Back up and edit the opencode config. Add the skill and the RULES lines.
4. **The user** sets a monthly spend limit just under $200 in the Anthropic console, as a backstop the broker cannot provide.
5. Run the live smoke test with the user's OK.

**Rollback:**
- Router: run the rollback script. The `claude-*` aliases disappear.
- opencode: restore the `.bak` config.
- The key can be restored to `auth.json` from its backup if the user wants direct access back.

## 7. Out of scope

- Sub-project A: speed and quality of the local qwen3.8 session.
- Multi-round follow-ups or giving Claude tool access (rejected for token cost).
- Any CPU-tier involvement (retired 2026-10-15).
- Using the escalation path for background or batch work. It is for interactive sessions only.
