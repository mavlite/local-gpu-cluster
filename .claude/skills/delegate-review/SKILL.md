# delegate-review — Review and measure delegated tasks

## When to Use

After a delegated `submit_task` job completes, use this skill to:
1. Read and verify the job's diff and check results
2. Honor the result gate's flagged items (items flagged by the result gate demand review before merge)
3. Record your review verdict (merged / fixed-then-merged / rejected)
4. Measure A/B token cost (local LLM vs. Claude) for the first ~30 eligible tasks

## Procedure

### Step 1: Fetch the Result

Call `result(job_id)` from the local-delegate service:

```
{
  "status": "done" | "failed" | "abandoned",
  "diff": "unified diff",
  "checks": [["cmd1", "output1"], ["cmd2", "output2"], ...],
  "checks_skipped": false,
  "summary": "task summary",
  "tokens": {"prefill": N, "decode": N},
  "duration_s": N,
  "flagged": ["path1", "path2", ...],
  "gate_reasons": ["reason1", "reason2", ...],
  "error": null
}
```

### Step 2: Review the Diff

READ the full diff (`result.diff`) line-by-line. Understand what changed. Is it correct?

### Step 3: Re-Run the Checks Yourself

For each check in `result.checks`:
- Run the shell command or Python assertion **in your environment**
- Compare your output to `result.checks[i][1]` (the local model's output)
- If they differ, investigate: is it a legit difference (platform-specific, env-dependent) or a bug?

### Step 4: Honor the Result Gate

The result gate flags dangerous or policy-violating changes:

- `gate_reasons`: reasons the diff was rejected (symlinks, gitlinks, mode changes, protected paths, binary files) — **all must be reviewed and resolved before merge**
- `flagged`: items requiring attention (package.json, requirements.txt, lock files, YAML) — **demand explicit review** but may be acceptable

**Never merge a job whose `gate_reasons` is non-empty.** Fix the issues or reject the job entirely.

### Step 5: Record Your Verdict

Call `record_review(job_id, verdict, session_id, fix_lines, cause)`:

- `verdict`: one of "merged", "fixed_then_merged", "rejected"
- `session_id`: the UUID in your scratchpad path, so the service can find this session's
  transcript and **measure** Claude's token cost for the task — the submit turn plus the
  result→review turns, excluding the background wait. Do NOT hand-type `claude_tokens`;
  only pass it to override a measurement you know is wrong.
- `fix_lines`: number of lines you had to fix (0 if merged as-is)
- `cause`: reason for rejection or fix (e.g., "test failed", "gate rejected diff", "check mismatch", "incorrect logic")

`record_review` reads the job's own `local_tokens`, `task_type`, and `duration_s` from
its state file, measures `claude_tokens` from the transcript, and writes ONE consolidated
measurement row (`kind: "review"`, `delegated: true`, plus `token_source` and
`claude_token_components`), then removes the job's work dir. It refuses (returns an `error`,
keeps the work dir) if the job is not terminal.

Example:

```
record_review(
  job_id="abc123",
  verdict="merged",
  session_id="0e083120-0cb6-4980-8493-748d37699fc5",
  fix_lines=0,
  cause="all checks passed, diff clean"
)
```

### Self-done arm (the coin-flip 'tails' case)

When the coin comes up **tails**, call `mark_start(task_type="...")` BEFORE doing the task
(it returns a `marker_id` that anchors the task's start in the transcript). Do the task
yourself, then log the self-done arm:

```
record_direct(
  task_type="refactor",
  marker_id="<from mark_start>",
  session_id="<your session UUID>",
  note="did it inline; ~250-line diff"
)
```

The service measures `claude_tokens` from the `mark_start`→`record_direct` window. This
writes the matching measurement row (`kind: "review"`, `delegated: false`, `job_id: null`)
so the delegated and self-done arms are joinable in one ledger.

## A/B Measurement (First ~30 Tasks)

For the first ~30 eligible tasks (≥ 20K input OR ≥ 200-line output), Claude's token cost is
**measured from the transcript**, not typed:

1. Pass `session_id` to `record_review(…)` (delegated) or `record_direct(…)` (self-done); the
   service computes `claude_tokens` for the task's own turns. The local LLM's count comes from
   the job state automatically.
2. Check progress and the running comparison with `python -m scripts.delegate.cli report`.
3. Go/no-go: keep delegation only if it is ≥20% cheaper in Claude tokens on some clear task band.

**Eligibility for A/B:**
- **ask_local**: Inputs ≥ ~20K tokens (e.g., summarize a long log or document)
- **submit_task**: Output-heavy tasks with ≥ ~200-line diffs (new features, multi-file refactors, significant fixes) that include `checks`

## Result Gate Violations (Do Not Merge)

Common gate rejections and how to fix them:

| Reason | Action |
|--------|--------|
| `symlink: path` | Remove symlinks; use text files instead |
| `gitlink: path` | Never commit submodules via agent; git submodule add manually if needed |
| `mode-change +x: path` | Remove executable bit changes; scripts should not change permissions |
| `protected path: .git, .github, .husky, conftest.py, .gitattributes, .gitmodules` | These are read-only; reject entirely |
| `binary: path` | Binaries are forbidden; reject or request text-only changes |

## Eligibility to Delegate (Reference)

**ask_local** — bulky reads (≥ ~20K tokens):
- Summarize a large log or config dump
- Condense verbose documentation
- Extract data from a long file

**submit_task** — agentic output-heavy (≥ ~200-line diff with checks):
- New feature implementation (state the requirements, provide checks)
- Multi-file refactoring (break it into clear tasks, provide checks)
- Non-trivial bug fix (expected behavior must be clear, checks must validate it)

**Not for delegation:**
- Design decisions, architecture reviews
- Security-sensitive code (auth, crypto, input validation)
- Tasks needing THIS conversation's context
- Small/medium changes (< ~200 lines) — review overhead is too high

## Coin-Flip A/B Protocol

For the first ~30 eligible tasks, flip a **real** coin (don't pick — it biases the arms):

    python -c "import secrets; print(secrets.choice(['heads', 'tails']))"

- **Heads:** Delegate (call `submit_task`), then review + `record_review(…, session_id=…)`
- **Tails:** `mark_start(task_type)` first, do it yourself, then `record_direct(…, marker_id=…, session_id=…)`

Measure token cost both ways. Use results to tune future delegation decisions.
