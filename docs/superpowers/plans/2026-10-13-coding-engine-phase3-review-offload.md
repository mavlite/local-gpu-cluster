# Coding Engine Phase 3 — Review Offload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Background tasks are reviewed on the three Minisforum CPU workers (round-robin, health-checked, one review per task, GPU does the one rework), each task gets a one-page report, and a 16-task run proves the gate costs at most 20% of S-arm wall time without losing acceptance.

**Architecture:** The workforce harness already runs arm G (GPU implementer + reviewer on the lead) with `--review-budget 1`. Phase 3 adds a *review pool*: one opencode reviewer agent per CPU worker (`reviewer-w1..3`), one review thread ("lane") per worker that only takes work while its worker is healthy, and the GPU lead as the fallback lane when no worker is up. A worker that dies mid-review is marked down and the review is retried elsewhere — never counted as a review round. The measurement window is first brought in line with Phase 1 (3 × 128K is now the production layout), because the done-check needs it.

**Tech Stack:** Python 3 (harness, pytest), Bash (host/guest scripts, pytest-driven fakes), opencode 1.18.34 in sandbox VM 176, llama.cpp router on LXC 153, Qwen3.6 on ik_llama.cpp on the CPU workers.

**Spec:** `docs/superpowers/specs/2026-10-12-local-coding-engine-design.md` (rev 1.2) — §3.2 (CPU node jobs), §3.3 B (background queue changes), §5 phase 3 row. Workforce background: `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md` §6 (review protocol), §9-§10 (windows, validity).

## Global Constraints

- Review budget 1: "one review, one rework, no lead fix" (spec §3.3 B) — runs pass `--review-budget 1`.
- Reviewer rules: "read-first + defects-only rules, one review, verdict feeds one GPU rework" (spec §3.2) — runs pass `--read-first --defects-only`.
- CPU workers: Qwen3.6 alias `qwen3.6` at `http://172.16.10.205:8090/v1`, `.206`, `.207`, key `WF_WORKER_KEY`; GPU lead `qwen3.8-nothink` via the router `http://192.168.6.153:8000/v1`, key `WF_ROUTER_KEY` (scoped key).
- The user lane stays free: background work may use at most 2 of the 3 GPU slots at any moment (one implementer + the fallback reviewer).
- Done-check (spec §5 row 3): "a 16-task run reaches S-arm wall time ± 20% with the review gate on, and acceptance ≥ the S mean". S baseline on the old build: qwen3.8 13/16 in 49 min (lt-9-s38); the plan re-measures S on the current build in the same window (Task 7).
- Keys never on argv, never printed, never in a harvest (existing key-leak check stays).
- Tests: `python -m pytest scripts/tools/workforce/tests -q` and `python -m pytest scripts/rag/tests scripts/files/tests scripts/tools/gate/tests -q`; `bash -n` on every edited script.
- Commits: conventional (`feat:`/`fix:`/`test:`/`docs:`), by explicit path, **no `Co-Authored-By` trailer**.
- Mutation-check guards with a matched replace and restore, never `git checkout`/`restore`/`stash`.

## Review Focus

1. A worker that dies or is stopped mid-review (rc ≠ 0, not a timeout): the review is retried on another healthy worker or the GPU, the dead worker is marked down, and it never burns the task's one review — Task 3, `test_a_worker_that_fails_mid_review_is_marked_down_and_the_review_retried_elsewhere`.
2. Every worker down for the whole run (Full mode started, or the agent tier paused): every review runs on the GPU lead and the run stays valid — Task 3, `test_all_workers_down_sends_every_review_to_the_gpu_lead_and_the_run_stays_valid`.
3. Session-resume turns (the tools-off verdict turn and the enforce-read re-read) must go to the same worker as the review they resume; a resumed session on another model is a different conversation — Task 3, `test_verdict_and_reread_turns_stay_on_the_reviewing_worker`.
4. A worker that flaps (fails, then answers the next health poll) must not ping-pong one task forever: two worker failures send that task to the GPU — Task 3, `test_two_worker_failures_send_the_task_to_the_gpu_lead`.
5. Opening the window while a swap profile is active (1 × 262K): refuse without touching chat; never reshape chat to reach 3 slots — Task 1, `test_open_refuses_a_non_production_layout_without_touching_anything`.

## Out of scope (rulings)

- **Grading on the runners.** The harness grades inside sandbox VM 176 (network-less Docker); the runners proved parity (Phase 2) but have no ssh and sit on VLAN 10, so remote grading needs a small authenticated grading service and a sandbox egress rule — a design of its own. Grading location does not change acceptance (parity 13/13) and the GPU, not grading, bounds wall time. Ruling: Phase 4 (queue front-end), recorded here so it is not lost.
- **Start-VCFLab pause flag / drain.** The health-checked pool already falls back to the GPU when workers stop, which is what Full mode needs functionally; the explicit drain (wait ≤ 10 min for in-flight reviews) belongs with the queue (Phase 4).
- **CPU shares for the workers.** Measured 2026-10-13 (VCF VM CPU ready 0.1% → 1-2.6% avg, peaks 6-9% under worker load); applying custom shares is a lab-ops decision for the owner, not a harness change.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `scripts/files/wf-window.sh` | modify | window open/close against the Phase 1 production layout |
| `scripts/files/tests/test_wf_window.py` | modify | window tests at capacity 3; new refusal + no-redteam tests |
| `scripts/tools/workforce/profiles.py` | modify | `review_pool` agents `reviewer-wN` / `reviewer-verdict-wN`; `keys_for(..., review_pool)` |
| `scripts/tools/workforce/tests/test_profiles.py` | modify | pool agent tests |
| `scripts/tools/workforce/pipeline.py` | modify | `HealthMonitor.is_up/mark_down`; review lanes; infra retry; lane in records |
| `scripts/tools/workforce/tests/wf_fixtures.py` | modify | fake maps `reviewer-*` agents; scripted `rc` / `sleep` reviews |
| `scripts/tools/workforce/tests/test_pipeline.py` | modify | lane tests |
| `scripts/tools/workforce/cli.py` | modify | `run --review-pool`; `report` command |
| `scripts/tools/workforce/tests/test_cli.py` | modify | wiring tests |
| `scripts/tools/workforce/report.py` | create | one markdown page per task + an index |
| `scripts/tools/workforce/tests/test_report.py` | create | report tests |
| `scripts/files/wf-run-control.sh`, `scripts/76-vm-wf-sandbox.sh` | modify | `--review-pool` through start/warm with the worker key on stdin |
| `scripts/files/tests/test_wf_run_control.py` | modify | pass-through and key tests |
| `docs/superpowers/workforce/phase3-review-offload.json` | create (Task 7) | measured S vs G-pool result |

---

### Task 1: Measurement window for the Phase 1 layout

The window was written when the normal layout was 1 × 262K: `open` called `redteam-mode-enter.sh` to reach 3 × 128K and `close` waited for capacity 1. Since Phase 1 (2026-10-13) the production chat **is** 3 × 131072 with vision, probabilistic drafting and b11577 flags; `redteam-mode-enter.sh` would restart chat with its own flags. New contract: `open` refuses unless chat is already in the production layout (capacity 3), never calls the redteam scripts, stops perturbers, ensures embed + rerank, and saves the capacity; `close` restores units and waits for the saved capacity.

**Files:**
- Modify: `scripts/files/wf-window.sh` (header comment, `WINDOW_CAPACITY`, `cmd_open`, `cmd_close`)
- Test: `scripts/files/tests/test_wf_window.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `bash scripts/files/wf-window.sh open|close|stamp|status` with the same CLI; env `WF_WIN_CAPACITY` (default 3). Task 7 uses it unchanged.

- [x] **Step 1: Update the fixture and expectations to the Phase 1 layout, and add the new tests**

In the `world` fixture change `"capacity": 1` to `"capacity": 3`. Every assertion that the normal layout is 1 becomes 3 (`test_close_puts_back_exactly_what_open_found`, `test_open_refuses_while_redteam_mode_is_active`, `test_a_failed_open_can_still_be_closed`, `test_stamp_records_boot_and_invocation_ids`, `test_open_refuses_while_a_timer_started_job_is_running`) — this follows the spec change, it does not weaken them. Add:

```python
def calls(tmp):
    p = tmp / "world.json.log"
    return p.read_text() if p.exists() else ""


def test_open_never_calls_the_redteam_scripts(world):
    run, state, _, tmp = world
    r = run("open")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "redteam-mode-enter.sh" not in calls(tmp) and "redteam-mode-exit.sh" not in calls(tmp)
    assert state()["capacity"] == 3


def test_open_refuses_a_non_production_layout_without_touching_anything(world):
    run, state, patch, tmp = world
    patch(capacity=1)                                   # a swap profile (1 x 262K) is active
    before = state()["units"]
    r = run("open")
    assert r.returncode != 0 and "production layout" in r.stderr
    assert state()["units"] == before and state()["capacity"] == 1
    assert "redteam-mode-enter.sh" not in calls(tmp)
    assert not (tmp / "state" / "window.env").exists()


def test_close_waits_for_the_capacity_open_found(world):
    run, state, patch, tmp = world
    assert run("open").returncode == 0
    assert "CAPACITY='3'" in (tmp / "state" / "window.env").read_text()
    r = run("close")
    assert r.returncode == 0, r.stdout + r.stderr
    assert state()["capacity"] == 3
```

- [x] **Step 2: Run the window tests to verify the new ones fail**

Run: `python -m pytest scripts/files/tests/test_wf_window.py -q`
Expected: FAIL — `open` dies with "not in the normal 1-slot layout" at capacity 3; `test_open_never_calls_the_redteam_scripts` fails on the enter call.

- [x] **Step 3: Implement the new open/close**

In `scripts/files/wf-window.sh` replace the `open` lines of the header comment with:

```bash
#   open    the production chat layout (WF_WIN_CAPACITY slots, default 3 -- 3 x 131072 since Phase 1,
#           2026-10-13) must already be in place: open never reshapes chat. It refuses any other layout
#           (a swap profile), stops every unit that could restart chat or swap its profile (the chat
#           restart timer, the redteam watcher/idle revert, the fast server, rag-refresh, the swap
#           webhook), makes sure embed + rerank are loaded, and saves what it found first.
#   close   put back exactly what `open` found, then wait for the capacity open found.
```

Below `WAIT_S=...` add:

```bash
WINDOW_CAPACITY="${WF_WIN_CAPACITY:-3}"
```

Replace `cmd_open` with:

```bash
cmd_open() {
  [ -f "$STATE" ] && die "window already open ($STATE) -- run close first"
  redteam_active && die "redteam mode is active -- refusing to open a window over an engagement"
  local cap
  cap="$(capacity)"
  [ "$cap" = "$WINDOW_CAPACITY" ] \
    || die "chat capacity is ${cap:-unknown}, not the ${WINDOW_CAPACITY}-slot production layout (a swap profile?) -- refusing to reshape chat"
  local busy
  for busy in "${BUSY_UNITS[@]}"; do
    [ "$(host_active "$busy")" = active ] && die "$busy is running -- wait for it to finish before opening a window"
  done
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  local u tmp
  tmp="$(mktemp "$STATE_DIR/.window.XXXXXX")"
  { for u in "${HOST_UNITS[@]}"; do echo "HOST_$(key_of "$u")='$(host_active "$u")'"; done
    for u in "${AMD_UNITS[@]}" "${RAG_UNITS[@]}"; do echo "AMD_$(key_of "$u")='$(amd_active "$u")'"; done
    echo "CAPACITY='$cap'"
    echo "OPENED_AT='$(date -u +%FT%TZ)'"
  } > "$tmp"
  mv "$tmp" "$STATE"
  log "saved the prior state to $STATE (close restores it, even after a failed open)"
  date +%s > "$RT_LAST"            # the redteam idle check must not act during the window
  stop_perturbers
  local left
  left="$(active_perturbers | tr '\n' ' ')"
  [ -z "${left// }" ] || die "perturbers still active after stop: $left"
  pct exec "$AMD" -- systemctl start "${RAG_UNITS[@]}"
  wait_layout "$WINDOW_CAPACITY" yes
  log "open: ${WINDOW_CAPACITY}-slot production chat as found, embed + rerank loaded, perturbers stopped"
}
```

In `cmd_close` replace `wait_layout 1 any` with:

```bash
  wait_layout "${CAPACITY:-$WINDOW_CAPACITY}" any
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/files/tests/test_wf_window.py -q` then `bash -n scripts/files/wf-window.sh`
Expected: all pass; syntax OK.

- [x] **Step 5: Mutation-check the refusal**

Matched replace of `[ "$cap" = "$WINDOW_CAPACITY" ] \` with `true \`, run the window tests (expect `test_open_refuses_a_non_production_layout_without_touching_anything` to FAIL), restore the exact original text, re-run (all pass).

- [x] **Step 6: Commit**

```bash
git add scripts/files/wf-window.sh scripts/files/tests/test_wf_window.py
git commit -m "fix(window): open against the Phase 1 3-slot production layout, never via the redteam scripts"
```

---

### Task 2: Review-pool agents in the opencode config

**Files:**
- Modify: `scripts/tools/workforce/profiles.py` (`build_config`, `keys_for`)
- Test: `scripts/tools/workforce/tests/test_profiles.py`

**Interfaces:**
- Consumes: existing `_provider`, `_agent`, `REVIEWER_PROMPT`, `READ_FIRST_RULE`, `DEFECTS_ONLY_RULE`.
- Produces: `build_config(arm, router_url, worker_urls, ..., review_pool=False)` adds, for each worker i (1-based), agents `f"reviewer-w{i}"` and `f"reviewer-verdict-w{i}"` identical to `reviewer` / `reviewer-verdict` except `model = f"w{i}/{walias}"`; `reviewer` / `reviewer-verdict` stay on the lead (the fallback lane). `keys_for(arm, lead_url=None, review_pool=False)` returns `KEY_ENV` (both keys) when `review_pool`. Task 3 uses the agent names; Task 4 calls both.

- [x] **Step 1: Write the failing tests**

Append to `test_profiles.py`:

```python
W = ["http://172.16.10.205:8090/v1", "http://172.16.10.206:8090/v1", "http://172.16.10.207:8090/v1"]


def test_review_pool_adds_one_reviewer_and_verdict_agent_per_worker_and_keeps_the_lead_fallback():
    cfg = profiles.build_config("G", "http://r/v1", W, review_pool=True, read_first=True, defects_only=True)
    a = cfg["agent"]
    for i in (1, 2, 3):
        assert a[f"reviewer-w{i}"]["model"] == f"w{i}/qwen3.6"
        assert a[f"reviewer-verdict-w{i}"]["model"] == f"w{i}/qwen3.6"
        assert a[f"reviewer-w{i}"]["prompt"] == a["reviewer"]["prompt"]          # same rules as the lead reviewer
        assert a[f"reviewer-w{i}"]["permission"] == a["reviewer"]["permission"]
        assert a[f"reviewer-verdict-w{i}"]["permission"] == a["reviewer-verdict"]["permission"]
        assert cfg["provider"][f"w{i}"]["options"]["baseURL"] == W[i - 1]
    assert a["reviewer"]["model"] == "router/qwen3.8-nothink" and a["impl-1"]["model"] == "router/qwen3.8-nothink"
    assert "READ" in a["reviewer-w1"]["prompt"].upper()


@pytest.mark.parametrize("kw,msg", [
    (dict(arm="T"), "arm G"),
    (dict(arm="G", worker_urls=[]), "worker"),
    (dict(arm="G", reviewer_model="w1/qwen3.6"), "reviewer_model"),
    (dict(arm="G", reviewer_temperature=0.2), "temperature"),
])
def test_review_pool_refuses_combinations_it_does_not_support(kw, msg):
    args = dict(arm="G", router_url="http://r/v1", worker_urls=W, review_pool=True)
    args.update(kw)
    with pytest.raises(ValueError, match=msg):
        profiles.build_config(**args)


def test_a_review_pool_run_gets_the_worker_key():
    assert profiles.keys_for("G", review_pool=True) == profiles.KEY_ENV
    assert profiles.keys_for("G") == ("WF_ROUTER_KEY",)
```

(If `pytest` is not yet imported in `test_profiles.py`, add `import pytest` at the top.)

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest scripts/tools/workforce/tests/test_profiles.py -q`
Expected: FAIL — `build_config() got an unexpected keyword argument 'review_pool'`.

- [x] **Step 3: Implement**

Change the signature and add the checks at the top of `build_config`:

```python
def build_config(arm, router_url, worker_urls, reviewer_model=None, read_first=False, worker_alias=None,
                 review_steps=None, lead_url=None, lead_alias=None, reviewer_temperature=None, defects_only=False,
                 review_pool=False):
    """... (keep the docstring) `review_pool` (arm G, coding engine phase 3): one reviewer + verdict agent
    per worker (`reviewer-wN`, `reviewer-verdict-wN`) for the harness's review lanes; `reviewer` stays on the
    lead as the fallback lane."""
    if arm not in ("T", "G", "S"):                   # S: the GPU implementer alone (round 2 §5.2)
        raise ValueError("arm must be T, G or S")
    if review_pool:
        if arm != "G":
            raise ValueError("review_pool is for arm G (GPU implementer, CPU reviewers)")
        if not worker_urls:
            raise ValueError("review_pool needs worker URLs")
        if reviewer_model:
            raise ValueError("review_pool and reviewer_model are exclusive")
        if reviewer_temperature is not None:
            raise ValueError("review_pool does not support reviewer_temperature")
```

After the `else:` branch that adds `impl-1` and the worker providers (just before `cfg = {...}`), add:

```python
    if review_pool:
        for i in range(1, len(worker_urls) + 1):
            agents[f"reviewer-w{i}"] = dict(agents["reviewer"], model=f"w{i}/{walias}")
            agents[f"reviewer-verdict-w{i}"] = dict(agents["reviewer-verdict"], model=f"w{i}/{walias}")
```

Replace `keys_for`:

```python
def keys_for(arm, lead_url=None, review_pool=False):
    """The keys an arm's agents need: arm G has no CPU workers, so no worker key -- unless the lead runs on
    a keyed server of its own (the orchestration trial) or the reviews run on the workers (review pool)."""
    return KEY_ENV if arm == "T" or lead_url or review_pool else ("WF_ROUTER_KEY",)
```

- [x] **Step 4: Run the tests**

Run: `python -m pytest scripts/tools/workforce/tests/test_profiles.py -q`
Expected: PASS (all, including the existing ones).

- [x] **Step 5: Commit**

```bash
git add scripts/tools/workforce/profiles.py scripts/tools/workforce/tests/test_profiles.py
git commit -m "feat(workforce): review-pool agents - one reviewer and verdict agent per CPU worker"
```

---

### Task 3: Review lanes in the pipeline

**Files:**
- Modify: `scripts/tools/workforce/pipeline.py` (`HealthMonitor`, `_Task`, `Pipeline.__init__`, `_lead_loop` → `_lane_loop`, `_review`, `_record`, `run`)
- Modify: `scripts/tools/workforce/tests/wf_fixtures.py` (`FakeOpencode.run`)
- Test: `scripts/tools/workforce/tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 2's agent names `reviewer-w{i}` / `reviewer-verdict-w{i}`.
- Produces: `Pipeline(..., review_lanes=None)` where `review_lanes` is a list of `(lane, url)` like `[("w1", "http://172.16.10.205:8090/v1"), ...]` and requires `monitor`; `HealthMonitor.is_up(url) -> bool`, `HealthMonitor.mark_down(url) -> None`; each review entry carries `"lane"` (`"w1"`..`"w3"` or `"lead"`); each record carries `"review_infra": [{"lane", "rc", "t"}]`; `run.json` carries `"review_lanes": {lane: count}` and `"review_infra": int`. Task 4 constructs it; Task 6 reads the records.

- [x] **Step 1: Teach the fake about pool agents, failed and slow reviews**

In `wf_fixtures.py` `FakeOpencode.run`, replace the `role = {...}.get(agent, "impl")` line with:

```python
        role = ("verdict" if agent.startswith("reviewer-verdict") else "review" if agent.startswith("reviewer")
                else "fix" if agent == "fixer" else "impl")
```

and at the top of the `if isinstance(entry, dict):` branch for `role == "review"` add:

```python
                if entry.get("sleep"):
                    time.sleep(entry["sleep"])
                if entry.get("rc"):                            # the worker died: no session, rc != 0
                    return oc.RunResult(entry["rc"], False, None, "", 0.01)
```

(Add `import time` to `wf_fixtures.py` if missing; update the class docstring: review entries may carry `"rc"` and `"sleep"`.)

- [x] **Step 2: Write the failing tests**

Append to `test_pipeline.py`:

```python
LANES = [("w1", "http://w1/v1"), ("w2", "http://w2/v1"), ("w3", "http://w3/v1")]


class PoolMonitor:
    """Current health per URL. sticky=True: mark_down is recorded but the next poll says the worker is up
    again (a flapping worker)."""

    def __init__(self, up, unreachable=None, sticky=False):
        self.up, self.marked, self.sticky = dict(up), [], sticky
        self.unreachable = unreachable or {}

    def start(self):
        return self

    def stop(self):
        return {"max_unreachable_s": {u: self.unreachable.get(u, 0.0) for u in self.up}}

    def is_up(self, url):
        return self.up.get(url, False)

    def mark_down(self, url):
        self.marked.append(url)
        if not self.sticky:
            self.up[url] = False


def all_up():
    return PoolMonitor({u: True for _, u in LANES})


def review_agents(fake):
    return [c["agent"] for c in fake.calls if c["role"] == "review"]


def test_pool_reviews_run_on_worker_lanes_and_record_the_lane(tmp_path):
    script = {t: {"impl": [{f"pkg/{t}.py": SOL}], "review": ["ACCEPT"]} for t in ("pa", "pb")}
    summary, fake = run_pipeline(tmp_path, "G", script, monitor=all_up(), review_lanes=LANES, review_budget=1)
    assert set(review_agents(fake)) <= {"reviewer-w1", "reviewer-w2", "reviewer-w3"}
    for t in ("pa", "pb"):
        assert record(tmp_path, t)["reviews"][0]["lane"] in ("w1", "w2", "w3")
    assert summary["valid"] and summary["accepted"] == 2
    assert sum(summary["review_lanes"].values()) == 2 and summary["review_infra"] == 0


def test_reviews_spread_over_the_lanes_while_one_is_busy(tmp_path):
    script = {t: {"impl": [{f"pkg/{t}.py": SOL}], "review": [{"text": "ACCEPT", "sleep": 0.6}]}
              for t in ("sa", "sb", "sc")}
    summary, fake = run_pipeline(tmp_path, "G", script, monitor=all_up(), review_lanes=LANES, review_budget=1)
    assert len({record(tmp_path, t)["reviews"][0]["lane"] for t in ("sa", "sb", "sc")}) >= 2
    assert summary["accepted"] == 3


def test_all_workers_down_sends_every_review_to_the_gpu_lead_and_the_run_stays_valid(tmp_path):
    mon = PoolMonitor({u: False for _, u in LANES}, unreachable={u: 3600.0 for _, u in LANES})
    script = {"da": {"impl": [{"pkg/da.py": SOL}], "review": ["ACCEPT"]}}
    summary, fake = run_pipeline(tmp_path, "G", script, monitor=mon, review_lanes=LANES, review_budget=1)
    assert review_agents(fake) == ["reviewer"]
    assert record(tmp_path, "da")["reviews"][0]["lane"] == "lead"
    assert summary["valid"], summary["invalid_reasons"]                 # the fallback is the design, not a fault
    assert summary["health"]["max_unreachable_s"]["http://w1/v1"] == 3600.0


def test_a_worker_that_fails_mid_review_is_marked_down_and_the_review_retried_elsewhere(tmp_path):
    mon = PoolMonitor({"http://w1/v1": True, "http://w2/v1": False, "http://w3/v1": False})
    script = {"fa": {"impl": [{"pkg/fa.py": SOL}], "review": [{"rc": 1, "text": ""}, "ACCEPT"]}}
    summary, fake = run_pipeline(tmp_path, "G", script, monitor=mon, review_lanes=LANES, review_budget=1)
    r = record(tmp_path, "fa")
    assert review_agents(fake) == ["reviewer-w1", "reviewer"]
    assert mon.marked == ["http://w1/v1"]
    assert len(r["reviews"]) == 1 and r["reviews"][0]["lane"] == "lead"     # the failure is not a review round
    assert r["review_infra"][0]["lane"] == "w1" and r["review_infra"][0]["rc"] == 1
    assert r["outcome"] == "implementer-accepted" and r["rework_rounds"] == 0
    assert summary["review_infra"] == 1 and summary["valid"]


def test_two_worker_failures_send_the_task_to_the_gpu_lead(tmp_path):
    mon = PoolMonitor({u: True for _, u in LANES}, sticky=True)              # flapping: back up at every poll
    script = {"ff": {"impl": [{"pkg/ff.py": SOL}],
                     "review": [{"rc": 1, "text": ""}, {"rc": 1, "text": ""}, "ACCEPT"]}}
    summary, fake = run_pipeline(tmp_path, "G", script, monitor=mon, review_lanes=LANES, review_budget=1)
    agents = review_agents(fake)
    assert len(agents) == 3 and agents[-1] == "reviewer" and all(a.startswith("reviewer-w") for a in agents[:2])
    assert record(tmp_path, "ff")["reviews"][0]["lane"] == "lead"


def test_verdict_and_reread_turns_stay_on_the_reviewing_worker(tmp_path):
    mon = PoolMonitor({"http://w1/v1": False, "http://w2/v1": True, "http://w3/v1": False})
    # va: capped review -> verdict turn ACCEPT -> enforce-read re-read (no read tools) ACCEPT
    script = {"va": {"impl": [{"pkg/va.py": SOL}], "review": [{"text": "I looked around.", "steps": 20}, "ACCEPT"],
                     "verdict": ["ACCEPT"]},
              "vb": {"impl": [{"pkg/vb.py": SOL}], "review": ["ACCEPT", "ACCEPT"]}}
    run_pipeline(tmp_path, "G", script, monitor=mon, review_lanes=LANES, review_budget=1, enforce_read=True)
    va = record(tmp_path, "va")["reviews"]
    assert va[0]["lane"] == "w2" and va[0]["verdict_turn"]["verdict"] == "ACCEPT"
    rec_vb = record(tmp_path, "vb")
    assert rec_vb["reviews"][0]["reread"] is not None                       # the packet-only ACCEPT was re-read


def test_verdict_and_reread_agents_match_the_lane(tmp_path):
    mon = PoolMonitor({"http://w1/v1": False, "http://w2/v1": True, "http://w3/v1": False})
    script = {"vc": {"impl": [{"pkg/vc.py": SOL}], "review": [{"text": "hmm", "steps": 20}, "ACCEPT"],
                     "verdict": ["ACCEPT"]}}
    _, fake = run_pipeline(tmp_path, "G", script, monitor=mon, review_lanes=LANES, review_budget=1,
                           enforce_read=True)
    by_role = [(c["role"], c["agent"], c["resumed"]) for c in fake.calls if c["role"] in ("review", "verdict")]
    assert ("verdict", "reviewer-verdict-w2", True) in by_role
    assert all(a.endswith("w2") for r, a, _ in by_role)


def test_review_lanes_need_a_monitor(tmp_path):
    with pytest.raises(ValueError, match="monitor"):
        pipeline.Pipeline("G", [], FakeOpencode({}), str(tmp_path / "run"), grade.LocalRunner(),
                          review_lanes=LANES)


def test_health_monitor_current_state_and_mark_down():
    state = {"http://w1/v1": True}
    clock = iter(range(100)).__next__
    m = pipeline.HealthMonitor(["http://w1/v1"], probe=lambda u: state[u], clock=clock)
    m.poll_once()
    assert m.is_up("http://w1/v1")
    m.mark_down("http://w1/v1")
    assert not m.is_up("http://w1/v1")
    m.poll_once()                                                          # the next good poll restores it
    assert m.is_up("http://w1/v1")
    state["http://w1/v1"] = False
    m.poll_once()
    assert not m.is_up("http://w1/v1")
```

(Add `import pytest` to `test_pipeline.py` if missing.)

- [x] **Step 3: Run them to verify they fail**

Run: `python -m pytest scripts/tools/workforce/tests/test_pipeline.py -q`
Expected: FAIL — `unexpected keyword argument 'review_lanes'` and `HealthMonitor has no attribute 'is_up'`.

- [x] **Step 4: Implement**

Module constants next to `UNREACHABLE_LIMIT_S`:

```python
LANE_POLL_S = 5.0          # review lanes re-check worker health this often (health changes do not notify)
MAX_REVIEW_INFRA = 2       # worker failures on one task before its review goes to the GPU lead
```

`HealthMonitor`: add a lock and the two methods (keep everything else):

```python
    def __init__(self, urls, interval=30.0, timeout=5.0, probe=None, clock=time.monotonic):
        ...                                           # existing lines
        self._lock = threading.Lock()

    def poll_once(self):
        now = self.clock()
        for u in self.urls:
            ok = self.probe(u)
            with self._lock:
                if ok:
                    self._down_since.pop(u, None)
                else:
                    since = self._down_since.setdefault(u, now)
                    self.max_down[u] = max(self.max_down[u], now - since)

    def is_up(self, url):
        """Current state: up unless the last poll failed or a caller saw it fail since."""
        with self._lock:
            return url not in self._down_since

    def mark_down(self, url):
        """A request to `url` just failed: treat it as down until the next successful poll."""
        with self._lock:
            self._down_since.setdefault(url, self.clock())
```

`_Task`: add two fields after `final`:

```python
    review_infra: list = field(default_factory=list)   # worker failures during a review (never a round)
    force_lead: bool = False                           # MAX_REVIEW_INFRA reached: review on the GPU lead
```

`Pipeline.__init__`: add `review_lanes=None` to the signature and, after `self.review_budget = review_budget`:

```python
        self.review_lanes = list(review_lanes or [])        # [(lane, url)]: coding engine phase 3
        if self.review_lanes and monitor is None:
            raise ValueError("review_lanes need a health monitor")
```

Replace `_lead_loop` with:

```python
    def _eligible(self, t, lane, url):
        if lane == "lead":
            return t.force_lead or not any(self.monitor.is_up(u) for _, u in self.review_lanes)
        return not t.force_lead and self.monitor.is_up(url)

    def _lane_loop(self, lane, url):
        """One review lane. Without review lanes the single "lead" lane takes every review (arms T/G as
        before); with them, a worker lane takes reviews while its worker is up and the lead lane only takes
        a task forced to it or any task while no worker is up -- it never competes with healthy workers."""
        while True:
            with self.cond:
                while True:
                    if self._finished():
                        return
                    i = next((k for k, t in enumerate(self.lead_q) if self._eligible(t, lane, url)), None)
                    if i is not None:
                        t = self.lead_q.pop(i)
                        break
                    self.cond.wait(timeout=LANE_POLL_S if self.review_lanes else None)
            self._guarded(t, self._review, t, lane, url)
```

`_review`: change the signature to `def _review(self, t, lane="lead", url=None):`, pick the agents at the top:

```python
        ragent = "reviewer" if lane == "lead" else f"reviewer-{lane}"
        vagent = "reviewer-verdict" if lane == "lead" else f"reviewer-verdict-{lane}"
```

use `ragent` in the first `self.oc.run(...)` and in the re-read `self.oc.run(...)`, `vagent` in the verdict-turn call. Directly after the first `r = self._run_as(...)` add:

```python
        if lane != "lead" and r.rc != 0 and not r.timed_out:
            # The worker failed (stopped, crashed, unreachable): not a review. Mark it down, put the task back
            # at the front of the queue, and after MAX_REVIEW_INFRA such failures send it to the GPU lead.
            shutil.rmtree(tree, ignore_errors=True)
            self.monitor.mark_down(url)
            t.review_infra.append({"lane": lane, "rc": r.rc, "t": time.time()})
            if len(t.review_infra) >= MAX_REVIEW_INFRA:
                t.force_lead = True
            with self.cond:
                self.lead_q.insert(0, t)
                self.cond.notify_all()
            return
```

and add `"lane": lane` to the `entry = {...}` dict.

`_record`: add `"review_infra": t.review_infra,` to the returned dict.

`run()`: replace the review-thread line with

```python
        if self.review:
            for lane, url in self.review_lanes + [("lead", None)]:
                threads.append(threading.Thread(target=self._lane_loop, args=(lane, url), daemon=True))
```

make the unreachable rule apply only without lanes:

```python
        if not self.review_lanes:                       # with lanes, an unreachable worker is handled by fallback
            invalid += [f"worker {u} unreachable for {s:.0f} s" for u, s in health["max_unreachable_s"].items()
                        if s >= UNREACHABLE_LIMIT_S]
```

and add to `summary`:

```python
                   "review_lanes": {ln: sum(1 for r in recs for rv in r["reviews"] if rv.get("lane") == ln)
                                    for ln in [x for x, _ in self.review_lanes] + ["lead"]},
                   "review_infra": sum(len(r.get("review_infra", [])) for r in recs),
```

- [x] **Step 5: Run the pipeline tests, then the whole harness suite**

Run: `python -m pytest scripts/tools/workforce/tests/test_pipeline.py -q` then `python -m pytest scripts/tools/workforce/tests -q`
Expected: PASS everywhere (existing T/G/S tests unchanged: without lanes the single "lead" lane behaves like the old `_lead_loop`).

- [x] **Step 6: Mutation-check the three guards**

One at a time, matched replace + run `test_pipeline.py` + restore the exact text:
- `if lane != "lead" and r.rc != 0 and not r.timed_out:` → `if False:` (expect the mid-review failure test to FAIL);
- `return t.force_lead or not any(` → `return not any(` (expect `test_two_worker_failures_send_the_task_to_the_gpu_lead` to FAIL);
- `ragent = "reviewer" if lane == "lead" else f"reviewer-{lane}"` → `ragent = "reviewer"` (expect the lane-agent tests to FAIL).

- [x] **Step 7: Commit**

```bash
git add scripts/tools/workforce/pipeline.py scripts/tools/workforce/tests/wf_fixtures.py scripts/tools/workforce/tests/test_pipeline.py
git commit -m "feat(workforce): review lanes - reviews on healthy CPU workers, GPU lead as fallback, worker failures retried"
```

---

### Task 4: `run --review-pool`

**Files:**
- Modify: `scripts/tools/workforce/cli.py` (`build_opencode`, `run` parser + handler)
- Test: `scripts/tools/workforce/tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2 `build_config(..., review_pool=)`, `keys_for(..., review_pool=)`; Task 3 `Pipeline(..., review_lanes=)`, `HealthMonitor`.
- Produces: `cli.py run --arm G --review-pool --worker URL [--worker URL ...] --review-budget 1 --read-first --defects-only ...`. Task 5 passes these flags from the guest.

- [x] **Step 1: Write the failing tests**

```python
def test_run_review_pool_wires_lanes_monitor_and_both_keys(tmp_path, monkeypatch):
    seen = {}

    class FakePipeline:
        def __init__(self, arm, bundles, opencode, out, grader, review=True, monitor=None, own=None, **kw):
            seen.update(arm=arm, review=review, monitor=monitor, lanes=kw.get("review_lanes"),
                        budget=kw.get("review_budget"))

        def run(self):
            return {"arm": seen["arm"]}

    def fake_build(arm, d, router, workers, cmd, env, launcher=None, **kw):
        seen.update(build_pool=kw.get("review_pool"), build_workers=list(workers))
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    w = ["http://172.16.10.205:8090/v1", "http://172.16.10.206:8090/v1"]
    argv = ["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"), "--router", "http://x/v1",
            "--review-pool", "--worker", w[0], "--worker", w[1], "--review-budget", "1"]
    assert cli.main(argv, env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"}) == 0
    assert seen["build_pool"] is True and seen["build_workers"] == w
    assert seen["lanes"] == [("w1", w[0]), ("w2", w[1])] and seen["budget"] == 1
    assert isinstance(seen["monitor"], cli.pipeline.HealthMonitor) and seen["review"] is True


@pytest.mark.parametrize("extra,env,msg", [
    (["--review-pool", "--worker", "http://w/v1"], {"WF_ROUTER_KEY": "k"}, "WF_WORKER_KEY"),
    (["--review-pool"], {"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w"}, "--worker"),
])
def test_run_review_pool_refuses_missing_inputs(tmp_path, extra, env, msg):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1", *extra], env=dict(env, WF_OPENCODE="opencode"))
    assert msg in str(e.value)


def test_run_review_pool_is_arm_g_only(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--arm", "S", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1", "--review-pool", "--worker", "http://w/v1"],
                 env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"})
    assert "arm G" in str(e.value)
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest scripts/tools/workforce/tests/test_cli.py -q -k review_pool`
Expected: FAIL — `unrecognized arguments: --review-pool`.

- [x] **Step 3: Implement**

`build_opencode`: add `review_pool=False` to the signature, pass `review_pool=review_pool` into `profiles.build_config(...)`, and use `keys=keys or profiles.keys_for(arm, lead_url=lead_url, review_pool=review_pool)`.

`run` parser (next to `--review-budget`):

```python
    p.add_argument("--review-pool", action="store_true",
                   help="arm G: reviews on the --worker CPU nodes (one lane each, health-checked), the GPU lead "
                        "as fallback (coding engine phase 3)")
```

In the `run` handler, before the existing key check:

```python
        if a.review_pool:
            if a.arm != "G":
                raise SystemExit("--review-pool is for arm G (GPU implementer, CPU reviewers)")
            if not a.worker:
                raise SystemExit("--review-pool needs at least one --worker URL")
            if not env.get("WF_WORKER_KEY"):
                raise SystemExit("WF_WORKER_KEY must be set for --review-pool")
```

pass `review_pool=a.review_pool` to `build_opencode(...)`, and replace the monitor/pipeline lines with:

```python
        monitor = pipeline.HealthMonitor(a.worker) if a.arm == "T" or a.review_pool else None
        lanes = [(f"w{i}", u) for i, u in enumerate(a.worker, start=1)] if a.review_pool else None
        out = pipeline.Pipeline(a.arm, bundles, opencode, a.out, grader, review=not a.w1 and a.arm != "S",
                                monitor=monitor, own=own, review_steps=a.review_steps,
                                enforce_read=a.enforce_read, review_budget=a.review_budget,
                                review_lanes=lanes).run()
```

- [x] **Step 4: Run the harness suite**

Run: `python -m pytest scripts/tools/workforce/tests -q`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add scripts/tools/workforce/cli.py scripts/tools/workforce/tests/test_cli.py
git commit -m "feat(workforce): run --review-pool wires the CPU review lanes and the worker key"
```

---

### Task 5: `--review-pool` through the sandbox (run-control + 76)

The guest owns the worker list (`WORKERS`, `WORKER_PORT`), exactly as for arm T, so the host passes only the flag; the worker key goes on stdin.

**Files:**
- Modify: `scripts/files/wf-run-control.sh` (`check_opts`, `cmd_start`, `cmd_exec`, `cmd_warm`)
- Modify: `scripts/76-vm-wf-sandbox.sh` (`start-run`, `warm`)
- Test: `scripts/files/tests/test_wf_run_control.py`

**Interfaces:**
- Consumes: Task 4's CLI flags.
- Produces: `76-vm-wf-sandbox.sh start-run <id> G --review-pool [--review-budget=1 --read-first --defects-only]` and `76-vm-wf-sandbox.sh warm G --review-pool`. Task 7 runs them.

- [x] **Step 1: Write the failing tests** (in `test_wf_run_control.py`, using the module's `guest` fixture and `KEYS_T`)

```python
def test_review_pool_is_arm_g_with_the_worker_key_and_the_guest_worker_list(guest):
    run, _, _, tmp = guest
    assert run("start rp-1 G --review-pool --review-budget=1", "WF_ROUTER_KEY=r\n").returncode != 0   # no worker key
    assert run("start rp-2 S --review-pool", KEYS_T).returncode != 0                                    # not arm G
    assert run("start rp-3 G --review-pool --review-budget=1 --read-first --defects-only", KEYS_T).returncode == 0
    assert run("_exec rp-3 G --review-pool --review-budget=1 --read-first --defects-only").returncode == 0
    dump = json.loads((tmp / "dump.json").read_text())
    argv = dump["argv"]
    assert "--review-pool" in argv and argv.count("--worker") == 3 and dump["worker"]
    assert argv[argv.index("--review-budget") + 1] == "1"


def test_warm_with_review_pool_touches_the_workers_too(guest):
    run, log, _, _ = guest
    r = run("warm G --review-pool", KEYS_T)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("warm ") == 4          # lead + three workers
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest scripts/files/tests/test_wf_run_control.py -q -k review_pool`
Expected: FAIL — `unknown option '--review-pool'`.

- [x] **Step 3: Implement**

`check_opts`: add `|| "$o" == --review-pool` to the accepted list and `--review-pool` to the error text. Add next to `has_lead_opt`:

```bash
has_pool_opt() { [[ " $* " == *" --review-pool "* ]]; }
```

`cmd_start`, after the `has_lead_opt` line:

```bash
  ! has_pool_opt "$@" || [ "$arm" = G ] || die "--review-pool is for arm G"
  ! has_pool_opt "$@" || [ -n "$KEY_WORKER" ] || die "--review-pool needs WF_WORKER_KEY on stdin"
```

`cmd_exec`: replace `if [ "$arm" = T ]; then` with `if [ "$arm" = T ] || has_pool_opt "$@"; then` (the `--review-pool` flag itself already passes through the `*) args+=("$o")` case).

`cmd_warm`: replace the whole function with

```bash
cmd_warm() {
  local arm="${1:-}" pool="${2:-}" w ok=0 workers=false
  [[ "$arm" == T || "$arm" == G || "$arm" == S ]] || die "arm must be T, G or S"
  [[ -z "$pool" || ( "$pool" == --review-pool && "$arm" == G ) ]] || die "usage: warm <T|G|S> | warm G --review-pool"
  [[ "$arm" == T || -n "$pool" ]] && workers=true
  read_keys
  ! $workers || [ -n "$KEY_WORKER" ] || die "arm T / --review-pool needs WF_WORKER_KEY on stdin"
  warm_one lead "$ROUTER_URL" "$KEY_ROUTER" "$LEAD_ALIAS" || ok=1
  if $workers; then
    for w in $WORKERS; do warm_one "$w" "http://$w:$WORKER_PORT/v1" "$KEY_WORKER" "$WORKER_ALIAS" || ok=1; done
  fi
  return $ok
}
```

and update the usage line to `warm <T|G|S> [--review-pool]`.

`76-vm-wf-sandbox.sh` `start-run`: change the worker-key requirement and the stdin line to cover the pool:

```bash
    pool=false; [[ " ${*:4} " == *" --review-pool "* ]] && pool=true
    [[ "$arm" == G || "$arm" == S ]] && ! $pool || [[ -s "$keys/worker.key" ]] || die "$keys/worker.key missing (arm T / --review-pool)"
    ...
      if [[ "$arm" == T || " ${*:4} " == *" --lead-url="* ]] || $pool; then printf 'WF_WORKER_KEY=%s\n' "$(tr -d '[:space:]' < "$keys/worker.key")"; fi
```

`warm`: replace the case body with

```bash
    arm="${2:-}"; pool="${3:-}"
    [[ "$arm" == T || "$arm" == G || "$arm" == S ]] || die "usage: 76-vm-wf-sandbox.sh warm <T|G|S> [--review-pool]"
    [[ -z "$pool" || "$pool" == --review-pool ]] || die "usage: 76-vm-wf-sandbox.sh warm <T|G|S> [--review-pool]"
    keys="${WF_KEY_DIR:-/root/wf/keys}"
    [[ -s "$keys/router.key" ]] || die "$keys/router.key missing"
    [[ "$arm" != T && -z "$pool" ]] || [[ -s "$keys/worker.key" ]] || die "$keys/worker.key missing (arm T / --review-pool)"
    {
      printf 'WF_ROUTER_KEY=%s\n' "$(tr -d '[:space:]' < "$keys/router.key")"
      if [[ "$arm" == T || -n "$pool" ]]; then printf 'WF_WORKER_KEY=%s\n' "$(tr -d '[:space:]' < "$keys/worker.key")"; fi
    } | vm_run_stdin 900 /usr/local/sbin/wf-run-control warm "$arm" ${pool:+"$pool"}
    ;;
```

- [x] **Step 4: Run the tests and syntax checks**

Run: `python -m pytest scripts/files/tests/test_wf_run_control.py -q` then `bash -n scripts/files/wf-run-control.sh && bash -n scripts/76-vm-wf-sandbox.sh`
Expected: PASS; syntax OK.

- [x] **Step 5: Commit**

```bash
git add scripts/files/wf-run-control.sh scripts/76-vm-wf-sandbox.sh scripts/files/tests/test_wf_run_control.py
git commit -m "feat(sandbox): --review-pool through start-run and warm, worker key on stdin"
```

---

### Task 6: Per-ticket report

**Files:**
- Create: `scripts/tools/workforce/report.py`
- Modify: `scripts/tools/workforce/cli.py` (`report` subcommand)
- Test: `scripts/tools/workforce/tests/test_report.py`

**Interfaces:**
- Consumes: `<run>/run.json`, `<run>/tasks/<id>/record.json` (fields from Task 3: `reviews[].lane`, `reviews[].verdict`, `reviews[].feedback`, `review_infra`; existing: `outcome`, `accepted`, `grade`, `rework_rounds`, `dropped`, `t_start`, `t_end`), `<run>/patches/<id>.patch`.
- Produces: `report.write(run_dir, out_dir) -> list[str]` (paths written: one `<id>.md` per task + `index.md`); `cli.py report --run DIR --out DIR`.

- [x] **Step 1: Write the failing tests**

```python
"""Per-ticket report: one page a human or Claude reads to decide on a background task's branch (spec §3.3 B)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import report  # noqa: E402


def make_run(root, tasks):
    os.makedirs(os.path.join(root, "patches"))
    for tid, rec in tasks.items():
        d = os.path.join(root, "tasks", tid)
        os.makedirs(d)
        with open(os.path.join(d, "record.json"), "w") as f:
            json.dump(dict({"id": tid, "dropped": [], "rework_rounds": 0, "review_infra": [], "reviews": [],
                            "t_start": 0.0, "t_end": 90.0, "grade": {"pass": True, "summary": "3 passed"}}, **rec), f)
        with open(os.path.join(root, "patches", f"{tid}.patch"), "w") as f:
            f.write(f"diff --git a/pkg/{tid}.py b/pkg/{tid}.py\n")
    with open(os.path.join(root, "run.json"), "w") as f:
        json.dump({"arm": "G", "wall_s": 600.0, "accepted": sum(1 for r in tasks.values() if r["accepted"]),
                   "n_tasks": len(tasks), "valid": True, "review_lanes": {"w1": 1, "lead": 1}}, f)
    return root


def test_one_page_per_task_with_outcome_review_lane_and_tests(tmp_path):
    run = make_run(str(tmp_path / "run"), {
        "a": {"outcome": "implementer-accepted", "accepted": True,
              "reviews": [{"lane": "w1", "verdict": "ACCEPT", "feedback": ""}]},
        "b": {"outcome": "reworked-unreviewed", "accepted": False, "rework_rounds": 1,
              "grade": {"pass": False, "summary": "1 failed, 2 passed"},
              "reviews": [{"lane": "lead", "verdict": "REVISE", "feedback": "f(-3) must be -6"}],
              "review_infra": [{"lane": "w2", "rc": 1, "t": 1.0}]}})
    paths = report.write(run, str(tmp_path / "out"))
    assert sorted(os.path.basename(p) for p in paths) == ["a.md", "b.md", "index.md"]
    a = open(tmp_path / "out" / "a.md", encoding="utf-8").read()
    assert "ACCEPTED" in a and "w1" in a and "ACCEPT" in a and "3 passed" in a and "a.patch" in a
    b = open(tmp_path / "out" / "b.md", encoding="utf-8").read()
    assert "NOT ACCEPTED" in b and "f(-3) must be -6" in b and "1 failed, 2 passed" in b
    assert "worker failure" in b.lower() and "w2" in b
    idx = open(tmp_path / "out" / "index.md", encoding="utf-8").read()
    assert "1 of 2 accepted" in idx and "[a](a.md)" in idx and "[b](b.md)" in idx


def test_a_task_with_no_review_and_no_grade_still_gets_a_page(tmp_path):
    run = make_run(str(tmp_path / "run"), {"c": {"outcome": "infra-error", "accepted": False, "grade": None}})
    report.write(run, str(tmp_path / "out"))
    c = open(tmp_path / "out" / "c.md", encoding="utf-8").read()
    assert "infra-error" in c and "not graded" in c.lower() and "no review" in c.lower()


def test_feedback_cannot_break_out_of_its_block(tmp_path):
    run = make_run(str(tmp_path / "run"), {"d": {"outcome": "reworked-unreviewed", "accepted": False,
        "reviews": [{"lane": "w1", "verdict": "REVISE", "feedback": "```\n# Injected heading\n```"}]}})
    report.write(run, str(tmp_path / "out"))
    d = open(tmp_path / "out" / "d.md", encoding="utf-8").read()
    assert "\n# Injected heading" not in d                                     # quoted, not rendered
```

- [x] **Step 2: Run them to verify they fail**

Run: `python -m pytest scripts/tools/workforce/tests/test_report.py -q`
Expected: FAIL — `No module named 'report'`.

- [x] **Step 3: Implement `report.py`**

```python
"""report.py -- one page per background task (coding engine spec §3.3 B: "results land as branches plus a
one-page report per ticket"), plus an index. Reads a harvested run directory; writes markdown only.
Agent-written text (review feedback) is quoted line by line so it cannot add headings or close blocks."""
import json
import os


def _quote(text):
    return "\n".join("> " + line for line in (text or "").splitlines()) or "> (empty)"


def _page(rec, run_dir):
    tid = rec["id"]
    status = "ACCEPTED" if rec.get("accepted") else "NOT ACCEPTED"
    g = rec.get("grade")
    tests = (g.get("summary") or ("pass" if g.get("pass") else "fail")) if g else "not graded"
    lines = [f"# {tid} -- {status}", "",
             f"- Outcome: `{rec.get('outcome')}`",
             f"- Hidden + visible tests: {tests}",
             f"- Rework rounds: {rec.get('rework_rounds', 0)}",
             f"- Time: {((rec.get('t_end') or 0) - (rec.get('t_start') or 0)) / 60:.1f} min"]
    patch = os.path.join(run_dir, "patches", f"{tid}.patch")
    lines.append(f"- Patch: `patches/{tid}.patch`" if os.path.exists(patch) else "- Patch: none")
    if rec.get("dropped"):
        lines.append(f"- Dropped paths (out of scope): {', '.join(f'`{p}`' for p in rec['dropped'])}")
    lines += ["", "## Review", ""]
    reviews = rec.get("reviews") or []
    if not reviews:
        lines.append("No review.")
    for i, rv in enumerate(reviews, start=1):
        lines += [f"Review {i} on `{rv.get('lane', 'lead')}`: **{rv.get('verdict')}**", "", _quote(rv.get("feedback")), ""]
    if rec.get("review_infra"):
        lines += ["", "## Worker failures (retried, not review rounds)", ""]
        lines += [f"- `{x.get('lane')}` rc {x.get('rc')}" for x in rec["review_infra"]]
    return "\n".join(lines) + "\n"


def write(run_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tasks_dir = os.path.join(run_dir, "tasks")
    recs = []
    for tid in sorted(os.listdir(tasks_dir)):
        p = os.path.join(tasks_dir, tid, "record.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                recs.append(json.load(f))
    written = []
    for rec in recs:
        path = os.path.join(out_dir, f"{rec['id']}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_page(rec, run_dir))
        written.append(path)
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
        run = json.load(f)
    acc = sum(1 for r in recs if r.get("accepted"))
    idx = [f"# Run report ({run.get('arm')}, {'valid' if run.get('valid') else 'INVALID'})", "",
           f"{acc} of {len(recs)} accepted in {run.get('wall_s', 0) / 60:.0f} min; "
           f"reviews per lane: {json.dumps(run.get('review_lanes', {}))}", ""]
    idx += [f"- [{r['id']}]({r['id']}.md) -- {'accepted' if r.get('accepted') else 'not accepted'} "
            f"(`{r.get('outcome')}`)" for r in recs]
    path = os.path.join(out_dir, "index.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(idx) + "\n")
    written.append(path)
    return written
```

In `cli.py`: `import report` with the other harness imports; add a subparser

```python
    p = sub.add_parser("report", help="one markdown page per task + index from a harvested run")
    p.add_argument("--run", required=True)
    p.add_argument("--out", required=True)
```

and in `main`'s dispatch: `elif a.cmd == "report": out = {"written": report.write(a.run, a.out)}` (match how the other subcommands return `out`).

- [x] **Step 4: Run the tests**

Run: `python -m pytest scripts/tools/workforce/tests -q`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add scripts/tools/workforce/report.py scripts/tools/workforce/cli.py scripts/tools/workforce/tests/test_report.py
git commit -m "feat(workforce): per-ticket report - one page per task plus an index"
```

---

### Task 7: Done-check — S and G-pool in one window (live)

Runs on the Proxmox host, in VCF lab **Minimal/Agent mode** (after the 2026-10-14 soak exit), never during a user session. Every step has its own rollback; nothing here edits production config.

- [x] **Step 1: Preconditions** — `pct exec 151 -- systemctl is-active llamacpp-chat`; router `/healthz` shows `capacity 3`, chat/embed/rerank ok; the three workers answer `/v1/models` with the worker key (stdin, never argv); `qm status 176` stopped or running with the locked policy (`75-vm-wf-sandbox-firewall.sh locked`).
- [x] **Step 2: Harness checkout** — `git -C /root/local-gpu-cluster fetch origin && git -C /root/local-gpu-cluster worktree add --detach /root/wf/lgc-p3 origin/main` (after this branch is merged; never deploy from the host's own checkout, it lags main); from `/root/wf/lgc-p3`: `bash scripts/76-vm-wf-sandbox.sh push-control && bash scripts/76-vm-wf-sandbox.sh push-harness` so VM 176 runs this harness and run-control; record `git -C /root/wf/lgc-p3 rev-parse HEAD` in the results.
- [x] **Step 3: Keys** — mint a scoped router key with the router-keys runbook (`pct exec 153 -- /opt/llm-router/router-keys add --out ...`; full path under `pct exec`), `pct pull` it to `/root/wf/keys/router.key` (0600); write the worker key from the workstation over stdin to `/root/wf/keys/worker.key` (0600). Never print either.
- [x] **Step 4: Window** — `bash scripts/files/wf-window.sh open` (Task 1 version), `stamp /root/wf/p3-stamps.jsonl before-s`.
- [x] **Step 5: S re-measure** — `76 warm S`, then the lead-trial driver for `p3-s-1 S` (as `wf-lead-trial3.sh`, LGC_CHECKOUT=/root/wf/lgc-p3); harvest; `stamp after-s`.
- [x] **Step 6: G-pool** — `76 warm G --review-pool`, then `p3-gp-1 G --review-pool --review-budget=1 --read-first --defects-only`; harvest; `stamp after-gp`.
- [x] **Step 7: Close** — `wf-window.sh close`; `76 ... clear-keys` in the guest; shred `/root/wf/keys/*.key`; revoke the scoped router key.
- [x] **Step 8: Decide and record** — compute from the two `run.json` files: `wall_gp / wall_s` (pass if ≤ 1.20), `accepted_gp >= accepted_s` and `>= 13` (the S mean); both runs valid, stamps agree (same boot/router/chat invocation). Write `docs/superpowers/workforce/phase3-review-offload.json` with the two summaries, harness commit, review-lane counts, worker failures, and the verdict; generate `cli.py report --run <gp run> --out <run>/report` and spot-read three pages. If wall time fails by > 20%, the record says so — no re-runs until the numbers are read (16 tasks: run-to-run noise ≈ 3 tasks).
- [x] **Step 9: Commit**

```bash
git add docs/superpowers/workforce/phase3-review-offload.json docs/superpowers/plans/2026-10-13-coding-engine-phase3-review-offload.md
git commit -m "docs(workforce): phase 3 done-check - S vs G with CPU review lanes in one window"
```
