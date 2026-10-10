# W3 amendment: a single team run against run 1's GPU-only baseline

**Date:** 2026-10-10, before any arm-T data existed.
**Decided by:** the user ("go with option 1"), after the 2026-10-09 power outage invalidated run `w3-1-g`.
**Replaces:** the 8-run schedule and statistical decision rule of `preregistration-w3.json`. That rule
is not evaluated.

## Why
- The power outage killed `w3-1-g` 2.5 hours in. Under the pre-registration it is invalid and not
  repeated.
- The remaining seven runs would take about 16–18 hours, because a GPU-only run takes about 3.5 hours
  (the GPU both implements and reviews).
- The user chose a cheaper, explicitly non-statistical comparison.

## The comparison
- **Baseline (arm G).** Run `w3-1-g`'s tasks finished before the outage, read from VM 176
  `/srv/wf/runs/w3-1-g/tasks/*/record.json`:
  - 11 tasks finished, 10 accepted (every acceptance implementer-accepted). The miss was `w2-05`.
  - 8,528 s from the run start (15:18:38 UTC) to the 11th finish.
  - That is **4.22 accepted tasks per hour**.
- **Team (arm T).** One run, `w3-2-t`, of the same frozen 16 tasks (manifest `54c494ae…`), with
  workers on C2 and the same window layout and harness. It runs in window 2, with a fresh 30-sample
  idle baseline of the user lane.

## Thresholds, fixed now
1. **Throughput.** Arm T's accepted tasks per hour (worker-accepted only, as the pre-registration
   counts them):
   - **≥ 8.44** (2× the baseline): the team is clearly faster;
   - **4.22–8.44:** marginal, and the user decides;
   - **below 4.22:** no.
2. **Quality.** On the same 11 tasks G finished, T accepts **at least 9** (G's 10 minus 1). T's result
   on all 16 tasks is reported too.
3. **User lane.** The run's time-weighted probe p50 is **≤ 1.5×** window 2's idle-baseline p50.

**Build signal:** all three hold, with throughput at or above 2×.

## Limits, stated up front
- One run per arm, so there is no confidence interval.
- G's figure covers only the 11 tasks finished before the outage, in schedule order.
- The two runs ran at different times of day.
- The transcript scan still applies: a tainted task is dropped from both sides of the comparison.
