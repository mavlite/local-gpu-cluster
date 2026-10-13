# CPU workers as overflow for a GPU-first workforce — proposal

**Status:** proposal, 2026-10-12. Not built. Grounded in the exploratory runs lt-1-t, lt-2-s and lt-3-g
(`qwen36-lead-orchestration-runs.json`); the S/G repeats (lt-4..7) will firm up the numbers.

## Why

The 2026-10-12 trials inverted the original premise. A tuned Qwen3.6 on the V620s implements the 16
frozen tasks alone in 27 minutes (S, 13/16), while the three CPU workers with the same GPU as lead need
93 minutes (T, 15/16). The CPU team is no longer the throughput engine. It still has two uses:

1. **The GPU is not always free.** It is the user's chat model first. While the user is chatting, or the
   chat slot is in the 1-slot layout, background work stalls. The CPU workers never compete for VRAM.
2. **Spare parallel capacity.** Three CPU workers at ~7 min/task add roughly 0.4 tasks/min next to the
   GPU's ~0.65 tasks/min, if they only take work the GPU would not reach soon.

## Measured per-task implement time (first + rework rounds, minutes)

| Run | Implementer | Median | Max | Note |
|---|---|---|---|---|
| lt-2-s | GPU, Qwen3.6 UD-Q4_K_M | 1.5 | 4.9 | no review |
| lt-3-g | GPU, same | 3.1 | 22.6 | includes reworks after GPU review; w2-10 is the tail |
| lt-1-t | CPU llmbench01-03, Qwen3.6 UD-IQ4_XS (ik) | 7.2 | 54.3 | w2-10 again the tail, 3 rounds on CPU |

The CPU is about 4-5x slower per task, and its reworks are where the time goes.

## Design

A new arm **O** (overflow) keeps the GPU as the primary implementer and adds the CPU workers under three
rules. Everything else (review settings, grading, scan) stays as in G.

1. **The GPU pulls first, always.** It takes the next pending task whenever it is free.
2. **A CPU worker pulls only when the backlog justifies it.** It takes a pending task only if
   `len(pending) * gpu_task_min > cpu_task_min`, i.e. the GPU would not reach that task before the CPU
   finishes it. With the medians above (1.5 vs 7.2) that is a backlog of more than ~5 tasks. Both
   estimates are running medians updated from finished tasks, seeded with the values above.
3. **Rework goes to the GPU.** Today a REVISE resumes the same implementer session (affinity). For a CPU
   task that means another 7-50 minutes on the slow path (w2-10: 54 min). In O, a REVISE on a CPU task is
   handed to the GPU implementer as a fresh session with the packet and feedback, like the lead fix.

**User-lane rule.** When the router reports user traffic on the chat slot (the existing scoped-key and
window machinery can expose this), the GPU implementer stops pulling new tasks and the CPU workers' gate
(rule 2) drops to zero, so background work continues on CPU only. This is the case the CPU fleet is
really for, and it needs no new hardware.

## Expected effect (estimate, to be measured)

For the 16-task set with an idle GPU: the CPU workers take roughly the first 4-5 tasks while the backlog
is deep, so the GPU handles ~11-12. Makespan drops from ~27 to ~20 minutes (~25%). With review (G-style)
the gain is larger, because the GPU's review queue is the bottleneck in G (implement 69 min, review
48 min, lead fix 25 min of G's 94). With the GPU busy serving the user, O degrades to the CPU team's pace
instead of stopping.

Quality should not drop: the CPU workers run the same model family, their first-round hidden-test pass
in T was 15/16 after review, and all reworks run on the GPU.

## Harness changes (small)

- `pipeline._impl_loop`: per-agent pull gate (rule 2) using running medians of finished tasks.
- `pipeline._review`: rework target = GPU implementer when the task's implementer is a CPU worker.
- `profiles.implementers("O")`: `impl-1` (GPU via `--lead-url`) + `w1..w3`.
- Controller/76: arm `O` accepted wherever `T` is, worker key required (like T).
- Tests: the gate never lets a CPU worker take a task when the backlog is short; a CPU REVISE becomes a
  GPU fresh session; the GPU always pulls first.

## How to decide

Run O once against the S and G repeats on the same 16 tasks. Adopt it if its makespan is at least 20%
below the better of S and G at equal or higher acceptance. Then test the user-lane rule separately by
running O while a scripted chat load holds the GPU.
