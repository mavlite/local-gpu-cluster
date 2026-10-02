# When to Delegate to local-delegate

The `local-delegate` MCP service enables Claude to queue agentic coding tasks and ask-local reads on a local LLM without blocking or loading bulky context into Claude.

## Eligibility

**GOOD fits for delegation:**

1. **ask_local**: Bulky document/log reads (inputs ≥ ~20K tokens)
   - Summarize a long log, condense a large file, extract facts from verbose docs
   - Local LLM reads the raw content; Claude receives a concise summary
   - Keeps Claude's context lean, model's latency low

2. **submit_task**: Agentic coding for well-specified, output-heavy tasks (≥ ~200-line diff)
   - Task description is clear and self-contained
   - Expects substantial code changes (new feature, refactor, multi-file fix)
   - **MUST include `checks`:** a list of shell commands or Python assertions Claude can later re-run to verify the work
   - Useful when the task would occupy >5-10% of Claude's context window
   - A/B measurement: for the first ~30 eligible tasks, flip a coin; compare local-LLM token cost vs. Claude token cost per task

**BAD fits (do NOT delegate):**

- Anything requiring THIS conversation's context (prior decisions, user preferences, project history)
- Architectural decisions, design reviews, or strategic choices
- Security-sensitive edits (auth, crypto, secrets, input validation)
- Small/medium changes (< ~200 lines diff) — review overhead outweighs savings
- Unstructured/ambiguous tasks — local model needs clarity; if you're unsure, Claude should clarify first
- Calls to `result()` or `record_review()` — only the human reviewer calls these; Claude calls `submit_task()` once and waits

## A/B Protocol (first ~30 tasks)

For tasks that meet eligibility, **flip a coin:**

- **Heads:** Delegate to `submit_task(…, checks=[…])` and await review
- **Tails:** Do it yourself in this conversation

**Record the outcome:** Either way, log the Claude token cost (from the Claude Code transcript) and the local LLM cost (from the result or ledger) to measure which is more efficient.

## Workflow

1. **Delegate (submit_task):**
   - Call `submit_task(task="...", repo="C:\\Users\\willi\\Documents\\GitHub\\local-gpu-cluster", base_ref="HEAD", checks=[[...], [...]], task_type="...")`
   - Return immediately; tell the user the job is queued
   - A human reviews it (call `result(job_id)`, re-run checks, then `record_review(…)`)

2. **Ask Local (ask_local):**
   - Call `ask_local(prompt="...", content="..." or files=[...], mode="summarize")`
   - Blocks briefly; returns summarized output
   - Useful for logs, config dumps, verbose docs — anywhere input >> output

3. **Do Not:**
   - Call `result()` or `record_review()` — only humans review delegated work
   - Delegate design decisions, security code, or anything needing conversation context
   - Queue a task without checks — the result gate will flag it as incomplete
