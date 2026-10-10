# Workforce review efficiency and the GPU-solo baseline: design (round 2)

**Status:** rev 2, 2026-10-10, after four adversarial reviews (experiment validity, harness
architecture, security boundary, cheapest path) and the transcript check that settled their main
disagreement. Rev 1 is superseded.
**Follows:** `2026-10-07-distributed-workforce-design.md` (rev 2) and the W3 result in
`docs/superpowers/workforce/results-w3.json`.
**Decision this produces:** whether the CPU-worker team is worth building, measured against the
GPU-only arm **and** against the GPU implementer alone with no reviewer, which is what the user would
otherwise run.

## 1. What the W3 data shows, re-read

- The team run `w3-2-t`: 14 of 16 accepted, 5.88 worker-accepted tasks per hour, 11 rework rounds.
  The GPU-only run `w3-1-g` (cut short at 11 tasks by a power outage): 10 of 11, 4.22 per hour.
- **7 of the 27 review rounds ended with no verdict.** In every one, the reviewer had used its 20
  steps, opencode injected its last-step banner ("MAXIMUM STEPS REACHED … respond with text only …
  summarize"), and the model's whole reply was that banner echoed back. The reviewer did not run out
  of things to check; it was told to summarize instead of decide, and it obeyed. Each of those 7 cost a
  worker rework round with the message "the reviewer gave no clear verdict", plus another review.
- The reviewer's 366 tool calls were 268 orientation (`read` of whole files, `grep`, `ls`) and 53
  pytest runs on its own copy, although the packet already carries the harness-run test output.
- The 6 real REVISE verdicts caught bugs the visible tests missed (a `-N` dry-run flag, a
  call-signature mismatch, a dead guard). Finding them needed **call sites**, not just the changed
  definitions. The workers' first pass had the visible tests passing on 15 of 16 tasks.
- Re-scoring `w3-2-t` with the 7 cap-caused rounds removed gives 7.0–7.7 accepted per hour
  (1.65–1.8× G), and the team's makespan already beat G's: 2.21 h for 16 tasks against an extrapolated
  3.45 h.
- **GPU-solo, computed from `w3-1-g`'s records:** the GPU implementer alone took a mean 516 s per
  task with 10 of 11 accepted, so about 6.3 accepted per hour run sequentially **at the speed observed
  while sharing the GPU with a reviewer**; single-stream it should be faster still. In arm G the
  reviewer was a rubber stamp (0 REVISE in 11; it accepted w2-05, which failed hidden tests). GPU-solo
  costs nothing to build and may beat the team outright. It must be in the comparison.
- The user has ruled that chat usability during a run does not matter: the GPU runs flat out, and the
  user-lane clause is dropped. The user-lane probe keeps running and is reported only.

## 2. Goal

Cut the review waste with the smallest change that the data supports, prove it offline against the
harvested W3 reviews, then measure three arms in one window on the metrics that match "assign a
project and wait": makespan and GPU-hours per accepted task, with throughput alongside.

## 3. Harness changes (`scripts/tools/workforce`; nothing in the router or the workers)

### 3.1 A review always ends in a verdict

1. **Prompt.** `REVIEWER_PROMPT` gains: "If you are told your steps are exhausted, your reply must
   still contain the ACCEPT or REVISE line, decided on what you have seen." It also gains: "The test
   output in the packet was produced by the harness on exactly the change being graded; do not re-run
   it. Run code only to test a specific suspicion. Issue independent reads and greps in the same step."
2. **Verdict turn.** If a review still ends with no verdict, the harness **resumes the same opencode
   session** (`oc.run(..., session=r.session_id)`) under a new agent `reviewer-verdict` (same model,
   every tool denied including read/grep/glob/list, `steps: 3`) with the message "Give your verdict
   now. Reply starting with ACCEPT or REVISE:". One step on a cached prefix. The review copy is kept
   until this turn is done (today `_review` deletes it right after the first run).
3. **If there is still no verdict**, the task goes to the lead fix (as a REVISE with the reviewer's
   last text as feedback), exactly as a second failed review does today. There is **no new outcome**:
   a task is never graded unreviewed, so a no-verdict reply never pays off for an implementer.
4. **Capped is a measurement, not a trigger.** `capped = loops.steps_from_events(events) >=
   REVIEW_STEPS`, never the banner text. `timed_out` is recorded separately. Both are reported per
   review and per arm.
5. `REVIEW_STEPS` stays **20** for the offline replay (§5.1), so the replay isolates the prompt and
   verdict-turn effect. The replay decides whether it rises for the window.

### 3.2 The packet carries what the reviewer was reading for

1. **Function context from git, not from the tree.** The diff for the reader is generated with
   `git diff -W` (whole-function context) from the workspace's own git dir, with
   `*.py diff=python` written to that git dir's `info/attributes` at workspace creation. Context
   therefore comes from git blobs of **allowed paths only** (`paths.classify`, which already drops
   symlinks and out-of-scope files), never from a tree agent code has run in. Rev 1's `ast` builder is
   dropped.
2. **Dropped paths by name only.** Out-of-scope or disallowed changes appear as one line each
   ("dropped: aaa.txt (not in scope)"), never as diff text, so padding cannot push the real change
   out of the packet.
3. **Call sites.** For each top-level `def` or `class` name added or changed by the diff, the packet
   lists `file:line: <source line>` for every reference in the snapshot's allowed Python files
   (a word-boundary grep of the pristine snapshot plus the patched in-scope files), capped at 40
   lines per name.
4. **Tests that touch the changed files.** Every test file in the pristine snapshot whose text
   contains a changed file's module stem or basename (the repo's tests load modules by `sys.path`
   inserts and `spec_from_file_location`, so an import match would miss `router-app.py`).
5. **Test output stays as it is** (`-q`, last 200 lines; a passing run is already a few lines),
   with one harness line above the untrusted note: "visible tests: X of Y expected passed, rc N",
   taken from the grader's JUnit report, so a forged "N passed" line in stdout cannot pass as the
   result.
6. **Caps.** 12,000 tokens (characters ÷ 3.5) **and** the existing 1,900 lines, whichever binds. When
   cut: call sites first, then the tests list, then function context (an oversized context block
   shrinks to ±60 lines around its hunk before it is dropped), the diff last. Every omission is listed
   by `file:start-end` at the end, so the prompt's claim about the packet stays true.
7. **Framing.** Every section that comes from the implementer (diff, context, call sites, test
   output, and the implementer's own SUMMARY, which the packet now includes so the reviewer can check
   a multi-part request part by part) sits **below** `UNTRUSTED_NOTE`, inside delimiters that carry a
   per-packet random nonce. `REVIEWER_PROMPT` repeats the rule.

### 3.3 The implementer finishes every part of a request

`IMPLEMENTER_PROMPT` gains: "A request may ask for several things. Your SUMMARY must list each one
with the file and function that covers it. If no listed test covers a part, implement it anyway."
This is the only worker-side change. Same model, C2, same step cap.

### 3.4 Scan and accounting

- The verdict turn's events go to `review-r{n}-verdict.jsonl`; `scan.transcript_order` is extended to
  cover it, with a test that both attempts are scanned.
- `run.json` gains `review_capped`, `review_timed_out` and `verdict_turns` counts; `reviews[]` gains
  `capped`, `timed_out`, `steps`. Nothing changes in what counts as accepted.
- `gpu_ms` attribution covers the verdict turn (it is one more lead request in the same window).

## 4. What does not change

The sandbox, keys, policies, grading (pristine snapshot + filtered patch + JUnit gate), the 16
frozen W2 tasks (manifest `54c494ae…`), worker configuration C2, the implementer and fixer step caps.

## 5. Measurement (round 2)

### 5.1 Offline replay first (no window, a few GPU-hours)

The 27 harvested W3 review rounds are replayed through the new reviewer: same packets rebuilt from
the harvested patches with §3.2, `REVIEW_STEPS = 20`, the §3.1 prompt and verdict turn. Three
questions, each with a gate:
- **Verdicts:** at most **1 of the 7** previously capped rounds still ends without a verdict after
  the verdict turn. Otherwise `REVIEW_STEPS` goes to 40 and the replay runs again; if it still fails,
  stop and report.
- **Agreement:** on the 20 rounds that had a verdict, the new reviewer agrees on at least 16, and
  disagreements are read by hand (a new REVISE on a task the hidden tests later failed is a gain, not
  a loss).
- **Cost:** median prompt tokens per review and median steps per review, both reported against W3's
  (the fix should cut both; if steps rise, the context is making the reviewer explore more, and §3.2
  is reconsidered).
- **Also settled here:** whether opencode resets its step counter on a resumed session (the verdict
  turn relies on at least one more step being available).

### 5.2 One window, three arms

- Arms: **S** (GPU implementer alone, no lead: `--w1` mode with grading, but the GPU model),
  **G** (lead + GPU implementer), **T** (lead + 3 C2 workers). All three use the §3 harness.
- Order **S G T T G S**, in one window, each run on the same 16 tasks. Before each run's clock
  starts: one untimed warm-up request per worker and one to the lead, recorded. Workers cold-start
  once per window as before.
- Stamped into every `run.json` before the window: harness commit, opencode config hash, the
  `qwen3.8-nothink` alias line from the router, the llama.cpp build and slot layout. No harness edit
  between runs.
- Validity as before (host stamps; harness `run.json`). An invalid run stays in the schedule and is
  not repeated.

### 5.3 Decision, fixed before the window

Per arm, pooled over its runs: **makespan per 16 tasks**, **accepted tasks per hour**
(implementer-accepted for G and T, accepted for S), **GPU-hours per accepted task** (chat-server
ms ÷ accepted), and a paired per-task acceptance table. For T also worker-hours per accepted task.

Three bands, on the pooled accepted-per-hour ratio **T ÷ max(S, G)** and makespan:
- **Build:** T's ratio ≥ 1.5 **and** T's makespan is the shortest **and** T's acceptance is within 1
  task of the best arm. (1.5 is a chosen bar, not derived: it is the gain that makes three extra
  nodes and a review loop worth operating. The W3 result stands as marginal under the old 2× rule.)
- **Marginal:** ratio in [1.2, 1.5) or makespan within 10% of the best. The user decides, with the
  GPU-hours figure in front of them.
- **No:** otherwise. In particular, if **S** has the shortest makespan at equal acceptance, the
  answer is "run the GPU implementer alone", and the review loop is kept only as an optional quality
  gate.
- **Reviewer health:** per arm, capped first reviews and timeouts are reported. If any arm has more
  than 2 capped reviews after the verdict turn, the result is **inconclusive**, the reviewer is fixed,
  and **all** arms rerun.
- A task-level paired bootstrap CI on bottleneck seconds per accepted task (lead GPU ms for G,
  implementer GPU ms for S, worker seconds ÷ 3 for T) is reported alongside; with 2 runs per arm it
  is the statistic least sensitive to one straggler task.

## 6. Tests the plan owns

- `REVIEWER_PROMPT` and `IMPLEMENTER_PROMPT` contain the new sentences (pinned).
- A reviewer run whose events show `steps >= REVIEW_STEPS` and no verdict triggers exactly one
  resumed verdict turn under `reviewer-verdict` with every tool denied; a verdict there finalizes
  normally; a second no-verdict goes to the lead fix; `review_capped` and `verdict_turns` count.
- `capped` comes from the step count, never from text; a banner quoted in a code comment does not
  set it.
- Packet: `git diff -W` context with the Python attribute; dropped paths by name; call sites for a
  changed `def`; the tests list matches a `spec_from_file_location` test; the JUnit-derived header;
  both caps and the drop order; the omissions list; the nonce delimiters; the implementer SUMMARY
  below the untrusted note.
- `transcript_order` includes `review-r{n}-verdict.jsonl`.
- The replay tool (`cli.py replay-reviews`) rebuilds packets from a harvested run and records
  verdicts, steps and prompt tokens per round.
- The analysis gains arm S, the pooled ratio, makespan, GPU-hours per accepted task and the bands.

## 7. Risks

- opencode may not reset the step counter on resume: the replay finds out; the fallback is a fresh
  session with the packet and the reviewer's last text, which costs more steps.
- `git diff -W` picks the enclosing function by the `diff=python` regex, which is coarser than `ast`;
  the ±60-line shrink and the omissions list bound the damage.
- Arm S has no reviewer, so its accepted count depends entirely on hidden tests; w2-05 shows what
  that costs (a wrong accept). That is the point of measuring it.
- Three arms in one window take ~9–11 h; the window runs unattended on the host, as W3 did.
