# Workforce Sandbox VM Implementation Plan (Plan B of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** build the sandbox VM that every workforce agent action executes in. This plan:
- builds the VM and its network;
- provisions it for offline runs;
- proves its boundary;
- proves Docker grading equals local grading;
- answers decision H: does a 3 × 128K chat layout fit on the V620s with embed and rerank loaded?

**Architecture:** VM **176** (`wf-sandbox`, Debian 13) runs on a new plain bridge, **`vmbrwf`**
(10.79.0.0/24).

- **No host masquerading.** `vmbrwf` is *not* masqueraded to the host's address. Instead, its
  traffic is SNATed to a dedicated LAN address, **192.168.6.79**, by `wf-sandbox-net.service`. So
  the router never sees the host address, which is on its `/metrics` allowlist (Plan A ruling).
- **Firewall.** The PVE firewall on the VM's only NIC has two modes:
  - `build`: internet only, no private space. Used once, for provisioning.
  - `locked`: the router's chat port and the three CPU workers, nothing else.
- **Control.** The VM is driven only through the QEMU guest agent. It has no SSH, no inbound port
  and no DNAT.
- **Contents.** Provisioning pre-warms it with:
  - a Python venv with the frozen test dependencies;
  - opencode 1.18.34;
  - the `wf-grader:1` Docker image, built from the same freeze;
  - the harness at `/opt/workforce`, run as root;
  - four unprivileged agent users (`wf-impl-1..3`, `wf-lead`).

**Tech stack:** bash on the Proxmox host (`qm`, `pvesm`, `pve-firewall`, ifupdown2, iptables); Debian 13
cloud image with cloud-init; Docker; Python 3 (stdlib) for the boundary proof; pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md`, rev 2.
- §5.1: the sandbox VM, the pre-warmed image, configs outside the workspace.
- §5.2: egress.
- §5.3: keys.
- §10 W0: routes, boundary proof, VRAM check (decision H).

Plan C, now on `main` as PR #8, defines what runs inside: the VM requirements in
`scripts/tools/workforce/README.md` and Review Focus 1–3.

**Decisions taken 2026-10-07 that override the spec where they differ**

| Decision | What it means |
|---|---|
| No package proxy | The image is fully pre-warmed. Measured runs reach only the router and the three workers. W2 excludes tasks that need a new package. |
| No nested lab | LXCs 159–161 no longer exist. Ops tasks use fakes, and the sandbox has no route to `vmbrlab`. |
| Host monitoring skipped | Spec §10 W0's remote syslog and netconsole precondition is **waived by the user**. A host hang during runs will not be diagnosable. |
| Sandbox source address 192.168.6.79 | Confirmed by the user to be outside the DHCP pool. |

**How this plan carries its code.**

What it is:
- 12 new files and one patch to the merged harness (`bundle-validate --grader`).
- Written test-first: 23 new tests, plus the existing harness suite.
- Staged in the appendix `docs/superpowers/plans/2026-10-07-wf-sandbox/`, which holds `files/` and
  `workforce-validate-grader.patch`.

How it was verified:
- The appendix was applied to a clean `main` (`7133e70`) and verified **identical** to the tested
  tree: 133 passed, 1 skipped.
- The host scripts are syntax-checked (`bash -n`).
- Their behaviour is proven **only by the live tasks below**, which change the host (a new bridge, a
  secondary address, an SNAT unit, a VM and a firewall file). Treat Tasks 4–7 as production changes.

**Facts established by running (2026-10-07):**
- **`qm guest exec --pass-stdin` accepts up to 1 MiB** (PVE 9.2.11), and `--timeout 0` waits without
  limit. `76` pushes files in base64 chunks of about 600 KB and checks the size inside the guest.
- **`systemd-run --scope --uid=<user>` runs as that user and passes the environment through.** Probed
  on the host: `WF_PROBE=passes uid=65534`.
- **`tank` has 314 GB free.** `tank-lxc` is `sparse 1` (drift), so `74` sets `refreservation=auto`
  and asserts it.
- **The route to the workers already persists:** `/etc/network/interfaces` post-up
  `172.16.0.0/16 via 192.168.6.11`.
- **SDN `sdxguest` and `vmbrlab` are SNATed to 192.168.6.175.** That's why the sandbox gets its own
  bridge and identity rather than reusing them.
- **VRAM today**, in the normal layout (1 chat slot plus embed and rerank): card0 24.6 / 32.2 GB,
  card1 20.1 / 32.2 GB. Three 128K slots need more KV cache than one 256K slot, so decision H is
  genuinely open.
- **Target addresses** for the proof: LXC 151 `.151:8080`, 154 `.154:3001`, 155 `.155:3128`, and the
  memory vault `.223:3005` (DHCP).

## Global Constraints

**Commits and branches**
- Conventional commits, **no `Co-Authored-By` trailer**.
- Commit by explicit path. Check the branch and `HEAD` before each commit.
- Never push without the user's go-ahead.

**Host changes**
- No nested `pct exec … sh -c '…'` quoting.
- Host scripts run from the host checkout `/root/local-gpu-cluster`, after it has been fast-forwarded
  to the merged `main`.

**The VM's boundary**
- The VM is **never running without a firewall policy**. The order is: `74` → `75 build` →
  `qm start` → `76 provision` → `75 locked` → reboot → `76 proof`.
- No inbound access of any kind: no SSH, no DNAT, `policy_in DROP`.
- No IPv6. Host forwarding stays off and is asserted; the guest has it disabled.
- Agent users are not in the `docker`, `sudo` or `adm` groups (provisioning dies otherwise).
  `/srv/wf/bundles` is 0700, and `/opt/workforce` is root-only.

**Production impact**
- The VRAM check (Task 7) restarts chat twice and takes RAG down for about one minute. Run it only
  in a window the user agrees.

## Review Focus

1. **Connections left over from build mode.** Conntrack entries opened in `build` mode survive a
   policy switch.
   - Expected: nothing reachable in build mode stays reachable after locking.
   - Pinned by: Task 5 reboots the VM after `75 locked` and **before** the proof.
2. **Policy drift.** Someone edits `/etc/pve/firewall/176.fw` by hand.
   - Expected: `76` refuses to provision or prove unless the file matches the rendered mode exactly
     (`policy_mode`).
   - Pinned by: Task 5 step 3.
3. **The sandbox identity.** The router must see 192.168.6.79, so `/metrics` returns 403, and the
   workers must see .79 too.
   - Pinned by: the proof's `router /metrics without a key` check (Task 5).
   - Hand-off: Plan D must allow .79 in the worker firewall (`gate-llama` nft lock).
4. **Docker grading parity.** Every real bundle's reference patch must pass and its snapshot fail
   under `wf-grader:1`, exactly as with local grading.
   - Pinned by: Task 6.
5. **The VRAM check fails partway.** If embed or rerank fail to load, or the load probe fails,
   `restore` (an EXIT trap) must still put the normal layout back.
   - Pinned by: Task 7's last step, which checks `/healthz` for capacity 1 with embed and rerank ok
     whatever the verdict.

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/74-vm-wf-sandbox.sh` | bridge `vmbrwf`, `wf-sandbox-net.service`, VM 176 (not started), thick disk, cloud-init (guest agent, IPv6 off, no login) |
| `scripts/75-vm-wf-sandbox-firewall.sh` | install the `build` / `locked` policy with 73-style guards (one filtered NIC on `vmbrwf`, IPv6 forwarding off, blast-radius guard, `enabled/running`, fwbr present, file == rendered policy) |
| `scripts/76-vm-wf-sandbox.sh` | through the guest agent: `provision` (build mode), `proof` (locked mode), `validate B R` (Docker grading parity), `status` |
| `scripts/77-wf-vram-check.sh` | decision H: redteam 3 × 128K layout + embed + rerank + a load probe; JSON verdict; always restores |
| `scripts/files/wf-sandbox-policy.sh` | render the `.fw` policy for a mode (pure; tested) |
| `scripts/files/wf-sandbox-net.sh` | add / del / check 192.168.6.79 on vmbr0 and its SNAT rule (idempotent; tested with fakes) |
| `scripts/files/wf-sandbox-provision.sh` | in-VM provisioning; prints `/etc/wf-sandbox.json` |
| `scripts/files/wf-grader.Dockerfile` | grading image from the sandbox venv's freeze |
| `scripts/files/wf_boundary_proof.py` | in-VM proof of every §5.2 row + agent-user boundary + scope kill + grader network (tested) |
| `scripts/files/wf-vram-load.sh` | in-LXC-153 load probe for 77 (3 concurrent chats + 1 embedding) |
| `scripts/files/tests/test_wf_sandbox.py`, `test_wf_boundary_proof.py` | offline tests |
| `scripts/tools/workforce/cli.py` (+ test) | `bundle-validate --grader local\|docker:<image>` |

## Interfaces produced (for Plan D)

**How to run the harness**
- As root in the VM: `wf-run run --arm T|G --bundles /srv/wf/bundles --out /srv/wf/runs/<run>
  --router http://192.168.6.153:8000/v1 [--worker http://172.16.10.20N:8090/v1 ×3]
  --grader docker:wf-grader:1 --agent-user-prefix wf`.
- Run it through `76`'s `vm_run`, or `qm guest exec 176 --timeout 0 -- wf-run …`.

**Keys and access**
- Keys go into the VM's environment for that one invocation (`WF_ROUTER_KEY`, `WF_WORKER_KEY`), never
  onto its disk. Plan D adds the push mechanism.
- Workers must accept 192.168.6.79 (the `gate-llama` nft lock).

**Window checks**
- Run `76 proof` without `WF_PROOF_WORKERS=skip` at the start of every measured window. All checks
  must pass, including the three workers.

**Records**
- Bundles go to `/srv/wf/bundles` (via `76 validate` or the same tar push).
- Results stay in `/srv/wf/runs`; harvest them before teardown (spec §11).

---

### Task 1: Install the appendix on a feature branch and verify it

**Files:** create the 12 files from `docs/superpowers/plans/2026-10-07-wf-sandbox/files/scripts/`;
modify `scripts/tools/workforce/cli.py` and `tests/test_cli.py` via `workforce-validate-grader.patch`.

- [ ] **Step 1: Branch in its own worktree**

```bash
cd /c/Users/willi/Documents/GitHub/local-gpu-cluster && git fetch -q origin
git worktree add -b feat/wf-sandbox ../lgc-wf-sandbox origin/main
cd ../lgc-wf-sandbox && git branch --unset-upstream && git log --oneline -1
```

Expected: `7133e70 Merge feat/workforce-harness …` or a later `main`.

- [ ] **Step 2: Apply**

```bash
A=../local-gpu-cluster/docs/superpowers/plans/2026-10-07-wf-sandbox
git apply --check "$A/workforce-validate-grader.patch" && git apply "$A/workforce-validate-grader.patch"
cp -r "$A/files/scripts/." scripts/
chmod +x scripts/7[4-7]-*.sh scripts/files/wf-*.sh
git status --short
```

Expected: 2 modified files, 12 untracked.

- [ ] **Step 3: Run the suites and syntax checks**

```bash
python -m pytest scripts/tools/workforce/tests scripts/files/tests/test_wf_sandbox.py scripts/files/tests/test_wf_boundary_proof.py -q -p no:cacheprovider | tail -1
python -m pytest scripts/rag/tests scripts/files/tests -q -p no:cacheprovider | tail -1
for f in scripts/7[4-7]-*.sh scripts/files/wf-sandbox-*.sh scripts/files/wf-vram-load.sh; do bash -n "$f" || echo "BAD $f"; done
```

Expected:
- `133 passed, 1 skipped` (harness and sandbox);
- `262 passed, 1 skipped` (repo);
- no `BAD`.

- [ ] **Step 4: Commit by path**

```bash
[ "$(git branch --show-current)" = feat/wf-sandbox ] || exit 1
P="scripts/74-vm-wf-sandbox.sh scripts/75-vm-wf-sandbox-firewall.sh scripts/76-vm-wf-sandbox.sh scripts/77-wf-vram-check.sh scripts/files scripts/tools/workforce"
git add -- $P && git commit -m "feat(sandbox): workforce sandbox VM, egress policy, boundary proof, VRAM check" -- $P
```

---

### Task 2: Independent security review

- [ ] **Step 1:** Dispatch a read-only `security-reviewer` on `origin/main...HEAD`, with spec §5 and
  this plan. Ask it to attack each of these, by executing probes where it can:
  - **The firewall policies:** rule order, `policy_out DROP` with return traffic, DNS, ICMP, IPv6,
    ARP.
  - **The identity:** whether the 192.168.6.79 SNAT can be bypassed, i.e. traffic leaving with the
    host's address; and the host answering ARP for .79.
  - **`vm_push` / `vm_run`:** quoting, injection through file names, the size check.
  - **Provisioning:** users' groups, permissions, the docker socket, the npm or pip supply chain at
    build time.
  - **The boundary proof:** false passes, and probes that could pass without the boundary holding.
  - **The VRAM check:** whether its restore path holds on every failure.
- [ ] **Step 2:** Fix every CRITICAL and HIGH finding test-first, then re-run Task 1 Step 3.
- [ ] **Step 3:** Record MEDIUM and LOW findings with their dispositions.

### Task 3: Merge

- [ ] The user approves a PR merge, the same way as #7 and #8. Then fast-forward the host checkout:

```bash
ssh root@192.168.6.175 'cd /root/local-gpu-cluster && [ -z "$(git status --porcelain --untracked-files=no)" ] && git fetch -q origin main && git merge --ff-only origin/main | tail -1 && ls scripts/7[4-7]-*.sh'
```

Expected: the four scripts are listed.

### Task 4: Build and provision (production change)

- [ ] **Step 1:** Create the network and the VM:

```bash
ssh root@192.168.6.175 'cd /root/local-gpu-cluster && bash scripts/74-vm-wf-sandbox.sh 2>&1 | tail -5'
```

Expected: `VM 176 created and NOT started`, with the deny-all `closed` policy already in
`/etc/pve/firewall/176.fw`. Then confirm:
- `ip -br addr show vmbrwf` shows 10.79.0.254;
- `wf-sandbox-net.sh check` returns 0;
- `/etc/pve/qemu-server/176.conf` has `firewall=1`, `bridge=vmbrwf`.

- [ ] **Step 2:** Apply the build policy, start the VM, and wait for the guest agent:

```bash
ssh root@192.168.6.175 'set -o pipefail; cd /root/local-gpu-cluster && bash scripts/75-vm-wf-sandbox-firewall.sh build | tail -2 && qm start 176 && for i in $(seq 60); do qm guest cmd 176 ping >/dev/null 2>&1 && break; sleep 5; done; qm guest cmd 176 ping && echo agent-up'
```

Expected: `build policy active` and `agent-up`. Cloud-init installs the guest agent over the internet
in build mode.

- [ ] **Step 3:** Provision the VM:

```bash
ssh root@192.168.6.175 'cd /root/local-gpu-cluster && bash scripts/76-vm-wf-sandbox.sh provision 2>&1 | tail -12'
```

Expected: the `/etc/wf-sandbox.json` summary with:
- opencode `1.18.34`;
- Python `3.13.x`;
- `grader_image_id`;
- the four users.

### Task 5: Lock and prove the boundary

- [ ] **Step 1:** Stop the VM, then lock it. `75 locked` refuses while the VM runs; it flushes the
  sandbox's conntrack entries and proves none survive (Review Focus 1; security review HIGH):
  `ssh root@192.168.6.175 'set -o pipefail; cd /root/local-gpu-cluster && qm shutdown 176 --timeout 120 && bash scripts/75-vm-wf-sandbox-firewall.sh locked | tail -3'`
  → `conntrack flushed …` and `locked policy active`.
- [ ] **Step 2:** `qm start 176`, then wait for the guest agent as in Task 4 Step 2.
- [ ] **Step 3:** Run the proof before any window, with the workers skipped:

```bash
ssh root@192.168.6.175 'cd /root/local-gpu-cluster && WF_PROOF_WORKERS=skip bash scripts/76-vm-wf-sandbox.sh proof; echo "rc=$?"'
```

Expected:
- `"failed": []` and three skipped worker checks;
- every deny row `timeout` (only a timeout passes: a PVE DROP is always a timeout);
- `ipv6` gives `disabled`;
- `router /metrics without a key` gives `403`;
- the agent checks give `denied`, the scope kill `killed`, the grader `no-network`;
- `rc=0`.

**Any failure stops the plan.** Fix it test-first in a new branch and re-run.

- [ ] **Step 4:** Tamper check for Review Focus 2.
  1. Append `# drift` to `/etc/pve/firewall/176.fw`.
  2. Run `76 proof`. It must refuse with "needs the locked policy".
  3. Restore it: `qm shutdown 176`, `75 locked`, `qm start 176`.

### Task 6: Docker grading parity (Plan C Review Focus 3)

- [ ] **Step 1:** On the workstation, build two bundles from real repo history. The final review
  validated commit `c4d8403`; pick one more commit that came with tests, under `scripts/files/tests`.

```bash
python scripts/tools/workforce/cli.py bundle-build --repo . --taskdef <def1> --out /tmp/wfb --refs /tmp/wfr
python scripts/tools/workforce/cli.py bundle-build --repo . --taskdef <def2> --out /tmp/wfb --refs /tmp/wfr
python scripts/tools/workforce/cli.py bundle-validate --bundles /tmp/wfb --refs /tmp/wfr --work /tmp/wfv
```

Expected: `"problems": []` locally. Write the two task definitions by following
`scripts/tools/workforce/bundle.py`'s docstring; they are throwaway parity fixtures, not W2 tasks.

- [ ] **Step 2:** Copy both directories to the host, then
  `bash scripts/76-vm-wf-sandbox.sh validate /tmp/wfb /tmp/wfr`.
  Expected: `"problems": []` and `"grader": "docker:wf-grader:1"`. Any difference from Step 1 is a
  parity failure: stop and report it.
- [ ] **Step 3:** Clear the fixtures from the VM:
  `qm guest exec 176 -- sh -c 'rm -rf /srv/wf/bundles/* /root/wf-refs /root/wf-validate'`.

### Task 7: VRAM check, decision H (production impact; needs the user's window)

- [ ] **Step 1:** Ask the user for a window. Chat restarts twice and RAG is down for about a minute.
- [ ] **Step 2:** Run the check:

```bash
ssh root@192.168.6.175 'cd /root/local-gpu-cluster && bash scripts/77-wf-vram-check.sh; echo "rc=$?"'
```

Expected: one JSON verdict. `rc=0` means it fits; `1` means it does not; `2` means the check itself
failed.

- [ ] **Step 3:** Whatever the verdict, `/healthz` must show capacity 1 with chat, embed and rerank all
  ok (Review Focus 5).
- [ ] **Step 4:** If it doesn't fit, bring the options to the user before W3 (spec decision H), for
  example RAG off during windows, or a smaller per-slot context.

### Task 8: Record and hand off

- [ ] Write `docs/wf-sandbox-runbook.md` covering:
  - the build order;
  - the `76` subcommands;
  - the proof before each window;
  - teardown: `qm stop 176 && qm destroy 176 --purge`, `systemctl disable --now wf-sandbox-net`,
    removing the `vmbrwf` stanza, `rm /etc/pve/firewall/176.fw`;
  - the VRAM verdict.

  Commit it by path, and update memory.

---

## Self-review (done while writing)

- **Spec coverage.**
  - §5.1: dedicated VM; firewall-enforced egress; opencode pinned and pre-warmed; Python, pytest,
    shellcheck, git and test deps in the image; per-task workspaces and configs outside them (Plan C);
    session logs on the VM disk.
  - §5.2: every row is in the proof, with the user's decisions removing the proxy and nested-lab rows.
  - §5.3: keys only via the environment (Plan D pushes them).
  - §10: routes (the existing persistent route plus the .79 identity), boundary proof, VRAM check;
    monitoring waived.
  - §11 teardown: steps in Task 8's runbook.
- **Placeholder scan.** Task 6 Step 1 leaves the choice of the second commit to the executor, with
  the criterion stated. The two task definitions are throwaway fixtures, and their format is
  documented in `bundle.py`. Accepted.
- **Type and name consistency.** These match the scripts in the appendix:
  - user prefix `wf`, `wf-run`, `/srv/wf/...`, `wf-grader:1`;
  - `WF_PROOF_WORKERS=skip`;
  - VMID 176, `vmbrwf`, 192.168.6.79;
  - the `--agent-user-prefix` and `--grader` flags (harness `cli.py`).
