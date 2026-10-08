# Workforce sandbox VM — operator runbook

VM 176 is the machine every workforce agent action runs in (workforce spec §5.1). The harness
(`scripts/tools/workforce`) runs there as root; agents run as `wf-impl-1..3` and `wf-lead` in systemd
scopes. There is **no SSH and no inbound port**: everything goes through the QEMU guest agent
(`qm guest exec`), which `76-vm-wf-sandbox.sh` wraps.

| Item | Value |
|---|---|
| VM | 176, Debian 13, 6 cores, 16 GB, 48 GB thick zvol on `tank-lxc` |
| Network | bridge `vmbrwf` 10.79.0.0/24, gateway 10.79.0.254, guest 10.79.0.10, IPv6 off |
| Egress address | SNAT to **192.168.6.79** (`wf-sandbox-net.service`), never the host's own address |
| Firewall | PVE per-VM policy in `/etc/pve/firewall/176.fw`, modes `closed` / `build` / `locked` |
| Inside | `/opt/wfpy` venv, opencode 1.18.34, grader image `wf-grader:1`, `/etc/wf-sandbox.json` |

All commands run on the Proxmox host as root, from `/root/local-gpu-cluster`.

## Firewall modes

| Mode | Allows | Used for |
|---|---|---|
| `closed` | nothing | default; `74` writes it before the VM exists |
| `build` | internet only, no private address space | provisioning |
| `locked` | `192.168.6.153:8000` (router chat) and `172.16.10.205-207:8090` (CPU workers) | measured runs |

`75-vm-wf-sandbox-firewall.sh locked` **refuses while the VM runs**. The PVE firewall accepts
established traffic before a guest's own rules, so a connection opened in `build` mode would survive
the switch. Locking therefore needs the VM stopped, flushes the sandbox's conntrack entries, and
proves none survive.

## Build order (first time, or after a teardown)

```bash
bash scripts/74-vm-wf-sandbox.sh                                   # vmbrwf, SNAT, VM 176 (not started), closed policy
set -o pipefail
bash scripts/75-vm-wf-sandbox-firewall.sh build | tail -2
qm start 176                                                       # wait: qm guest cmd 176 ping
bash scripts/76-vm-wf-sandbox.sh provision                         # 10-20 min; prints /etc/wf-sandbox.json
qm shutdown 176 --timeout 120
bash scripts/75-vm-wf-sandbox-firewall.sh locked | tail -3         # "conntrack flushed", "locked policy active"
qm start 176
WF_PROOF_WORKERS=skip bash scripts/76-vm-wf-sandbox.sh proof        # must exit 0
```

## `76-vm-wf-sandbox.sh` subcommands

| Subcommand | Needs | Does |
|---|---|---|
| `provision` | `build` | pushes the harness, grader Dockerfile, requirements and provision script, then runs it |
| `proof` | `locked` | runs the boundary proof in the guest; exit 0 only if every check passed |
| `validate B R` | `locked` | pushes bundle dir `B` and reference-patch dir `R`, then grades them in Docker: must match local grading |
| `status` | any | prints the active policy mode and `/etc/wf-sandbox.json` |

## Before every measured window

1. Make sure the CPU workers' nftables allow **192.168.6.79** on port 8090.
2. Run the full proof, workers included:

   ```bash
   bash scripts/76-vm-wf-sandbox.sh proof; echo "rc=$?"
   ```

   Expect `"failed": []`, every deny row `timeout`, `/metrics` without a key `403`, and the agent
   checks `denied` / `killed` / `no-network`.
3. **Any failure means no window.** A proof that refuses with "needs the locked policy" means the
   firewall file has drifted: `qm shutdown 176`, `75 locked`, `qm start 176`, then re-run the proof.

The proof first checks the host's conntrack table for sandbox flows that are not on the allowlist.
Unanswered entries to multicast addresses are exempt. A freshly booted guest sends an IGMP report
and LLMNR queries, and the kernel records bridged multicast before the firewall drops it (verified
2026-10-08: 3 packets on `tap176i0`, 0 on `fwln176i0` / `fwpr176p0` / `vmbrwf`). An answered
multicast entry still fails the check.

## VRAM verdict (decision H)

Measured 2026-10-08 with `scripts/77-wf-vram-check.sh`: **fits** (`rc=0`). In 3-slot mode with
RAG loaded, card0 used 28.1 of 32.2 GB and card1 23.5 GB, and a concurrent probe succeeded.
Workforce windows can run with RAG left on.

The check restarts chat twice and takes RAG down for about a minute. Re-run it only after a change
to the chat model, its context, or the embed/rerank models. `rc=1` means it does not fit; `rc=2`
means the check itself failed. Either way the script restores the normal layout; confirm with
`/healthz` (capacity 1, chat/embed/rerank all `ok`).

## Teardown

```bash
qm stop 176 && qm destroy 176 --purge
systemctl disable --now wf-sandbox-net
rm /etc/pve/firewall/176.fw
# then remove the vmbrwf stanza from /etc/network/interfaces and run: ifreload -a
```
