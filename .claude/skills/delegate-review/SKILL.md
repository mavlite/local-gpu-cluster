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

Call `record_review(job_id, verdict, fix_lines, cause)`:

- `verdict`: one of "merged", "fixed_then_merged", "rejected"
- `fix_lines`: number of lines you had to fix (0 if merged as-is)
- `cause`: reason for rejection or fix (e.g., "test failed", "gate rejected diff", "check mismatch", "incorrect logic")

Example:

```
record_review(
  job_id="abc123",
  verdict="merged",
  fix_lines=0,
  cause="all checks passed, diff clean"
)
```

## A/B Measurement (First ~30 Tasks)

For the first ~30 eligible tasks (≥ 20K input OR ≥ 200-line output), **record Claude's token cost**:

1. Note the task's Claude token cost from the Claude Code transcript (the "used tokens" line after this response)
2. Call `record_review(…)` — the local LLM's token count goes into the ledger automatically
3. Compare: Is delegating cheaper than doing it in-conversation?

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

For the first ~30 eligible tasks, flip a coin:
- **Heads:** Delegate (call `submit_task`)
- **Tails:** Do it yourself

Measure token cost both ways. Use results to tune future delegation decisions.
