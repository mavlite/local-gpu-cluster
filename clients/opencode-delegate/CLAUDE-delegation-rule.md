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
- Repos outside `LOCAL_DELEGATE_ALLOWED_ROOTS` — `submit_task` refuses them
- Sensitive repos — Phase-1 jobs run with the user's rights (no OS sandbox until Phase 2)

Claude reviews every delegated result itself (design decision D4) using the `delegate-review`
skill — nothing lands without that review.

## A/B Protocol (first ~30 tasks)

For tasks that meet eligibility, **flip a real coin** — do not pick, or the arms are biased:

    python -c "import secrets; print(secrets.choice(['heads', 'tails']))"

- **Heads:** Delegate to `submit_task(…, checks=[…])`. When the job finishes, review it with the
  `delegate-review` skill and call `record_review(job_id, verdict, claude_tokens=…, …)` with
  Claude's token cost for the task; the local LLM cost is read from the job state automatically.
- **Tails:** Do it yourself in this conversation, then call
  `record_direct(task_type=…, claude_tokens=…, note=…)` to log the self-done arm.

**Record the outcome:** Either way, log Claude's token cost (from the Claude Code transcript)
via `record_review` (delegated) or `record_direct` (self-done) so both arms land in one
joinable ledger and we can measure which is more efficient.

## Workflow

1. **Delegate (submit_task):**
   - Call `submit_task(task="...", repo="C:\\Users\\willi\\Documents\\GitHub\\local-gpu-cluster", base_ref="HEAD", checks=[[...], [...]], task_type="...")`
   - Tell the user the job is queued, then start the wait as a **background** Bash command so the
     harness notifies you when it ends: `python -m scripts.delegate.cli wait <job_id>`
   - Keep working meanwhile; on the notification, follow `delegate-review` (`result(job_id)`,
     re-run the checks, read the diff, then `record_review(…)`)

2. **Ask Local (ask_local):**
   - Call `ask_local(prompt="...", content="..." or files=[...], mode="summarize")`
   - Blocks briefly; returns summarized output
   - Useful for logs, config dumps, verbose docs — anywhere input >> output

3. **Do Not:**
   - Apply a delegated diff without the `delegate-review` procedure, or when `gate_reasons` is non-empty
   - Delegate design decisions, security code, or anything needing conversation context
   - Queue a task without checks — the result gate will flag it as incomplete
