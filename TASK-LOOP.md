# Standard Task Loop

Use this protocol for any multi-step plan: tickets in an issue, a PLAN.md
or PHASE-N-TASKS.md file, or "work through these one by one" type asks.
Follow the loop below for EACH task in order; do not start the next task
until the current one is verifiably complete.

General operating behavior (reading before writing, tool hygiene, git
safety, scope discipline) is governed by RULES.md. This file covers
execution procedure.

The **Verification Discipline** section below applies BOTH inside the
loop AND to one-shot code-generation requests outside the loop. Do not
skip it just because a request looks like "write me a quick script."

## When This Loop Applies

Active when the user says any of:

- "standard task loop"
- "execute ... using the loop"
- "work through ... task by task"
- Any phrase clearly asking for ordered execution of a multi-step plan.

For one-off questions, analysis, or single-edit requests, do not run
the loop's Baseline / Implement / Verify steps. Answer directly. But if
the single request involves generating executable code, **Verification
Discipline still applies** — see below.

## Verification Discipline

Applies to ANY task that produces executable code: shell scripts, IaC
templates, SDK calls, API requests, queries, regex patterns, anything
that will run against a real system.

### What to verify

Before writing code, confirm against authoritative documentation:

- **Command names and flags** — the model often conflates similar
  subcommands (a "list" vs a "get" form) or invents flag spellings
  that read plausibly.
- **Property and option names** — fabricated property/option names
  look real but trigger errors like "bad property" or "unknown option"
  at runtime, or worse, silently misbehave.
- **API method names and namespaces** — SDK and REST methods vary
  across versions; training-data recall is often outdated. Method
  names that sound like they should exist often don't.
- **Response field names** — when parsing JSON responses, confirm each
  field path against a sample response or schema. Don't infer field
  names from method names ("get_user" doesn't imply a `.user` field).
- **Argument shapes** — empty string is not an empty array; null is
  not a missing key; nested objects differ from flat key-value lists.
  Wrong argument shapes often produce silent empty results rather
  than loud errors.
- **Subcommand and module structure** — `tool subcommand-a` vs
  `tool subcommand-b`, import paths in language packages, namespace
  separators in API methods.

This applies to any tool where surface vocabulary matters: CLIs with
many subcommands and flags, REST/RPC SDKs, package managers, database
clients, infrastructure-as-code DSLs, file format tools.

"I'm pretty sure" is not verification. If you didn't consult a specific
doc page, schema, or sample response in this session, the syntax is
unverified.

### How to verify (budget discipline)

For each non-trivial command, method, or field, follow this sequence:

1. **One documentation search** — use whatever doc tool is available
   (MCP RAG, library docs index, web search). Keep the result set
   small (a handful of chunks, not dozens). A small chunk of correct
   signal beats many of noise.
2. **If the first search returns nothing relevant: one targeted fetch**
   of the canonical vendor docs URL.
3. **If still unverified after step 2: STOP that command.** Either drop
   it from the design, mark it UNVERIFIED in the output and warn the
   user explicitly, or ask the user for guidance.

Three retrievals per command is the hard ceiling. Do not retry the
same tool with reworded variations — that's how verification eats
several times its budget.

### Context-cost discipline (mandatory)

Verification cost can blow the context window before code is written:

- After fetching a doc page, **extract only the relevant syntax line(s)**
  via `grep` / `findstr` / `sed`. Never paste the full page back into
  your response. Reference docs are commonly 30-80K tokens; a single
  full fetch can exhaust your budget.
- If any single retrieval returns more than ~15% of available context
  worth of text, summarize it immediately. Don't let large blobs
  accumulate.
- The verification table is the deliverable, not the underlying doc
  text. Each row cites: command | source URL | one-line syntax fragment
  that confirmed it.
- If verification has consumed more than ~10-15% of your context budget
  before any code is drafted, STOP. Tell the user the task needs to be
  split into a verification pass and a code-generation pass.

### Verification output format

Before code-generation begins, produce a table with EXACTLY these
columns. The `Quote / line` column is mandatory — it carries the
evidence and is what distinguishes verification from theater.

| # | Claim | Source | Quote / line |
|---|---|---|---|
| 1 | `<tool>` binary exists | man page / vendor docs URL | "verbatim fragment from source" |
| 2 | `<api.method.name>` is a real method | API reference URL | "verbatim line proving it exists" |
| 3 | Argument shape: `<tool> ... '[]'` | example in docs / curl smoke test | `midclt call pool.query '[]' '{}'` |
| 4 | Response field `.actual_field_name` | sample response / schema | line where the field appears in real JSON |
| 5 | `<unverifiable claim>` | UNVERIFIED | (must be dropped or flagged) |

The quote must be copied (or closely paraphrased) from a source OUTSIDE
the model's own assertion. Restating the claim ("this command exists
and takes these arguments") does not count as evidence — the quote has
to be something the user could spot-check by clicking the source link.

**Claim granularity — each row is a single fact.**

Do not lump compound claims into one row. A command like
`midclt call pool.dataset.query '[]'` is at MINIMUM three claims:

1. `midclt` is the correct binary name (vs `midclient`, `midcli`, …).
2. `pool.dataset.query` is a real method (vs `pool.datasets.query`,
   `dataset.query`, …).
3. The argument is a JSON-shaped string `'[]'` (vs empty string `''`,
   bare brackets `[]`, kwargs `{"filters":[]}`, …).

Each field referenced in parsing logic is its own claim: `.sync` vs
`.synced`, `.compression` vs `.compression_algo`. If the script
extracts ten fields, that is ten field-name rows — unless one sample
response confirms them all, in which case one row citing that response
suffices.

If any row is UNVERIFIED:

- Drop the dependency on the unverified claim, OR
- Mark it `UNVERIFIED` in BOTH the table AND a code comment, AND warn
  the user in the response that the script may fail at that point, OR
- Ask the user to provide the missing information.

**Two anti-patterns that explicitly fail this section:**

1. **Single-column tables.** A table whose only column lists command
   names (no Source, no Quote) is verification theater. The model has
   asserted verification without producing evidence.
2. **Restating the claim as the quote.** If the Quote column reads
   "this command exists and takes these arguments," that is the claim
   reworded — not a quote. The quote must come from outside the model.

These anti-patterns apply uniformly across domains: CLI scripts, SDK
calls, SQL, IaC, config files, library API usage. Anywhere the model
names *things that exist*, the table format above is the deliverable
that proves the names are real.

## The Loop

### 1. Baseline

Run the project's test suite before touching code.

- Check AGENTS.md first, then package.json scripts, then pyproject.toml,
  Makefile, etc. If the test command isn't discoverable, ask before
  proceeding.
- Show real test output, not a summary.
- Tests pass → proceed to step 2.
- Tests fail → STOP. Report which tests broke. A dirty baseline means
  task signals can't be trusted.

### 2. Plan

Write out a brief plan covering:

- **What you're about to change** — specific components, functions, files.
- **Your write plan** — how many edit tool calls, what goes in each.
  State the count explicitly.
- **Which tests you expect to be affected** — new passes, previously-
  failing tests that should now pass, regressions you're watching for.
- **Verification table** (if generating executable code) — see
  Verification Discipline above. List each non-trivial command/method/
  field with its source.

**Write plan requirements** (see also RULES.md "Output Shape for Code
Edits"):

- Each new component, function, class, or top-level definition: ONE
  dedicated edit tool call.
- Each edit: at most ~200 lines of output.
- If the task involves N independent units, your tool-call count MUST
  equal N. Do not plan one call covering multiple units.
- Do not rationalize bundling with "these are closely related," "they
  belong together," or "it's more efficient." Those have repeatedly
  caused SSE timeouts in this environment.

Do not begin editing until the plan AND any required verification table
are written out.

### 3. Implement

Execute the write plan one tool call at a time. Do not bundle remaining
calls into a single final edit.

If mid-execution the plan needs to change (new component, edit larger
than expected), stop, revise the plan explicitly, then continue. Do not
silently deviate from the declared plan.

### 4. Verify

Validate the change. The right validation depends on what you wrote:

- **Code with a test suite**: re-run the full suite, show actual output.
- **Shell script**: `bash -n script.sh` (syntax check), then
  `shellcheck script.sh` if available, then if safe, a dry-run with
  `-x` or `--dry-run`. Note any failures.
- **Code in a typed language**: run the type checker (`tsc --noEmit`,
  `mypy`, etc.) or at minimum a parse / compile check.
- **Code in an interpreted language**: run the linter (`ruff`,
  `eslint`, etc.) and a parse check (`python -m py_compile`, etc.).
- **One-shot snippet with no test scaffold**: walk the user through
  the expected behavior + tell them how to test on their end. State
  this explicitly; don't pretend you verified it.

Never claim "done" without actually verifying. "I think this is right"
is not verification.

### 5. Evaluate

- All checks pass → mark complete, summarize in 2-3 lines, next task.
- Any check fails → enter Troubleshooting Loop. Do not move on.

## Troubleshooting Loop

On failure, repeat up to 3 times per task:

1. **Read the full failure output.** Do not guess — read it.
2. **State a hypothesis in one sentence**: "I think X is failing
   because Y."
3. **If the failure indicates a name doesn't exist** — "bad property",
   "unknown command", "invalid method", "field not found", "no such
   option", "module not found", or similar — **that is a verification
   miss.** Re-verify the offending name against docs BEFORE fixing.
   Do NOT just try a different guess; that's how scripts accumulate
   compounding wrong names.
4. **Make a single targeted fix** based on the hypothesis.
5. **Re-run the verification step** and show output.
6. Pass → next task. Fail → repeat.

After 3 attempts, STOP using the Stop Format.

## Stop Conditions

Halt and report when any of these trigger:

- Baseline tests fail before any changes have been made.
- 3 troubleshooting attempts exhausted on a single task without passing.
- A fix requires changes outside the task's declared scope.
- Task description is ambiguous or conflicts with AGENTS.md.
- You need a permission, credential, or tool that isn't available.
- Proceeding would require a destructive operation that isn't explicitly
  sanctioned.
- **Verification budget exhausted** — doc lookups have consumed a
  meaningful fraction (>10-15%) of context with no code drafted. Task
  needs splitting into a verification pass and a code-generation pass.
- **A required command, method, or field couldn't be verified** after
  one search + one targeted fetch, and it can't be dropped without
  breaking the task.

## Stop Format

When stopping, output EXACTLY these fields, each on its own line:

- STOPPED on task: [task name / ID]
- Reason: [one sentence — which stop condition triggered]
- What was changed: [files modified, or "nothing" if baseline / verification failed]
- What's broken: [failing test/check names and exact error output]
- Hypothesis: [best guess at root cause]
- Suggested next step: [what a human should do to unblock]

Do not continue past a stop. Do not "try one more thing."

## Progress Reporting

Announce each step with these exact headers so the user can scan long
runs:

- `## Baseline` — before first test run
- `## Verifying syntax: [scope]` — when running verification lookups
- `## Planning task N: [short description]`
- `## Implementing` — before edits
- `## Verifying change` — before post-change checks
- `## Troubleshooting attempt N` — on each retry
- `## Task N complete` — only when checks pass

## Task Completion Criteria

A task is complete only when ALL of:

- Baseline checks that previously passed still pass.
- Any new checks specified in the task now pass.
- No new failures have been introduced.
- The verification table (if one was required) was produced and every
  row is confirmed.
- You have stated "Task N complete" explicitly.

Do not mark a task complete while any check is failing, even if you
believe the failure is unrelated to your change.

## Edge Cases

- **No test suite exists.** State this and ask what "verification"
  should mean: linter, type-check, build, smoke test, dry-run. Do not
  silently skip verification.
- **Task requires a long-lived process** (dev server, daemon, watcher).
  Ask the user to start it or confirm it's running. Don't start
  background processes you can't cleanly terminate.
- **Test suite is slow (over ~2 min).** State runtime up front. In the
  Troubleshooting Loop, run only the affected test file; full suite
  once before claiming complete.
- **Task depends on a stopped task.** Don't attempt it. Report blocked
  status using the Stop Format.
- **Compaction fires mid-task.** See Compaction Agent Mode below. When
  the user resumes, pick up from the declared plan.

## Compaction Agent Mode

OpenCode has a hidden compaction agent that asks you to summarize the
conversation for memory management. Its prompts are system-level and
request a rigid output format with specific section headings (Goal,
Instructions, Discoveries, Accomplished, Current State, or similar).

On an actual compaction prompt: respond with pure text only, no tool
calls. Summarize from conversation memory.

If a normal user turn just contains words like "summarize," "recap,"
or "status," that is regular work, not compaction. Continue using
tools freely. Do not refuse tool calls based on surface-level word
matching.
