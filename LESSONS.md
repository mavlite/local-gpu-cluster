# Lessons Learned

Dated ledger of specific mistakes and their fixes. Each entry should
describe something concrete that happened, the underlying cause, and
the action that resolved it.

Entries are historical records. Some specific fixes reference
infrastructure that has since been replaced — when that happens, add an
**Update (YYYY-MM-DD)** postscript noting what changed rather than
rewriting history. The underlying principle in each entry is usually
still load-bearing even when the specific tool or config file is gone.

This file is for failure memory — not for operating guidance (that
lives in RULES.md and TASK-LOOP.md) or per-repo conventions (that lives
in the repo's AGENTS.md).

Read this file before proposing any plan or making non-trivial changes.
Prior failures often apply to the next task.

## 2026-04-16: Hallucinated project structure

**What happened:** LLM proposed a refactoring plan based on assumed file
sizes, assumed function counts, and assumed tooling that didn't match
reality.

**Root cause:** No pre-flight checks were run. The model guessed instead
of reading actual files.

**Fix:** Before proposing any plan, run glob + read on the relevant
files to confirm actual structure. Do not infer file contents, function
names, line counts, or organization from filenames alone. If a plan
references a file, that file must have been read in the current session.

## 2026-04-16: Hallucinated tool names

**What happened:** LLM called tools named "list" and "todo" which don't
exist in OpenCode.

**Root cause:** Model guessed tool names instead of using the ones
actually provided.

**Fix:** Only use tools that appear in the current session's tool list.
Tool names change between OpenCode versions and plugin loads. When
unsure whether a tool exists, enumerate the available tools first
rather than guessing a plausible name. Common confabulations include
"list," "search," "find," and "todo" — but the specific set varies.
The discipline is: read the tools list before calling, not "memorize
which fictional tools don't exist."

## 2026-04-16: Incorrect file parsing

**What happened:** LLM tried to parse an xlsx file using zipfile and raw
XML instead of using openpyxl or pandas.

**Root cause:** Model defaulted to a low-level approach instead of using
proper libraries.

**Fix:** For binary file formats, use purpose-built libraries (openpyxl
or pandas for Excel, python-docx for Word, pdfplumber for PDF). Never
parse xlsx as raw zip+XML. Verify the library is available before
writing code against it.

## 2026-04-20: Compaction tool-call violations

**What happened:** Twice in one day, Qwen3.6 attempted to call tools
(read in the morning, bash in the afternoon) while OpenCode's hidden
compaction agent was generating a conversation summary. OpenCode
rejected the tool calls and the session entered unrecoverable context
overflow.

**Root cause:** The compaction agent prompts the active model to
"summarize the conversation" and explicitly forbids tool calls during
that turn. Qwen3.6's fine-tuning emphasizes tool use so heavily that it
reaches for tools even when explicitly told not to, especially when the
conversation being summarized contains tool-heavy history. The
coder-long Modelfile did not have a compaction-mode rule; coder-36 also
needed it.

**Fix:** Added a Compaction Agent Mode block to both coder-36 and
coder-long Modelfiles forbidding tool calls on system-level summary
prompts. Use manual /compact between tasks so compaction happens at
clean state boundaries rather than mid-loop. Watch for the same pattern
on other hidden system agents (title generation, session summary).

**Update (2026-05-19):** Modelfiles are retired (no more Ollama in the
V620 cluster). The Compaction Agent Mode rule now lives in
`~/.config/opencode/TASK-LOOP.md` and applies model-side via the system
prompt rather than via Ollama Modelfile injection. The underlying
discipline (don't reach for tools during compaction-agent prompts) is
unchanged.

## 2026-04-20: Compaction rule over-triggered on user prompts

**What happened:** The initial Compaction Agent Mode rule was matching
too broadly. Any user prompt containing the word "summarize" or similar
— even when used conversationally ("summarize what was done") —
triggered Qwen to enter summary mode and refuse all tool calls. Session
stalled because Qwen could no longer read files to verify state.

**Root cause:** The original rule used loose pattern matching on the
words "summarize," "summary," and "compact context." Qwen's
instruction-following is literal; it didn't distinguish between a user
saying "summarize X" (normal work) and OpenCode's hidden compaction
agent issuing a system-level summarization prompt (meta-work).

**Fix:** Tightened the trigger to require ALL of four co-occurring
signals: system-level prompt, explicit compaction terminology, rigid
output format with named sections, and absence of a user turn.
Explicitly noted that user-facing words like "summarize," "recap," and
"status" do NOT trigger compaction mode. Applied to both coder-36 and
coder-long Modelfiles, and mirrored in TASK-LOOP.md.

**Update (2026-05-19):** Modelfile-based rule replaced by the
TASK-LOOP.md "Compaction Agent Mode" section. Same four-signal
discrimination criteria.

## 2026-04-21: Qwen 3.x thinking mode incompatible with RAG backend

**What happened:** Initial setup used Qwen 3.6 (coder-long, then a
coder-rag variant) as the LLM behind AnythingLLM's RAG pipeline. Every
query took 60-90 seconds because Qwen 3.6 generated 1000+ tokens of
<think> reasoning before answering. Attempts to suppress thinking via
Modelfile SYSTEM prompt /no_think failed because AnythingLLM sends its
own system prompt per request, which overrides the Modelfile. A
TEMPLATE-level override appending /no_think to every user message also
failed — the model ignored it.

**Root cause:** Qwen 3.x's thinking mode is deeply baked into the
fine-tuning and cannot be reliably suppressed from either the system
prompt or the user message. The only reliable way to disable it is via
the Ollama API think:false parameter, which requires client support
(AnythingLLM does not set it).

**Fix:** Switched AnythingLLM to Qwen 2.5 32B Instruct (rag-qwen25),
which has no thinking mode. RAG query times dropped from 60-90s to
20-40s, no <think> leaks, citations work cleanly. Kept Qwen 3.6
(coder-long, coder-36) for OpenCode where reasoning does help. Lesson:
use the right model family for each job. Thinking mode is great for
code/agent work, overhead for retrieval synthesis.

Added belt-and-suspenders strip_thinking() in the MCP bridge so if any
future client reintroduces a thinking model, tags won't leak to
downstream consumers.

**Update (2026-05-19):** The V620 cluster migration replaced this fix.
Qwen3.6 35B-A3B is now used for BOTH OpenCode AND RAG via a unified
chat upstream. The FastAPI router on LXC 153 injects
`chat_template_kwargs: {enable_thinking: false}` automatically for any
model request matching `rag-*` aliases — see `scripts/files/router-
app.py`. AnythingLLM no longer needs a separate Qwen 2.5 model. The
original principle still applies: thinking mode is overhead for
retrieval-synthesis workloads; suppress it at the request boundary,
not via SYSTEM-prompt tricks.

## 2026-04-21: Broadcom search backed by local SearXNG, not Broadcom API

**What happened:** Initial implementation of broadcom-techdocs MCP
tried to call https://knowledge.broadcom.com/api/search/v1/documents —
a guessed endpoint that returned 404. Broadcom does not publish a
public search API; their portal search uses internal
session-authenticated calls.

**Root cause:** Assumed parity with typical knowledge-base platforms.
Did not verify the endpoint existed before coding against it.

**Fix:** Redirected search_broadcom to call the local SearXNG instance
with a site-filter on techdocs.broadcom.com and knowledge.broadcom.com.
Gets the same functional outcome (list of relevant Broadcom URLs),
avoids dependency on an undocumented private API, and doesn't burn
Tavily or context7 quotas. fetch_broadcom still calls Broadcom directly,
which is fine — that's how their portal is designed to be accessed.

Lesson: for any API-backed MCP, verify the endpoint returns the expected
shape with a curl test BEFORE writing the server code.

**Update (2026-05-19):** The broadcom-techdocs MCP described here was
retired during the V620 migration. VCF docs are now served via the
sdg-docs MCP on LXC 155, which queries the `vcf-reference` AnythingLLM
workspace directly (no external search needed). The general principle
(verify API endpoints with curl before coding against them) is now
codified as part of TASK-LOOP.md's Verification Discipline.

## 2026-04-21: SSE read timeout on large batched edits

**What happened:** While executing Tasks 2-6 from a plan (inserting 6
React components before the TabButton function), Qwen bundled all 6
inserts into a single edit tool call rather than making 6 separate
edits. The combined output was large enough that the SSE stream between
Ollama and OpenCode timed out partway through the generation, aborting
the tool call and losing all work for that attempt.

**Root cause:** Two contributing factors.

1. Qwen's default "efficiency" instinct to batch independent edits into
   single tool calls conflicts with the reality of streaming over SSE
   from a local model at ~30-40 tok/s — long outputs take minutes and
   are fragile.
2. OpenCode's SSE read timeout fires when the stream goes too long
   without new chunks. There is a known OpenCode regression making this
   more frequent.

**Fix:** Added Output Shape for Code Edits section to RULES.md enforcing
many-small-edits over one-large-edit. Each logical unit (component,
function, section) gets its own tool call. Mirrored the rule into
TASK-LOOP.md's Plan step so it's consulted during planning.

Also worth remembering: if SSE timeouts recur even with small edits,
consider downgrading OpenCode to the last known stable version or
disabling reasoning mode for mechanical execution tasks where thinking
tokens are pure overhead.

## 2026-04-22: Rule-level enforcement is probabilistic; prompt-level is deterministic

**What happened:** Despite the Output Shape rule in RULES.md and its
mirror in TASK-LOOP.md, Qwen still occasionally bundled multiple
components into one edit and hit SSE timeouts. However, when the user
added "break this plan up into small write chunks" directly in the task
prompt, Qwen complied immediately and the session completed successfully.

**Root cause:** Rule files get loaded once per session and applied as
background knowledge, not as active planning guidance. Qwen can quote
the Output Shape rule verbatim when asked but doesn't reliably consult
it during every planning step. A direct in-prompt instruction for the
current task is consulted at planning time, not at session start.

**Fix:** Integrated "declare tool-call count before editing" into
TASK-LOOP.md's Plan step — the moment Qwen actually plans — rather than
leaving it as a standalone RULES.md section only. When the rule is tied
to the workflow moment it applies to, Qwen consults it in context rather
than as dormant knowledge.

Also worth remembering: for stubborn pattern-violations, a short
reminder in the task prompt ("one tool call per component, please") is
a reliable backup even when the rules should prevent it. Not a failure
of the ruleset — just a recognition that local models apply rules
probabilistically, and prompt-level reinforcement matters.

## 2026-04-22: OpenCode SSE timeouts on Qwen 3.6 thinking mode — proxy workaround

**What happened:** Qwen 3.6 with thinking mode enabled consistently
triggered "SSE read timed out" errors in OpenCode at roughly the 2-minute
mark during long generations (large code files, multi-component work).
Raising `timeout` and `chunkTimeout` in opencode.json had no effect — the
actual timeout is hardcoded somewhere in OpenCode's AI SDK path and
isn't exposed via config.

**Root cause:** Three stacked OpenCode bugs:
  1. `options.think` parameter isn't forwarded to Ollama (#3755), so
     you can't disable thinking via config.
  2. SSE read timeout is hardcoded around 2 minutes (#1065, #2974, #17307).
  3. OpenCode sends some message shapes (multimodal content arrays, tool
     call assistant messages with string-serialized arguments) that
     Ollama's /api/chat rejects with 400 errors.

**Fix:** Built a Node.js HTTP proxy that sits between OpenCode and Ollama
(deployed at port 11435 on the Ollama LXC). The proxy:
  - Injects `think: false` into requests for configured models.
  - Normalizes OpenAI message shapes into Ollama-compatible shapes.
  - Sends SSE comment-line keepalives every 15s while Ollama is idle.
    Client-side SSE timers treat any arriving data (including comments)
    as activity, so OpenCode's read timer never fires during long waits.

Full source, tests, and systemd unit in /opt/no-think-proxy/ on the
Ollama LXC. 30 assertions across 4 test suites verify the behaviors.

**Also worth remembering:** if a cloud-assistant tool is designed around
cloud LLM latency assumptions, running it against a local 30B+ model
will expose hidden assumptions. The fix is usually a proxy that shims
the gap, not a configuration tweak.

**Update (2026-05-19):** Architecture migrated to llama.cpp + FastAPI
router during the V620 cluster build. The no-think-proxy is retired.
The same three jobs (think:false injection, SSE keepalive,
OpenAI-compatible request/response shaping) are now done by
`router-app.py` on LXC 153. The hardcoded-OpenCode-SSE-timeout problem
(#1065, #2974, #17307) is addressed by the router's
KEEPALIVE_INTERVAL=15s SSE comment-line writer, which preserves
OpenCode's "data arrived" timer during long generations. The deeper
principle still applies: cloud-assistant tools designed around
frontier-LLM latency assumptions need a shim layer when run against
local 30B+ models — the shim is now the router instead of a dedicated
proxy.

## 2026-05-19: Hallucinated CLI syntax in generated TrueNAS audit script

**What happened:** Asked to generate a TrueNAS Scale dataset-audit script
with the sdg-documentation MCP (containing OpenZFS docs + TrueNAS official
docs) available. The model produced a Bash script that called
`zfs get -H -o name,type,used,...` — that syntax is wrong (the `-o` flag
for `zfs get` selects output columns from a fixed set, not properties).
It also called `midclt call snapshottestutil.get_periodic_snapshot_tasks`,
a fabricated method (real one: `pool.snapshottask.query`). The script
wouldn't run as written; `set -e` combined with `2>/dev/null` would
silently produce empty output instead of failing loudly.

**Root cause:** RAG retrieval was available via MCP and the docs DID
contain correct syntax (openzfs/docs has full zfs/zpool reference). The
model retrieved the high-level "best practices" content (which was
accurate — its 10 validation rules were correct) but didn't call MCP a
second time for CLI/API syntax. It defaulted to training-data recall
for command flags, conflated `zfs get` with `zfs list`, and invented
midclt method names that sounded plausible.

**Fix:** When generating code that invokes external CLI tools or APIs,
each command's syntax must be verified against the docs MCP (or
context7, or project docs) before inclusion. Training-data recall is
unreliable for: CLI flags, subcommands, API method names, library
function signatures. Especially for: zfs, zpool, midclt, kubectl,
terraform, gcloud/az/aws, language package managers, database CLIs.

Documented as a rule in RULES.md under "Verification Before Assertion"
and as an explicit Plan-step requirement in TASK-LOOP.md.

## 2026-05-19: Custom error envelope shape breaks OpenCode's Zod validator

**What happened:** Router emitted upstream failures as
`{"error": "service_degraded", "detail": "..."}` — error field as a
STRING. OpenCode's response validator (AI-SDK / Zod) parses
chat-completion responses as a union of either `{choices: [...]}`
(success) or `{error: {object}}` (failure). A flat error-as-string
matched neither, surfacing a cryptic "invalid_union" Zod validation
error to the user instead of the actual upstream problem (a llama-
server crash). The visible symptom masked the real failure for several
minutes during debugging.

**Root cause:** Assumed OpenAI-compatible APIs were lenient about error
response shape. They aren't — clients validate strictly against the
documented schema. Our error JSON needed `error` as an OBJECT with at
least `type` and `message` keys, not a flat string.

**Fix:** Centralized `_error_body(type, message)` helper in
router-app.py that returns `{"error": {"type": ..., "message": ...,
"code": null}}`. Routed every error path through it — 11 sites total,
including the SSE streaming DEGRADED_FRAME. Added a global FastAPI
exception_handler for HTTPException so token-budget 413s, 403 auth
failures, etc. also go through the envelope instead of FastAPI's
default `{"detail": "..."}` shape. Verified via a deliberate 413
smoke test that the response body matches the OpenAI schema.

General principle: when implementing "compatible" APIs, the
client-side schema is the spec. Don't rely on what the docs say should
work — send a curl-based smoke test that deliberately triggers each
error path before declaring done. If a custom field shape "looks
about right," that's not verification.

## 2026-05-19: systemd ExecStart silently truncated by blank line in heredoc

**What happened:** scripts/51-lxc-amd.sh wrote llamacpp-chat.service's
ExecStart= as a multi-line continuation. The unit included an optional
draft-model block ($DRAFT_LINES) on its own line in the heredoc. When
the draft block was empty (the default for Qwen3.6 — speculative
decoding disabled due to vocab mismatch with available drafts), the
heredoc substitution produced a literal blank line in the middle of
ExecStart=. systemd treats a blank line as end-of-directive, silently
dropping every flag after it — `--no-mmproj`, `--flash-attn`,
`--reasoning-format`, `--jinja`, `--mlock`, `--log-prefix`, `--metrics`.

The chat unit had been running in this degraded configuration for the
entire post-pivot history. Only discovered when investigating an
unrelated issue by running `ps -ef | grep llama-server` and noticing
the flag list was suspiciously short.

**Root cause:** systemd unit-file continuation semantics. Backslash at
end of line continues to the next line, BUT a blank line ends the
directive regardless of what was intended. Combined with an optional
conditional segment ($VAR + literal newline) that produced nothing when
empty, the generated file looked syntactically fine to a human reading
it but parsed as truncated to systemd. No warning, no error — just
silently missing flags.

**Fix:** Move optional conditional content to the BEGINNING of the next
guaranteed-present line rather than its own line. Make the variable
self-terminate with `\\\n` when non-empty so it slots in cleanly.
Empty case: `${VAR}    --next-flag \` becomes `    --next-flag \`.
Non-empty case: the variable's trailing `\\\n` provides the
continuation to the next flag.

General principle: when generating multi-line directives that include
optional conditional content, never put the optional segment on its
own line. Anchor it to a line that's always present. And after every
provisioning script change, verify the GENERATED output, not just the
source — `ps -ef` on the running process tells you what's actually
applied.

## 2026-05-19: Recurring ROCm fault on llama.cpp --cache-reuse long-context checkpoint

**What happened:** llama-server on gfx1030 (V620) crashed ~6 times per
24 hours with "ROCm error: an illegal memory access was encountered"
during `hipMemcpyAsync`. Faults always occurred at the same code path:
a multi-turn conversation continuation where `--cache-reuse 1024`
triggered a KV-cache checkpoint comparison on a long-context
(>70K-token) prompt, and the host-to-device tensor copy extending the
existing slot's KV cache faulted on the second turn. systemd
auto-restarted the unit in ~10 seconds, but during that gap OpenCode
received an upstream error.

**Root cause:** Known stability issue in llama.cpp's HIP backend on
gfx1030 specifically in the cache-reuse checkpoint path. Not OOM — VRAM
was nowhere near full. The bug surfaces when ctx_size is large and
multi-turn conversation continuity triggers the `hipMemcpyAsync` call
that extends an existing slot's KV cache. Cannot be fixed locally.

**Fix:** Multi-layer defense rather than a single fix:
  1. Router emits OpenAI-shaped error envelope (see prior entry) so
     OpenCode's `chatMaxRetries: 3` can transparently retry.
  2. systemd `Restart=on-failure RestartSec=10` on llamacpp-chat.service
     brings the unit back automatically.
  3. New systemd timer (`scripts/59-llamacpp-restart-timer.sh`)
     proactively restarts llamacpp-chat twice daily (04:00 + 16:00 UTC)
     to flush GPU memory fragmentation before it accumulates to the
     point of causing a fault during a user session.
  4. Fresh-start slots have no checkpoint to fault on, so retries
     after a crash almost always succeed on the first attempt.

General principle: for known-but-unfixable upstream bugs, build the
defense in layers (proper error shapes → client retries → automatic
service recovery → proactive prevention) rather than trying to
eliminate the failure. Each layer reduces user-visible impact even if
none individually solves the problem.

## 2026-05-20: Verification theater — table without an evidence column

**What happened:** Re-tested the TrueNAS dataset-audit prompt with
thinking-on and the tightened TASK-LOOP verification rules in place.
The model did more research than prior attempts — multiple RAG
searches, two web searches, one WebFetch — then framed the script with
TWO "Verified commands" tables. Both tables had a single column
listing command names. The script underneath shipped:

- a fabricated API call (`midclt call core.get_instance system.info` —
  real one is `midclt call system.info`),
- a broken argument shape (`midclt call pool.dataset.query ''` with
  empty string — must be JSON like `'[]'`),
- hallucinated property names (`synced` instead of `sync`, `snapdev`
  which isn't a standard OpenZFS property),
- and invented response field names on `pool.query`
  (`disk_devices`, `check_disk_health`, `quota_warnings`).

All of this rode underneath verified-looking command *names*.

**Root cause:** The verification table's columns didn't force the
model to produce evidence — only to assert verification. Verifying a
command *name* says nothing about its *arguments* or *response field
shapes*, but the table format conflated the three. Two visually-thorough
"Verified" tables made the output look like substance when it was
performance.

**Fix:** Tightened TASK-LOOP.md "Verification output format" to:

1. Require a `Quote / line` column carrying a literal fragment from
   the source. No quote → row is UNVERIFIED.
2. Define claim granularity explicitly — each command, each argument
   shape, each field name in parsing logic is its own row.
3. Name the two anti-patterns (single-column table; claim restated as
   quote) so the failure mode has a name the model can recognize.

Also added a one-line cross-reference in RULES.md so the principle is
visible in the always-loaded universal rules, not only inside the
multi-step loop.

**General principle (surfaced by the TrueNAS test, applies anywhere):**
the gap between "I verified that X exists" and "X behaves as I assume"
is where hallucination hides. The verification artifact has to demand
evidence the user could spot-check in 5 seconds, not a checkmark the
model self-issues. The discipline applies uniformly to any code-gen
task that names CLI subcommands, API methods, response fields,
library keyword arguments, SQL identifiers, or config keys — kubectl
flags, terraform resource fields, AWS CLI options, ORM column names,
systemd directives. The TrueNAS query is the vehicle that surfaced
the rule, not its scope.

## 2026-05-20: Two-pass verification — self-audit catches what first-pass misses

**What happened:** Same model, same RAG access, same rules. First pass
on a TrueNAS audit-script prompt produced a 3-column verification
table with no Quote column and a fabricated `zfs.pool.query` API call
that would have failed at runtime with "method not found." When the
operator explicitly asked "review your output against documentation
and validate against the docs to ensure nothing was hallucinated," the
model did a real second-pass audit:

- Went back to the RAG, listed the actual pool query methods
  (`pool.query`, `zpool.query`, `zfs.resource.query`), confirmed that
  `zfs.pool.query` never appears in any retrieved doc — caught the
  fabricated method name.
- Produced a corrected 4-column verification table populated with
  literal quotes from the API reference ("Parameter 1: filters —
  Type: array Default: []", "Id — Type: integer — Unique identifier
  for this storage pool", etc.).
- Honestly flagged UNVERIFIED rows for things it had inferred rather
  than quoted — argument shape inherited from a sibling method, JSON
  property keys derived from descriptions instead of explicit schema
  lines.
- Fixed the script (replaced `zfs.pool.query` with `pool.query`)
  before any execution attempt.

**Root cause:** First-pass verification is biased toward producing the
*appearance* of verification — a correctly-shaped table — rather than
the *substance*. The model treats the verification artifact as a
presentation element by default. A second prompt that explicitly
frames the task as **audit** (not production) flips the model into
evidence-gathering mode against the same sources it had skimmed past.

This is a different failure than "the rules don't bind." The rules
bind on the audit pass; they don't bind on the generation pass. The
trigger is explicit framing as review/audit/validate, not the rule's
existence in the system prompt.

**Fix:** For any code-gen output that names commands, APIs, response
fields, library kwargs, SQL identifiers, or config keys, run a
two-pass workflow:

1. **Pass 1 — produce:** generate the script plus an initial
   verification table.
2. **Pass 2 — audit:** explicitly prompt "review your output against
   documentation, validate every claim against a source, and flag
   anything you cannot quote as UNVERIFIED. Look specifically for
   hallucinated method/command names."

The audit pass produces materially different (and more honest) output.
Bake this into the standard prompt as a mandatory second step rather
than treating it as optional follow-up — the operator who shipped the
first-pass script without the audit would have hit a runtime "method
not found" on the very first call.

**General principle (complements the verification-theater entry
above):** the model is *capable* of the verification discipline; it
just doesn't trigger by default. Production framing produces theater;
audit framing produces substance. For high-stakes outputs, the
two-pass workflow is operationally non-negotiable. The TrueNAS
audit-script test surfaced this — but the pattern applies anywhere
production-and-audit are distinguishable tasks (code review, security
review, doc accuracy review, config validation).

## Template for new entries

<!-- Copy this when adding new lessons:

## YYYY-MM-DD: Brief title

**What happened:** Concrete observation of what went wrong.

**Root cause:** The underlying reason, not a guess.

**Fix:** Specific action taken. Describe the discipline, not a skill or
tool name that might change later.

-->
