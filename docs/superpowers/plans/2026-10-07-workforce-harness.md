# Workforce Harness Implementation Plan (Plan C of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** the Python harness that runs the workforce experiment on the sandbox VM. For each task it:
1. dispatches the task to an implementer agent;
2. drives the lead's review and the rework rounds;
3. hands the task to the lead to fix after two failed rounds;
4. exports a path-filtered patch;
5. grades it from the pristine snapshot;
6. scores the session for loops.

It also builds task bundles from repository history on the workstation, imports accepted patches as
branches for you to merge, and applies the pre-registered W1 and W3 decision rules.

**Architecture:** one new stdlib-only package, `scripts/tools/workforce/`, made of 11 modules with
one job each (see File structure), 12 test modules and 2 test helpers.
- **opencode configuration.** It lives outside the agents' writable area, is read-only, and is
  loaded with project config disabled.
- **Who runs what.** The harness runs as root on the VM. Each opencode run is launched as an
  unprivileged agent user in its own transient systemd scope.
- **What agents can reach.** Agents see only `<run>/agent/<id>/`. Bundles, hidden tests, the
  baseline git and the records stay private.

**Tech stack:**
- Python 3.12+ (stdlib only; pytest for the tests);
- git;
- opencode 1.18.34;
- systemd-run;
- Docker (the grader, on the VM).

**Spec:** `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md`, rev 2, approved
2026-10-07. This plan covers:
- §5.1: workspaces, agent config, built-in subagents disabled;
- §5.3: keys reach agents only through the environment;
- §5.5: write-back and grading;
- §6: the review protocol;
- §7: the loop metric and the W1 rule;
- §9: the W3 rule and validity.

**Neighbouring plans:**
- **A, router keys:** live since 2026-10-07 (PR #7).
- **B, sandbox VM:** builds the VM this runs on and proves the boundary.
- **D, experiment runbook:** authors the W2 tasks and runs W1 and W3.

**How this plan carries its code.** The whole package was written test-first and **executed before
this plan was written**:
- 99 tests: 98 pass on Windows/Python 3.14 (the POSIX permission test skips), 98 pass on
  Linux/Python 3.13 (the opencode end-to-end test skips, since opencode isn't installed there), and
  98 pass on Python 3.12.3.
- Mutants killed:
  - project-config lock removed;
  - review on the live workspace;
  - acceptance without the grade;
  - path filter skipped;
  - rework sent to another worker;
  - session not resumed;
  - full packet not written;
  - loop thresholds changed.

The code is stored verbatim in the appendix `docs/superpowers/plans/2026-10-07-workforce-harness/files/`
(26 files) and was verified identical to the tested tree. Tasks **copy** it; nobody retypes it.

The plan is still not evidence that the code is right: reviewers must read the code and run it.

## Facts established by running (opencode 1.18.34, 2026-10-07)

1. **Project config can hijack agents.** `OPENCODE_CONFIG` with `OPENCODE_DISABLE_PROJECT_CONFIG=1`
   loads only our agents. Without that switch:
   - a workspace `.opencode/agent/<name>.md` **replaced the agent's prompt**;
   - a workspace `opencode.json` **granted webfetch**.

   Every review round starts a new opencode process, so an agent could rewrite its own permissions
   between rounds. The end-to-end test plants both files and fails if either loads; it was
   mutation-checked.
2. **Session resume works.** `--session <id>` resumes with the full history, so a REVISE continues the
   same implementer session.
3. **No subagents.** `task: deny` removes the task tool; `general` and `explore` are disabled as well.
4. **A read-only reviewer can still write.** A reviewer with `edit: deny` but bash still wrote files.
   Reviews therefore run on a disposable copy, which is deleted afterwards.
5. **`--file` placement.** `--file` is an array option and swallowed the message placed after it. An
   attachment reaches the model **capped at 2000 lines**, and works from outside `--dir`.
6. **Session storage.** Sessions are stored in `opencode.db` (SQLite). `opencode export <id>` prints
   a status line, then JSON with each tool call's name, input, status and timing.
7. **Bash ignores `external_directory: deny`.** That setting governs opencode's file tools only; bash
   can `cat` any file its user can read. Hence a separate agent user and private harness directories
   (decision 10 below).
8. **Grader false pass.** A grading runner that can't import pytest also reports "failed". A test
   asserts that the failure is a real test failure.
9. **Repo conftest files.** The root `conftest.py` only adds the repo root to `sys.path`, which
   `python -m pytest` from the root already does. `vcf-spec-tools/tests/conftest.py` provides
   fixtures (decision 3 below).
10. **Snapshot size.** A repository snapshot is 3 MB and 214 files, so a full `git add -A -f` diff per
    round is cheap.

## Decisions for you to confirm

The spec was silent or self-contradictory on these points; the code implements the choice shown.

| # | Decision | Why | Cost if wrong |
|---|---|---|---|
| 1 | A task counts as **accepted** only if the lead accepted it (or lead-fixed it) **and** its exported patch passes the visible and hidden tests. W3 quality uses accepted including lead-fixed; W3 win uses implementer-accepted only (§9). | Counting lead-accepted but wrong work would reward a careless reviewer. | Records keep both the verdict and the grade, so the analysis can be re-cut. |
| 2 | The W1 bound is the **one-sided** 95% Clopper–Pearson. With 30 attempts that means **zero loops in 30**. | Two-sided gives 11.6% for 0 of 30, so no configuration could ever pass the 10% rule. | Allowing 1 loop in 30 needs about 46 attempts per configuration. |
| 3 | `--noconftest` by default. A task may set `needs_conftest` (vcf-spec-tools). `conftest.py` is a protected path, so only the pristine conftest ever runs. | Strict `--noconftest` makes vcf-spec-tools tasks ungradeable. | Drop the option, which excludes those tasks from W2. |
| 4 | Strip all of `docs/superpowers/` from snapshots. | It's a superset of plans and specs, and also holds gate ledgers and results. | None: no task targets docs/superpowers. |
| 5 | A worker takes a new task while its last one awaits review. Rework goes back to the **same worker and session**, ahead of new work. The lead reviews in arrival order. | Spec §6 removes straggler blocking; idle workers would waste the arm. | Pinning a worker to one task until it's accepted is a small change. |
| 6 | A reply with no verdict counts as a failed review round. | The lead must give a verdict. | NONE verdicts are recorded per task; Plan D checks their rate in the pilot. |
| 7 | Invalid runs are excluded from W3. With fewer than 2 valid runs per arm there is no decision. | Spec §9 forbids added runs. | Plan D needs your call on a re-run window if that happens. |
| 8 | Agents can read `WF_ROUTER_KEY` and `WF_WORKER_KEY` from their environment. | These are the per-run scoped key and the throwaway worker key (§5.3), revoked or shredded at teardown. | Those keys' scope limits what this exposes. |
| 9 | The user-lane prober and lead GPU-time attribution run on the host (Plan D). | No owner key may enter the VM. The harness records timestamps for every round and review. | None for this plan. |
| 10 | Agents run as a separate unprivileged user (`--agent-user`), with the bundles and `<run>/tasks/` private (0700). `cli run` refuses world- or group-readable bundles. | Fact 7: bash would otherwise read hidden tests. | Plan B must create the user; the harness refuses to start without the permissions. |

## Global Constraints

**Code**
- Stdlib only; `pytest` for tests. Nothing added to `scripts/rag/requirements.txt`. No new linter or
  formatter config.
- Python 3.12+ (the VM), and also green on 3.14 (the workstation).
- Each file keeps one responsibility. The longest is `pipeline.py` at about 330 lines.

**Keys**
- Keys are read only from the environment: `WF_ROUTER_KEY` and `WF_WORKER_KEY`.
- They are never in argv, never logged, and never in config files (configs use `{env:...}`).
- No owner key is ever in the VM.

**Agent configuration**
- The agent config path is outside every agent-writable directory and read-only (`profiles.install`).
- `LOCK_ENV` is always applied (`profiles.opencode_env`).

**What the export and grade may use**
- Exported patches contain only declared, non-protected regular files (`paths.classify`).
- Grading uses only the pristine snapshot, the filtered patch, the restored visible tests and the
  hidden tests.

**Git and commits**
- Conventional commits, **no `Co-Authored-By` trailer**.
- Commit by explicit path only, and check the branch and `HEAD` before each commit; other sessions
  share this checkout.
- Never push; you push, or approve a PR merge.

**Checks**
- The workforce suite: `python -m pytest scripts/tools/workforce/tests -q -p no:cacheprovider`.
- The repo suites: `python3 -m pytest scripts/rag/tests scripts/files/tests -q`.

## Review Focus

These failure modes can only be exercised on the real VM or with the real model. Each is assigned
to the plan that owns the environment.
1. **Scope launcher on a real systemd.** `systemd-run --scope --uid=<agent>` must run opencode as the
   agent user with the `WF_*` keys in its environment, and the timeout kill must reach a
   `setsid nohup sleep` child.
   - Expected: the child dies when the scope is killed.
   - Pinned by: Plan B's boundary proof (here, only the argv and kill calls are tested).
2. **The bash boundary.** As the agent user, `cat <bundles>/<id>/hidden/*`, `ls <run>/tasks` and
   `git --git-dir <run>/tasks/<id>/git log` must all fail with permission denied.
   - Pinned by: Plan B's boundary proof.
3. **Docker grading reproduces local grading.** Every bundle's reference patch must pass, and its
   unchanged snapshot must fail, under `--grader docker:<image>`, not only under `LocalRunner`.
   - Expected: identical outcomes.
   - Pinned by: Plan B, which builds the grader image and validates every bundle with
     `DockerRunner` once.
4. **The real lead's verdict format.** qwen3.8-nothink may write "Verdict: ACCEPT" or bold text with
   a prefix. `parse_verdict` accepts a verdict line that starts with ACCEPT or REVISE (markdown
   allowed); anything else is NONE and burns a round.
   - Expected: a NONE rate near 0.
   - Pinned by: Plan D's pilot, which checks `reviews[].verdict` over 3 or more tasks before W1.
5. **A worker going down mid-task.** The implementer round hangs until `impl_s` (1800 s) and is
   recorded as a time-cap hit. The health monitor marks the run invalid at 300 s or more.
   - Pinned by: `test_worker_unreachable_for_five_minutes_invalidates_the_run` (monitor logic), with
     the real `/health` probe covered by Plan D's window checks.

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/tools/workforce/bundle.py` | workstation: task bundle from a commit (stripped parent snapshot + visible tests), answer-leak check, reference patch kept off the VM, validation, manifest |
| `scripts/tools/workforce/workspace.py` | unpack a snapshot safely; baseline git directory **outside** the workspace; raw changes; binary patch; apply a patch |
| `scripts/tools/workforce/paths.py` | which changed paths may be exported (declared, not protected, regular files) |
| `scripts/tools/workforce/profiles.py` | read-only opencode config (`impl-N`, `reviewer`, `fixer`; general and explore disabled); lock-down environment |
| `scripts/tools/workforce/oc.py` | `opencode run` headless: argv, session id, final text, export; `DirectLauncher` and `SystemdScopeLauncher` |
| `scripts/tools/workforce/review.py` | messages, review packet under the 2000-line attachment cap, verdict parsing |
| `scripts/tools/workforce/pipeline.py` | scheduling, review rounds, rework affinity, lead fix, export, grade, records; worker `HealthMonitor` |
| `scripts/tools/workforce/grade.py` | grading tree + `LocalRunner` / network-less `DockerRunner`; bundle layout |
| `scripts/tools/workforce/loops.py` | repeat/cycle loop episodes and steps per turn from `opencode export` |
| `scripts/tools/workforce/analysis.py` | W1 choice (one-sided Clopper–Pearson); W3 decision (paired bootstrap, Welch CI, user probe) |
| `scripts/tools/workforce/cli.py` | `bundle-build`, `bundle-validate`, `manifest`, `run`, `import`, `w1`, `w3` |
| `scripts/tools/workforce/README.md` | reference: pieces, footguns, tests |
| `scripts/tools/workforce/tests/*` | 12 test modules + `fake_opencode.py` + `wf_fixtures.py` |

## Interfaces produced (for Plans B and D)

**What the VM must provide (Plan B)**
- Python 3.12+ with pytest, git, and opencode 1.18.34 at `WF_OPENCODE` (or on `PATH`).
- `systemd-run`.
- An unprivileged agent user, e.g. `wfagent`. The harness runs as root.
- Docker and a grader image holding `python3`, `pytest` and the repo's test dependencies (`PyYAML`,
  `requests`, `trafilatura`). It is used as `--grader docker:<image>`.
- Bundles at mode 0700.

**Commands**

| Command | Where | Purpose |
|---|---|---|
| `cli.py bundle-build --repo R --taskdef D --out BUNDLES --refs REFS` | workstation | build one bundle |
| `cli.py bundle-validate --bundles BUNDLES --refs REFS --work W` | workstation | `{"problems": [...], "manifest": sha256}` |
| `cli.py manifest --bundles BUNDLES` | workstation | the hash frozen in the pre-registration |
| `cli.py run --arm T\|G --bundles B --out RUN --router URL [--worker URL ×3] [--w1] [--grader docker:IMG] [--agent-user wfagent]` | VM | writes `RUN/run.json`, `RUN/tasks/<id>/record.json` and `RUN/patches/<id>.patch` |
| `cli.py import --repo R --patch P --bundle B --run-id X` | workstation | creates branch `workforce/X/<id>` on the task's parent commit; your checkout is untouched |
| `cli.py w1 --config C0=run1,run2 ...` | workstation | W1 choice |
| `cli.py w3 --schedule S.json --tasks BUNDLES` | workstation | W3 decision; `S.json` lists runs with `arm`, `run_dir`, `valid`, `probe_p50` and `baseline_p50` from the host-side prober |

**Fields Plan D reads**
- `run.json`: `accepted`, `implementer_accepted`, `lead_fixed`, `accepted_per_hour`, `accepted_by_task`,
  `looped`, `rework_rounds`, `time_cap_hits`, `valid`, `invalid_reasons`.
- `record.json`: `outcome` (`implementer-accepted` / `lead-fixed` / `implemented` / `harness-error`),
  `accepted`, `grade`, `dropped`, `loops`, `rounds[]` and `reviews[]` with timestamps, `fix`.

**Task definition (Plan D authors these)**
- `taskdef.json` holds `id`, `commit`, `files`, `tests`, `hidden` (name → destination),
  `needs_conftest`, `timeout_s` and optionally `pytest_args`.
- Alongside it: `request.md` and `hidden/`.

---

### Task 1: Install the package on a feature branch and verify it

**Files:**
- Create: `scripts/tools/workforce/**`, the 26 files copied from the appendix.

**Interfaces:** see Interfaces produced above.

- [ ] **Step 1: Create the branch in its own worktree, off `main`**

```bash
cd /c/Users/willi/Documents/GitHub/local-gpu-cluster
git fetch -q origin
git worktree add -b feat/workforce-harness ../lgc-wf-harness origin/main
cd ../lgc-wf-harness && git branch --unset-upstream && git log --oneline -1
```

Expected: `7e9200d Merge feat/router-scoped-keys …` or a later `main`.

- [ ] **Step 2: Copy the appendix**

```bash
A=../local-gpu-cluster/docs/superpowers/plans/2026-10-07-workforce-harness/files
cp -r "$A/workforce" scripts/tools/workforce
git status --short && find scripts/tools/workforce -type f | wc -l
```

Expected: `?? scripts/tools/workforce/` and `26`.

- [ ] **Step 3: Run the suites**

On the workstation (Python 3.14, and the real opencode for the end-to-end test):

```bash
python -m pytest scripts/tools/workforce/tests -q -p no:cacheprovider | tail -1
python -m pytest scripts/rag/tests scripts/files/tests -q -p no:cacheprovider | tail -1
```

Expected:
- `98 passed, 1 skipped` (the POSIX permission test skips on Windows);
- `249 passed, 1 skipped`.

Then on Linux, in a throwaway venv in `/tmp` on the Proxmox host. The package is pushed as a
tarball, and the venv is removed afterwards:

```bash
(cd scripts/tools && tar --exclude=__pycache__ -cf /tmp/wf.tar workforce)
scp -q /tmp/wf.tar root@192.168.6.175:/tmp/wf.tar && rm -f /tmp/wf.tar
ssh root@192.168.6.175 'rm -rf /tmp/wftest && mkdir /tmp/wftest && tar -xf /tmp/wf.tar -C /tmp/wftest && rm -f /tmp/wf.tar && python3 -m venv /tmp/wftest/venv && /tmp/wftest/venv/bin/pip -q install pytest && cd /tmp/wftest && /tmp/wftest/venv/bin/python -m pytest workforce/tests -q -p no:cacheprovider -rs | tail -2; cd / && rm -rf /tmp/wftest'
```

Expected: `98 passed, 1 skipped`, with the skip being `test_e2e_opencode.py: opencode not installed`.

- [ ] **Step 4: Prove the key tests are live**

Mutate by matched replace, then revert. Never use `git checkout`.

```bash
cat > /tmp/mut.py <<'EOF'
import sys
p, a, b = sys.argv[1:]
s = open(p, encoding="utf-8").read()
assert s.count(a) == 1, (p, s.count(a))
open(p, "w", encoding="utf-8", newline="\n").write(s.replace(a, b))
EOF
W=scripts/tools/workforce
python /tmp/mut.py $W/profiles.py 'LOCK_ENV = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", ' 'LOCK_ENV = {'
python -m pytest $W/tests/test_e2e_opencode.py -q -p no:cacheprovider | tail -1
python /tmp/mut.py $W/profiles.py 'LOCK_ENV = {' 'LOCK_ENV = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", '
python /tmp/mut.py $W/pipeline.py '        accepted = bool(g and g["pass"] and outcome' '        accepted = bool(g is not None and outcome'
python -m pytest $W/tests/test_pipeline.py -q -p no:cacheprovider | tail -1
python /tmp/mut.py $W/pipeline.py '        accepted = bool(g is not None and outcome' '        accepted = bool(g and g["pass"] and outcome'
python /tmp/mut.py $W/pipeline.py '        patch = t.ws.patch(allowed)' '        patch = t.ws.patch([c[2] for c in changes])'
python -m pytest $W/tests/test_pipeline.py -q -p no:cacheprovider | tail -1
python /tmp/mut.py $W/pipeline.py '        patch = t.ws.patch([c[2] for c in changes])' '        patch = t.ws.patch(allowed)'
diff -r --exclude=__pycache__ $W ../local-gpu-cluster/docs/superpowers/plans/2026-10-07-workforce-harness/files/workforce && echo "reverted, identical to the appendix"
```

Expected:
- 1 failed for the lock mutant (the planted override hijacks the implementer);
- 1 failed for the accepted-without-grade mutant;
- 1 failed for the no-filter mutant;
- then `reverted, identical to the appendix`.

- [ ] **Step 5: Commit by path**

```bash
[ "$(git branch --show-current)" = feat/workforce-harness ] || exit 1
git add -- scripts/tools/workforce
git commit -m "feat(workforce): harness for the distributed-workforce experiment" -- scripts/tools/workforce
git show --stat --oneline HEAD | tail -1
```

Expected: `26 files changed`.

---

### Task 2: Independent security review of the agent boundary

**Files:** fixes only, each with a new test.

**Interfaces:** consumes the Task 1 commit. Produces a reviewed branch.

- [ ] **Step 1: Dispatch a `security-reviewer` subagent, read-only, on the branch diff**

Brief it with:
- the diff `origin/main...HEAD`;
- spec §5 and §6;
- this plan's Facts, Decisions and Review Focus sections.

Ask it to try each of these escape and cheat paths, **by running probes** (write throwaway
tests in its own temp directory):
- **Self-reconfiguration.** Can an agent change its own configuration or prompt between rounds?
  Consider project config, `.opencode`, `AGENTS.md`, env, and opencode's data home.
- **Grade tampering.** Can a workspace change alter the grade without being in the exported patch?
  Consider conftest, `pytest.ini`, `.pth` files, `sitecustomize.py`, symlinks, `__pycache__`, and
  files at hidden-test destinations.
- **Reviewer leakage.** Can the reviewer's copy leak changes into the workspace or the patch?
- **Malformed input.** Can a crafted patch or tar escape the grading tree? Consider `..`, absolute
  paths, symlinks, and the `git apply` behaviour.
- **Hiding work.** Can the agent hide changes from `changes()` (`.gitignore`, its own `.git`,
  `core.*` config), or make `patch()` include undeclared paths?
- **Import safety.** Can `cli import` be steered off the declared paths, or touch your checkout?
- **Secrets.** Can a key leak to argv, the logs or the records?
- **Scheduling failures.** Look for deadlocks, lost tasks, and a run that never ends, for example
  when a thread dies outside `_guarded`.

Have it write `.superpowers/sdd/workforce-review/review_workforce_harness.md`.

- [ ] **Step 2: Fix every CRITICAL and HIGH finding test-first**

For each one, write a failing test, watch it fail, fix, watch it pass, then re-run Task 1 Step 3.
Commit by path as `fix(workforce): <finding> (security review)`.

- [ ] **Step 3: Record MEDIUM and LOW findings**

Give each a disposition in the review file. Anything that depends on the VM goes to Plan B's
boundary proof.

---

### Task 3: Merge

- [ ] **Step 1:** You push `feat/workforce-harness` and merge it, or approve a PR merge. The final
  whole-branch review (executing-plans / subagent-driven) happens before this.
- [ ] **Step 2:** Record the merge commit in the ledger. Plans B and D reference
  `scripts/tools/workforce` on `main`.

---

## Self-review (done while writing)

- **Spec coverage.**
  - §5.1 workspace snapshot, strip, no history, answer-blob check, out-of-workspace config, subagents
    disabled: `bundle.py`, `workspace.py`, `profiles.py`.
  - §5.3 keys only via the environment: `profiles.opencode_env`.
  - §5.5 patch-only export, declared paths, rejected-path list, import to `workforce/<run>/<task>`,
    network-less grading with a harness-owned ini, `--noconftest` and `-p no:cacheprovider`:
    `paths.py`, `cli.import_patch`, `grade.py` (decision 3 for conftest).
  - §6 steps 1–6, including ≤2 rework rounds, the separate fix session and attribution:
    `pipeline.py`.
  - §7 loop metric: `loops.py`. The W1 rule: `analysis.w1_choose` (decision 2).
  - §9 measured fields, the validity tests this harness can see (harness exception, worker
    unreachable) and the decision rule: `pipeline.py`, `analysis.w3_decide`.
  - **Not covered here:** host reboot and router-restart validity, the prober, and GPU attribution
    belong to Plan D (decision 9). The VM, users, Docker image and egress belong to Plan B.
- **Placeholder scan.** Tasks apply verbatim code, and every command has an expected output.
  Task 2's fixes can't be written in advance; they follow the test-first protocol.
- **Type consistency.** CLI flags, `run.json` and `record.json` fields, and the `taskdef.json` keys
  match the appendix code; checked against `cli.py`, `pipeline._write_record` and
  `bundle.load_taskdef`.
- **Review Focus.** All five items need the real VM or model. Each names the plan and step that pins
  it, and items 1–3 are explicit inputs to Plan B.
