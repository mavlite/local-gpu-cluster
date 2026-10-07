# Distributed workforce: GPU lead + CPU worker agents — design (experiment)

**Status:** draft for review, 2026-10-07.

**Supersedes:** the capacity part of `2026-10-05-minisforum-cluster-integration-design.md` §5, for this
experiment. The Phase-1 gate's quality data stays valid. Its capacity result is superseded, because the
workers there could not execute anything.

**Decision this produces:** build the workforce (Phase 2) or not.

## 1. Goal

The GPU model (**lead**) runs the work:
1. It breaks work into tasks.
2. It delegates them, with instructions and acceptance checks, to CPU worker **agents** on the three
   Minisforum nodes.
3. It **reviews** their results, then accepts them or sends them back.

Workers are real agents: they read, edit, **run tests** and iterate. The aim is a distributed workforce
that uses every machine: the GPU on planning and review, the CPUs on execution.

We **measure first**. Phase 2 is built only if the team beats the lead working alone on a real-repo task
set, by the rule in §9.

## 2. Why the previous measurement is superseded

The Phase-1 gate (gate `1f13ead`, amendment `e553446`, v2 `b66f607`) established the following.

- **Quality is a tie.** Worker pass@2 was 62.7% (n=6) against the coordinator's 63.5% (n=5), on Aider
  Polyglot Python.
- **Arm B is slower and fragile.**
  - B1 ran at 48.5 tasks/h against A1's 99.2.
  - It passed 4/6 tasks against 6/6.
  - Workers looped in 2 of 3 arm-B attempts in the gate, and again in v2.
- **The loop mechanism is known.** Workers had no command tool (no bash, by design). They tried to verify
  their work and re-wrote an identical `verify.py` 16–37 times. The only tool response was "Wrote file
  successfully". The timelines are in the opencode session databases.
- **Neither attempted fix worked.**
  - Presence penalty 1.5 did not stop the loops. Its look-back is 64 tokens; the repeats were about
    1,000 tokens.
  - It also cost 3.9 points of pass@2.
  - opencode's `doom_loop` cannot see one-call-per-turn loops. This is upstream issue #51965, and we
    reproduced it.
  - opencode's `steps` limit is honoured but advisory: tools stay available after the limit.
- **Upstream reports point the same way.** Qwen3.6-35B-A3B is reported to loop more than dense models in
  agent use. Some ik_llama.cpp-specific tool-call repetition is reported against Qwen3.5. Mainline
  llama.cpp on CPU is untested.
- **Memory:** workers need 32 GB VMs. At 24 GB all three were OOM-killed by ik's RAM prompt cache.

Root cause: a no-execution harness, not the model's coding ability. Execution in a sandbox is the fix
under test.

## 3. Scope

**In scope:**
- **Sandbox runner.** It executes all agent tool calls. Workers and lead get a full shell with internet
  inside it.
- **Lead protocol:** plan, delegate, review (read the diff, run tests), feedback, accept, then export.
- **Router change:** per-run **scoped API keys**.
- **Loop check (W1)**, which picks a non-looping worker configuration, reliability first.
- **Real-repo task set (W2):** historical replay of about 8 commits from this repository.
- **Measurement (W3):** team against lead-alone, with a pre-registered rule.
- **Host monitoring** during runs.

**Out of scope (roadmap, only if built):**
- Day-to-day entry points: an opencode session, a Claude Code hand-off, a task queue.
- An asynchronous dispatcher.
- Research and analysis work types.
- Persistent services.
- Running workforce load while the lab is in Full mode. Workers exist only in **Minimal** mode.

## 4. Roles and resources

| Role | Model / place | Resources |
|---|---|---|
| Lead | `qwen3.8-nothink` (router alias; Qwen preset, presence 1.5 router-injected) | 1 of 3 chat slots, 3 × 128K mode |
| User | the user's own chat/RAG | 1 slot reserved for the whole run; never queued behind the workforce |
| Spare | — | 3rd slot (unused by design, keeps the user's slot free) |
| Workers ×3 | Qwen3.6-35B-A3B on llmbench01-03 (32 GB, ik_llama.cpp + MTP unless W1 picks otherwise) | one slot each, CPU |
| Sandbox | per-run Docker container on LXC 158 | executes every tool call for lead and workers |

The lead is told to dispatch in **waves of 3**. opencode blocks the lead until every task in a parallel
batch returns, so a straggler idles the others. This is a known limit. The asynchronous dispatcher that
would remove it is on the roadmap.

## 5. Sandbox and boundaries

**Runner.** A pinned image `workforce-runner` containing:
- opencode 1.18.34 (Linux);
- git, Python 3, pytest, shellcheck, bash and coreutils.

How a run executes:
1. A fresh container starts as a non-root user, with CPU/memory limits and a `--rm` lifetime.
2. It mounts only that run's workspace: a **clone** of the repo, with no remotes and no credentials.
3. opencode runs inside the container, so every tool call executes there.
4. Grading and hidden checks run **outside** the container after the run. Hidden tests never enter the
   workspace.

**Network (egress allowlist, enforced in `DOCKER-USER`).**

| Destination | Allowed |
|---|---|
| Internet (default route) | yes — packages, docs |
| Router `192.168.6.153:8000` | yes, with the run's scoped key |
| Workers `172.16.10.205-207:8090` | yes, worker key |
| Nested validation lab `10.50.10.0/24` (vmbrlab) | yes — ops tasks may execute against it |
| Everything else in RFC1918 (user LAN, Proxmox host, production LXCs, real VCF lab VLANs, CRS309) | **no** |

**Keys.**
- The router gains **scoped keys**: per-run tokens limited to the workforce aliases, with an expiry, and
  revoked at teardown.
- The worker key stays throwaway: rotated per experiment and shredded at teardown.
- Keys are visible to agents inside the container, by design. Because egress is limited, a leaked key
  only reaches the same services it already reaches, and only until revoked.

**Agent permissions** (opencode, inside the sandbox):

| Agent | Permissions |
|---|---|
| Workers | edit / write / read / glob / grep / **bash** allowed; webfetch allowed; `external_directory` deny; `steps: 40` |
| Lead | read / glob / grep / **bash** (to run tests) / task |

The lead's `edit` permission is **allowed but prompt-gated**: it may edit only after two failed rework
rounds. This is a soft boundary, and §8 measures early edits.

**Write-back.** Accepted work becomes commits on per-task branches inside the clone. The harness exports a
git bundle, and the user's machine fetches it as `workforce/<run>/<task>` branches. **Nothing in the
sandbox can push. The user merges.**

**Proven by running before any measured run (W0).** From inside the container:
- the internet answers;
- the router and workers answer with the scoped key;
- the nested lab answers;
- the Proxmox host, `192.168.6.226`, the user LAN and the real lab VLAN time out;
- no host path is visible outside the workspace;
- the clone has no remotes.

## 6. Lead protocol

1. **Plan.** Split the request into tasks. Each task carries: the files involved, the instructions, and
   **acceptance checks** — which tests must pass and what behaviour is expected.
2. **Delegate.** Use `task` to send waves of up to 3 to `worker-1..3`.
3. **Review each result.**
   - Read the diff and run the acceptance tests in the sandbox.
   - **Accept**, or **re-delegate to the same worker** with concrete feedback.
   - Allow at most 2 rework rounds per task.
4. **Fallback.** After 2 failed rounds, the lead implements the fix itself and flags it.
5. **Finish.** Commit each accepted task on its own branch, then report: accepted, reworked, lead-fixed,
   failed.

The protocol is tested against the stub server before W3. It must show rework and fallback actually
happening, the same way the step limits were verified.

## 7. W1 — loop check: choose the worker configuration (reliability first)

**Setup**
- **Tasks:** the gate's T1 (ttl-cache) and T2 (semver), where loops were worst, plus 2 tasks from W2.
- **Agent:** a single worker agent with bash in the sandbox, no lead.
- **Repetitions:** 3 per configuration per task, run on all three workers in parallel.

**Configurations**

| ID | Engine | MTP | Thinking | Presence |
|---|---|---|---|---|
| C0 | ik_llama.cpp | on | off | 0 (Qwen coding preset) |
| C1 | mainline llama.cpp b11026, CPU | draft-mtp | off | 0 |
| C2 | ik_llama.cpp | off | off | 0 |
| C3 | ik_llama.cpp | on | on, `preserve_thinking: true` | 0 |

**Metrics**
- **Loop episodes:** at least 3 consecutive identical tool calls (same tool, same arguments) in the
  opencode session DB.
- Steps used, step-limit hits, hidden-test pass, and time per task.

**Rule.** Choose the configuration with **0 loop episodes** and the highest pass rate. Break ties by speed.
If none is loop-free, choose the lowest loop rate and record that loops remain.

## 8. W2 — real-repo task set (historical replay)

**Selection.** About 8 past commits from this repository, each of which:
- came with tests (Python under `scripts/**/tests`, or bash with fakes like `test_gate_env.py`);
- has relevant files plus tests that fit a **64K-token worker context**;
- is self-contained, with no live-cluster dependency.

At least 2 must be **ops scripts**, with tests exercisable against fakes or the nested lab.

**Each task:**
- the repository at the commit's parent, with the change reverted;
- an issue-style request;
- the commit's own tests, visible to agents;
- **hidden checks**: extra assertions the agents never see.

**Freeze.** The set is frozen and hashed before W3, like the gate's task set.

## 9. W3 — measurement and pre-registered decision rule

**Arms**

| Arm | Who does the work |
|---|---|
| **L (lead alone)** | Same lead, same protocol, same sandbox, same 3-slot layout. The lead implements every task itself, sequentially. |
| **T (team)** | Lead plus 3 workers, as in §4–6. |

**Run schedule.** 3 runs per arm, interleaved `L T T L L T`.
- §5.1-style sizing applies: the CI half-width on each decision metric must be below its threshold, or
  runs are added before any cross-arm comparison.
- A run is the whole frozen task set.
- Workers are cold-restarted before every T run.

**Per run, measured**
- accepted tasks (passing the hidden checks);
- wall-clock and **accepted tasks per hour**;
- **lead GPU time**: the sum of chat-server prompt and eval ms during the run, from the llama-server
  logs. No other chat use is allowed during runs; the user's slot is idle by protocol;
- user-slot responsiveness: a probe on the reserved slot, p50 against an idle baseline;
- rework rounds, lead fallbacks, early lead edits, loop episodes and step-limit hits.

**Validity**
- A run is **invalid**, and repeated, only for **infrastructure** failures: host hang or crash, network
  loss, harness error.
- **Loops, timeouts and failed tasks are outcomes, not invalid runs.** This fixes the gate's flaw, which
  treated a loop-induced timeout as an infrastructure failure.

**Decision rule.** Build Phase 2 if **all** of these hold:
1. **Quality.** In every pair, T accepted ≥ L accepted − 1.
2. **Win.** Either one of:
   - T accepted tasks per hour exceeds L, with a 95% Welch CI excluding 0; **or**
   - lead GPU time per accepted task is ≥ 30% lower in T, with a CI excluding 0.
3. **User protection.** The user-slot probe p50 is ≤ 1.5× its idle baseline in every T run.

Otherwise do not build. Record which clause failed.

## 10. W0 — preconditions

**Host monitoring.** The Proxmox host hung twice on 2026-10-06 with no logged cause.
- Enable `systemd-pstore` / efi-pstore.
- Enable the AMD hardware watchdog (`sp5100_tco`) so a hang reboots the host and leaves a boot reason.
- Send remote syslog / netconsole to a listener off-host.
- A run spanning a hang is invalid.

**Routes.** Make persistent:
- LXC 158 → `172.16.10.0/24` via `192.168.6.11` (today it is a manual, non-persistent route);
- LXC 158 → `10.50.10.0/24` via the Proxmox host, with matching forward rules.

**Sandbox boundary tests.** All of §5's checks, run and recorded.

**Scoped keys.** The router change, test-first, with a security review before deploy.

**Lab mode.** The lab is in Minimal mode with workers at 32 GB.

**Freeze.** Commit the pre-registration — the task-set hash, the chosen worker configuration from W1, and
the thresholds — before the first W3 run.

## 11. Teardown

1. Revoke the scoped keys.
2. Shred the worker key.
3. Run `gate_env.sh restore`, or the 3-slot-mode exit.
4. Stop the worker servers.
5. Remove the sandbox images and volumes.
6. Commit results and the decision.

The exported `workforce/*` branches stay for the user to merge or delete.

## 12. Risks and open questions

- **Lead review is the bottleneck.** Its single slot does all review prefill. It may cap team throughput
  regardless of how many workers there are; clause 2b (GPU time) captures the benefit if so.
- **Straggler waves.** opencode batch semantics persist until an asynchronous dispatcher exists.
- **Soft boundary on the lead's edits.** The lead may edit early. This is measured, not prevented.
- **Internet-enabled sandbox.** Agents can fetch and run arbitrary code. Containment rests on the
  container plus egress rules plus scoped keys. W0 must prove the boundary.
- **The host's stability is unknown.** Monitoring makes a recurrence diagnosable. It does not prevent one.
- **The task set is small.** About 8 tasks, with a 64K worker context, drawn from one repository.
  Generalising to other repositories is not established.
