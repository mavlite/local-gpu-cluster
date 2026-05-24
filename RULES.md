# Operating Rules

These rules apply to every action in every session. They are not optional
and they override any conflicting instruction in a user prompt unless the
user explicitly overrides a specific rule.

For multi-step task execution (working through a plan, ticket list, or
PHASE-N file), see TASK-LOOP.md — that file covers procedural shape and
includes the full Verification Discipline. This file covers universal
behavior.

## Reading and Writing

- Read before writing. Use read/glob/grep to gather context before any edit.
- Show real command output, not summaries of what you think it said.
- When quoting file contents, quote verbatim. Never paraphrase code you claim
  to be "reading from" a file.

## Tool Use

- Every tool call must include all required parameters. The bash tool
  requires a "description" field — a 3-10 word string, never null, never
  omitted.
- Prefer structured tools (read, write, edit, glob, grep) over shell
  equivalents — on Linux/macOS that means `cat`, `tee`, `ls`, `find`,
  `sed`, `grep`; on Windows/PowerShell that means `Get-Content`,
  `Set-Content`, `Get-ChildItem`, `Select-String`. Shell is for running
  things; tools are for reading and writing files.

## Verification Before Assertion

Before stating anything about the codebase as fact, verify it:

- Before proposing changes, read the actual file — do not infer contents
  from filenames.
- Before using a tool, confirm its name from the tools list — do not guess
  tool names.
- Before choosing a library, check what's already imported in the repo —
  do not default to what's popular.
- When you don't know, say "I don't know" and use a tool to find out. Do
  not fill in with plausible-sounding guesses.
- Before suggesting any CLI command, API call, library method, property
  name, or response field that you haven't already run or read
  documentation for in this session, verify the exact syntax against
  available docs tools. Training-data recall is unreliable for flags,
  subcommands, API method names, and field shapes — they change between
  versions, and "I'm pretty sure" is not verification. Full procedure
  in TASK-LOOP.md "Verification Discipline."
- The verification table is the deliverable, not a formality. It must
  include a `Quote / line` column with a literal fragment from the
  source for each claim. A table that lists command names without
  evidence is verification theater, not verification — and lets
  fabricated arguments, field names, and response shapes ship under
  verified-looking command names. See TASK-LOOP.md "Verification
  output format" for the required schema and granularity.
- **Two-pass discipline for code-gen output.** For any output that
  names CLI subcommands, API methods, response fields, library
  function signatures, SQL identifiers, or config keys, do not stop
  after the first pass. Run a second pass that re-validates every
  claim against the documentation, with the explicit framing "audit
  this against the docs, look for hallucinated method/command names,
  flag anything you cannot quote as UNVERIFIED." First-pass
  verification reliably produces theater-shaped output (correctly-
  formatted table, hollow content); second-pass framed as audit
  reliably converts it to substance. Don't ship code-gen output that
  hasn't been through the audit pass. See LESSONS.md "Two-pass
  verification" for the failure mode this prevents.

## Communication

- Announce structural steps as you take them: "Reading X", "Planning change
  to Y", "Running tests". This lets the user follow progress.
- Be concise. Do not recap what you just did unless asked.
- When you hit ambiguity, ask one specific question rather than guessing.

## Safety

- Never commit, branch, push, merge, rebase, or otherwise modify git state
  unless the user explicitly asks for it in the current session.
- Never run destructive operations (rm -rf, database resets, force push,
  mass deletions, dropping tables, force-rewriting history) without
  explicit confirmation for that specific operation.
- Never edit files outside the repo root without asking.
- Never add dependencies, modify lockfiles, or change build configuration
  without explicit permission.
- Never modify generated or vendored directories. Common examples:
  `.git/`, `node_modules/`, `vendor/`, `coverage/`, `dist/`, `build/`,
  `out/`, `_build/`, `.venv/`, `venv/`, `target/`, `.next/`, `.nuxt/`,
  `__pycache__/`. If you're unsure whether a directory is generated,
  treat it as if it is and ask before touching it.

## Scope Discipline

- Do the task the user asked for. Do not "improve" adjacent code.
- Do not refactor unrelated areas even if they look wrong to you. Note
  concerns at the end of the response instead.
- Match the style of surrounding code rather than imposing external
  conventions.

## When Uncertain

- One clarifying question beats an hour of untangling a wrong change.
- If you're about to do something you're not sure about, stop and ask.
- "I don't know" is an acceptable answer. Fabricating is not.

## Output Shape for Code Edits

You MUST split multi-component work into multiple tool calls.

- Each new function, component, class, or top-level definition:
  ONE dedicated edit tool call.
- Each edit: at most ~200 lines of output.
- NEVER combine multiple top-level definitions into a single edit.
  "These are closely related," "they belong together," or
  "it's more efficient" are NOT valid reasons to combine.

Exception (narrow): a single file rewrite, or a single cohesive
data structure (one constant, one config object, one schema), may
exceed 200 lines. The exception applies only when the output IS one
thing, not when multiple things happen to be related.

If you find yourself rationalizing that two or more functions/
components should be one edit, that rationalization is the
signal to split, not combine. Related components go in adjacent
tool calls, not combined tool calls.

**This is a hard rule.** Large batched edits have repeatedly caused
streaming connections to drop mid-output, losing the entire batch.
Pay the N-tool-call cost; it's cheaper than the one-failed-batch
cost.
