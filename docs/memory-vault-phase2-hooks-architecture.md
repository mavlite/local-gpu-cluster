# Memory Vault Phase 2 — Automatic Memory via Client Hooks (Design Spec)

- **Date:** 2026-06-18
- **Status:** Approved (design) — pending implementation plan
- **Builds on:** [`memory-vault-architecture.md`](./memory-vault-architecture.md) (phase 1). Memory Vault is live at `http://192.168.6.223:8000` (DHCP), MCP-SSE bridge at `:3005`, per-repo `?space=` convention.

## 1. Goal

Make memory **rehydrate automatically** across new sessions, resumes, and context compactions, and make **saving durable decisions** happen with minimal manual effort — for both Claude Code and OpenCode on the workstation. Phase 1 left `remember`/`recall` as purely model-/user-invoked; phase 2 wires client lifecycle hooks so the agent starts each session already primed and is actively prompted to checkpoint before losing context.

## 2. Platform reality (verified against Claude Code 2.x hook docs)

These constraints shaped the design and must not be designed around:

- **`SessionStart`** fires on `startup` | `resume` | `clear` | **`compact`** (post-compaction recovery). It can inject context via `hookSpecificOutput.additionalContext` (wrapped as a system reminder before the first turn). Input on stdin includes `session_id`, `transcript_path`, `cwd`, `source`.
- **`PreCompact`** can only **block or log**. It **cannot** inspect/modify the compacted payload, inject content that survives compaction, influence the summary, or trigger a model tool call. → It is **not** used for saving.
- **`UserPromptSubmit`** can inject `additionalContext` for the current turn.
- Command and HTTP hooks can call external REST APIs. Hooks cannot force a model tool call (only inject context the model may act on).
- MCP tool identifiers in permissions: `mcp__memory__recall`, `mcp__memory__remember`, etc.

**Consequence:** rehydration (recall) is fully automatic and reliable (via `SessionStart`, including the post-`compact` source). Saving cannot be force-triggered by a hook; it is realized as a **standing instruction** injected at session start plus a **context watchdog** that nudges a checkpoint as the transcript grows.

## 3. Decisions (locked)

| Decision | Choice |
| --- | --- |
| Auto-save mechanism | **Model-written, instruction-driven** (Approach A: SessionStart standing instruction + `UserPromptSubmit` context watchdog) |
| Auto-recall mechanism | **Recent-memories primer + usage nudge** injected at session start (and post-compact) |
| Scope | **User-global** (`~/.claude/`, `~/.config/opencode/`) — every repo, each its own space |
| Clients / order | **Both**, but **Claude Code first**, then OpenCode |
| Space derivation | **git top-level basename** (env-overridable `MEMVAULT_SPACE`); matches the per-repo `?space=` convention |
| Recall source | `GET /api/chunks` for recent N (chronological), **not** `/api/search` (no query exists at startup) |
| Token handling | **Dedicated hooks token** (minted on host), in `~/.config/memory-vault/hooks.env` (mode 600); never committed |
| Failure mode | **Silent, non-blocking**; short timeouts; a missing/unreachable cluster never breaks a session |
| GPU impact | **None** — recall/remember/chunks are CPU-only on Memory Vault; no model warm-up involved |

## 4. Architecture

### 4.1 Shared hook library
`clients/memory-hooks/lib.sh` — sourced by the Claude Code hook scripts:
- `mv_load_env` — source `~/.config/memory-vault/hooks.env` (sets `MEMVAULT_API_URL`, `MEMVAULT_HOOKS_TOKEN`); if absent, exit 0 silently (feature simply inert).
- `mv_space` — `basename "$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || echo "$cwd")"`, overridable by `MEMVAULT_SPACE`.
- `mv_curl` — `curl` wrapper with `-m 3`, bearer header from env, failures swallowed.

### 4.2 Claude Code (phase 2a)
User-global `~/.claude/settings.json` hooks + `~/.claude/hooks/` scripts (versioned in `clients/memory-hooks/`):

- **`memory-session-start.sh`** (`SessionStart`, matcher `startup|resume|compact`):
  1. resolve space; `GET /api/chunks?space=<space>&limit=N` (N≈6, env `MEMVAULT_PRIMER_COUNT`); take most-recent chunks.
  2. emit JSON `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"<primer>\n<standing-instruction>"}}`.
  - **Primer:** a short bulleted list of recent memories for the space (truncated per item).
  - **Standing instruction:** "You have a project memory (MCP `memory` tools). Call `recall` when starting a task or when you need prior context. Proactively call `remember` to checkpoint durable decisions, file/why notes, and current state — especially as the conversation grows — so context survives compaction."
  - On empty/unreachable: inject only the standing instruction (or nothing), exit 0.

- **`memory-context-watchdog.sh`** (`UserPromptSubmit`):
  - read `transcript_path`; if its size ≥ `MEMVAULT_WATCHDOG_BYTES` (default ~300 KB, tunable) **and** a per-session sentinel (`$TMPDIR/mv-watchdog-<session_id>`) is absent: create the sentinel and inject `additionalContext` = "Context is getting large. If there are durable decisions or state not yet saved, call `remember` now before the next compaction." Otherwise exit 0 with no output.
  - Heuristic proxy for "near auto-compaction" (Claude Code compacts on token budget; transcript bytes is a cheap, monotonic approximation). One nudge per session.

- **Permissions:** add `mcp__memory__recall`, `mcp__memory__remember` (and `forget`/`memory_status`) to the user `permissions.allow` so the model's calls don't prompt.

### 4.3 OpenCode (phase 2b — after 2a is validated)
User-global plugin under `~/.config/opencode/` (TS), versioned in `clients/opencode-memory/`:
- `session.created` → inject recent-memories primer + standing instruction.
- `experimental.session.compacting` → add a "preserve durable decisions; you may call the memory tool" note to the compaction context.
- `session.compacted` → re-inject the primer.
- **Exact injection API is pinned at implementation** (confirmed against the installed OpenCode version, the same way phase 1 pinned the bridge's REST contract live). Fallback if injection is limited: a `session.created` instruction-only nudge.

## 5. Data flow
- **Session start / resume / post-compact:** client → SessionStart hook → `mv_curl GET /api/chunks` (space) → format primer → `additionalContext` → model sees prior memories + instruction.
- **During work:** model calls `recall`/`remember` via the MCP bridge (phase 1 path) per the standing instruction.
- **Context grows:** `UserPromptSubmit` watchdog → one-time "checkpoint now" nudge → model calls `remember`.

## 6. Security & robustness
- Dedicated hooks token (`memory-vault token create workstation-hooks` on the host), stored in `~/.config/memory-vault/hooks.env` mode 600, never committed. `hooks.env.example` (no secret) is versioned.
- All hook REST calls: `curl -m 3`, output discarded on error, scripts `exit 0` on any failure → never block or slow a session noticeably.
- LAN-only endpoint; off-LAN repos simply get inert hooks (calls fail silently).
- No GPU/fan impact (CPU-only endpoints).

## 7. Repository layout (new)
| Path | Purpose |
| --- | --- |
| `clients/memory-hooks/lib.sh` | shared bash helpers |
| `clients/memory-hooks/memory-session-start.sh` | SessionStart recall+instruction |
| `clients/memory-hooks/memory-context-watchdog.sh` | UserPromptSubmit save nudge |
| `clients/memory-hooks/settings.snippet.json` | hooks + permissions block to merge into `~/.claude/settings.json` |
| `clients/memory-hooks/hooks.env.example` | env template (URL + token placeholder) |
| `clients/memory-hooks/install.sh` | copies scripts to `~/.claude/hooks/`, merges settings snippet, scaffolds `hooks.env` |
| `clients/opencode-memory/` | OpenCode plugin (phase 2b) |
| `docs/memory-vault-phase2-hooks.md` | install + verification doc |

## 8. Verification
- **SessionStart:** in a repo, start `claude`; confirm a system reminder appears with recent memories + the instruction (use a space pre-seeded with a known memory). With the cluster unreachable, confirm the session still starts cleanly (silent).
- **Watchdog:** set `MEMVAULT_WATCHDOG_BYTES` low, drive the transcript past it, confirm exactly one "checkpoint now" nudge.
- **End-to-end:** store a decision in session 1 (model calls `remember`), `/clear`, confirm session 2's SessionStart primer surfaces it.
- **OpenCode (2b):** session.created injects the primer; post-compaction re-injects.

## 9. Out of scope / YAGNI
- LLM-summarized saves (rejected: GPU/fan cost + latency).
- Deterministic transcript-tail saves (rejected: store noise).
- Cross-space/global memory search at startup (per-repo space only).
- Token-accurate compaction prediction (bytes heuristic suffices).

## 10. Open items pinned at implementation
- Exact `GET /api/chunks` query params (space filter, limit, ordering) — confirm against the live `/openapi.json` (captured at deploy).
- OpenCode plugin context-injection API for `session.created` / `session.compacted` — confirm against installed OpenCode `1.17.7`.
- Claude Code `additionalContext` size budget — keep the primer small (≤ ~1.5 KB) to avoid bloating every session start.
