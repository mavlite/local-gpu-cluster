# Workforce review efficiency: design (round 2 of the experiment)

**Status:** draft for the user's review, 2026-10-10.
**Follows:** `2026-10-07-distributed-workforce-design.md` (rev 2) and the W3 result in
`docs/superpowers/workforce/results-w3.json`.
**Decision this produces:** whether the team arm (CPU workers + GPU lead) beats the GPU-only arm once
the review step stops wasting rounds, measured with one fresh run per arm.

## 1. What the W3 data showed

The team run `w3-2-t` (16 tasks, 2.2 h) accepted 14 tasks against the GPU-only run's 10 of 11, at
1.39× the accepted tasks per hour. It needed 11 rework rounds; the GPU implementer needed none.

Reading the 27 review rounds, rework is mostly **the reviewer's fault, not the workers'**:

| Review verdict | Count | Cause |
|---|---|---|
| ACCEPT | 14 | — |
| REVISE (a real defect) | 6 | 3 bugs the visible tests missed (`-N` dry-run flag, a call-signature mismatch, a dead guard); 2 on w2-10 where the worker never applied the change; 1 incomplete integration |
| **NONE: the reviewer ran out of steps** | **7** | `REVIEW_STEPS = 20`; the reviewer spends ~15 calls per round, 268 of 366 on orientation (`read` whole files, `grep`, `ls`), and 7 of 27 rounds hit the cap with no verdict |

Each NONE cost a full worker rework round (with the useless message "the reviewer gave no clear
verdict") plus another review. Those 7 account for 7 of the 11 rework rounds.

The workers' first pass is sound on its own terms: 15 of 16 ran the visible tests and had them passing
before handing off. The exception, w2-10, is a two-part request where the worker solved the easy half.

The reviewer's long prompts (up to 34K tokens; 785K prompt tokens in the run) are not the packet,
whose diffs are 100–4,000 tokens. They are the reviewer re-reading whole files (`router-app.py` is
~30K tokens) because the packet shows a diff with 3 lines of context.

The user has ruled that **chat usability during a run does not matter**: the GPU runs flat out, and
the user-lane clause is dropped.

## 2. Goal

Cut the review waste, then re-measure both arms with the same harness. Three changes, all in the
harness (`scripts/tools/workforce`), none in the router or the worker configuration.

## 3. Changes

### 3.1 A capped review is a terminal verdict, not a rework trigger

- `REVIEW_STEPS` goes from 20 to **40**.
- When a review ends with no verdict (`NONE`):
  1. **Once**, the reviewer is re-run with a fresh session on the same packet plus its own last text
     under "# Your notes from the previous attempt". Fresh, because a continued session would carry
     the step count.
  2. If that also ends in `NONE`, the task goes to grading as it stands, with outcome
     **`review-capped`** (accepted if the hidden tests pass). The worker is never sent back for a
     reviewer failure.
- The run record gains `review_capped` (count) and per-task `reviews[].capped: true/false`
  (`capped` = the final text contains the opencode step-cap banner "MAXIMUM STEPS REACHED").
- `accepted_per_hour` keeps counting `implementer-accepted` only; `review-capped` acceptances are
  reported separately. A run's throughput cannot be inflated by a reviewer that never decides.

### 3.2 The packet carries the context the reviewer keeps re-reading

For each hunk in the diff, the packet shows the **whole enclosing top-level definition** (function
or class) from the changed file, not 3 lines of context: `git diff -U<n>` is not enough, so the
harness computes it from the file. Python files use `ast` (the definition spanning the hunk's lines);
other files use the hunk ± 40 lines. Each context block is capped at 300 lines.

The packet also lists, under "# Tests that import the changed modules", every test file in the
snapshot whose imports name a changed module, so the reviewer stops `grep`ping for them.

The test output section is **summarized**: the final pytest summary line, then only the failing
tests' tracebacks (each capped at 60 lines). A fully passing run contributes one line.

The packet is capped at **12,000 tokens** (characters ÷ 3.5), not 1,900 lines. When it is cut, the
diff keeps priority: context blocks are dropped first (largest first), then the import list, and the
diff last. The full packet still goes to `FULL_PACKET` in the reviewer's copy.

The reviewer prompt gains one line: "The packet already shows every changed definition in full and
names the tests that import the changed modules; start from the packet, and read a file only for
something the packet does not show."

### 3.3 The implementer finishes every part of a multi-part request

`IMPLEMENTER_PROMPT` gains: "A request may ask for several things. Before you finish, list each one
and confirm your change covers it." Nothing else about the workers changes: same model, C2, same
step cap, same key.

## 4. What does not change

- The sandbox, keys, policies, grading and the transcript scan.
- The 16 frozen W2 tasks (manifest `54c494ae…`), so the new runs compare with the W3 data.
- Worker configuration C2, chosen by W1.
- The user lane probe keeps running and is **reported**, not judged.

## 5. Measurement (round 2)

- **One fresh run per arm**, G then T, same 16 tasks, same window layout, GPU flat out. Both arms get
  the §3 changes.
- **Decision rule, fixed before the runs:**
  1. **Throughput:** T's `implementer-accepted` per hour ≥ **1.5×** G's. (W3 measured 1.39× against a
     reviewer that wasted 7 rounds; the fixes help both arms, so the bar is above W3's ratio.)
  2. **Quality:** T accepts at least G's count minus 1 over the 16 tasks (hidden tests decide).
  3. **Reviewer health:** `review_capped` ≤ 1 per run in both arms. If it is higher, §3.1 did not
     work and the result is reported as inconclusive.
- **Reported, not decisive:** GPU ms per accepted task, prompt tokens per review, rework rounds,
  user-lane p50, w2-10's outcome.
- Validity as before: the host stamps (no reboot, no router or chat restart) and the harness
  `run.json`.

Both runs are expected to take 2–3.5 h. The G run first: if it is cut short, T waits.

## 6. Tests the changes need (owned by the plan)

- `parse_verdict` / a new `is_capped(text)` recognize the step-cap banner.
- The pipeline re-runs a capped review once, with the notes attached, then finalizes
  `review-capped`; a capped review never enqueues rework; `run.json` counts `review_capped`;
  `accepted_per_hour` excludes it.
- Packet: enclosing-definition context from a Python file (one hunk inside a function, one at module
  level, one spanning two functions), non-Python fallback, the 300-line block cap, the
  importing-tests list, the summarized test output (passing run → one line; failing run → the
  failing tracebacks), the 12K-token cap and its drop order.
- Prompt text pinned by a test (the two added sentences).
- The W3 analysis helper reads `review_capped` and the new outcome.

## 7. Risks

- A 40-step reviewer that still caps on big tasks: §3.1 bounds the cost at one retry, and §5 rule 3
  reports it.
- Context blocks for large functions (`router-app.py` has 200-line handlers): capped at 300 lines
  each and dropped first under the token cap.
- Any change to the review step changes arm G too, so the W3 baseline (4.22/h) is not comparable;
  that is why round 2 re-runs G.
