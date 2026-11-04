# Agent runner (VM 178): operator runbook

Built 2026-10-27..30 by the SDG-IAC session and captured here on 2026-10-30. The host and VM files in this repo
match the live machines byte for byte as of that date: `scripts/78-vm-agent-runner.sh diff` reports no drift.

VM 178 is a headless agent host. It runs local Qwen agents (opencode) on tasks written as GitHub issues in
`DraconisCluster/SDG-RUST` and `DraconisCluster/SDG-ARMA3`; the queue itself is in
[agent-runner-dispatcher.md](agent-runner-dispatcher.md). It replaces the retired workforce sandbox (VM 176, see
[wf-sandbox-runbook.md](wf-sandbox-runbook.md)). No secrets are in this repo. The router key, the GitHub token and
the VPN config stay on the machines that hold them.

## What is where

| Item | Value |
|---|---|
| VM | 178 `agent-runner` on host `gpu-cluster` 192.168.6.175, Debian 13 genericcloud (same image + SHA as script 74), cloud-init NoCloud |
| CPU / RAM / disk | 4 cores `cpu: host`, `cpuunits: 50` (yields to everything else), 8 GB (balloon off), 64G thick `tank-lxc:vm-178-disk-0` (discard, iothread, ssd) |
| NIC | virtio on `vmbrar`, `firewall=1`, `macfilter: 1` |
| Address | 10.81.0.10/24, gateway 10.81.0.254, DNS 9.9.9.9; egress SNAT to **192.168.6.78** |
| User / admin | `runner` (cloud-init, passwordless sudo). Admin SSH from the host only: `ssh -i /root/.ssh/agent-runner-admin runner@10.81.0.10` |
| Guest agent / console | `qemu-guest-agent` (`qm agent 178 ping`); `vga: serial0`, `serial0: socket` (`qm terminal 178`) |
| Autostart | `onboot: 1` (set 2026-10-30, owner's decision, once the dispatcher went live) |
| GitHub token | expires **Mon 2027-01-25**: the runner loses GitHub access that day (reminder issue in this repo) |

### Files managed as code (`scripts/78-vm-agent-runner.sh manifest`)

| Repo file (`scripts/files/`) | Installed at | Where |
|---|---|---|
| `agent-runner-net.sh` + `agent-runner-net.service` | `/usr/local/sbin/`, `/etc/systemd/system/` | host |
| `agent-runner-key-renew.sh` (installed without `.sh`) + `.service` + `.timer` | `/usr/local/sbin/agent-runner-key-renew`, `/etc/systemd/system/` | host |
| `agent-runner-178.fw` | `/etc/pve/firewall/178.fw` | host |
| `agent-run.sh` | `/usr/local/bin/agent-run` | VM |
| `agent-runner-opencode.json` | `/etc/agent-runner-agent/opencode.json` (moved out of `/etc/agent-runner` 2026-10-30) | VM |
| `agent-runner-probes.sh` | run by `78-vm-agent-runner.sh probe` (not installed) | VM |
| dispatcher (`agent_runner_dispatch.py`, its units) | see `80-agent-runner-dispatcher.sh` | VM |

```
scripts/78-vm-agent-runner.sh diff                       # read-only drift check, exit 1 on drift
scripts/78-vm-agent-runner.sh install-host --dry-run     # then without --dry-run (host files, daemon-reload, pve-firewall compile)
scripts/78-vm-agent-runner.sh install-vm   --dry-run     # then without --dry-run (wrapper + profile, opencode config validated)
scripts/78-vm-agent-runner.sh install-tools --dry-run    # then without: VM_DEBS + pinned binaries (agent-runner-tools.lock.json)
```
Not managed by the script:
- **VM creation:** use the settings above with `qm create` / `qm set`.
- **`/etc/network/interfaces`:** `diff` checks it; you edit it by hand.
- **Secrets:** the router key and gh token values.
- **The OpenVPN client config:** SDG-IAC's.

## Host networking

- **`vmbrar` bridge:** 10.81.0.254/24, no ports, IPv6 off (in `/etc/network/interfaces`). Apply changes with
  `ifreload -a`, never `ifdown`, on a bridge with a running VM: `ifdown` drops its firewall-bridge port.
- **Subnet choice:** 10.78 was rejected because SDN `sdxguest` (tester VM 172) uses it. SDN subnets live in
  `/etc/pve/sdn` and the generated `/etc/network/interfaces.d/sdn`; check both before picking a subnet.
- **Egress identity 192.168.6.78:** a second `address` line in the `vmbr0` stanza. A oneshot `ip addr add` was
  wiped by `ifreload -a` on 2026-10-27; the persistent line fixed it.
- **SNAT** 10.81.0.0/24 → .78 by `agent-runner-net.service`. Each `ifreload` or SDN apply re-appends SDN's
  `snat 1` rules as duplicates; de-duplicate them by hand. Expect exactly 4 NAT rules: DNAT :21422 → tester,
  SNAT 10.81 → .78, 10.50.10 → .175, 10.78 → .175.
- **Firewall** (`178.fw`, DROP both ways; egress locked down 2026-10-30):
  - **In:** SSH from the host only.
  - **Out, allowed, and nothing else:**
    - DNS 9.9.9.9;
    - the router 192.168.6.153:8000;
    - Memory Vault 192.168.6.223:3005;
    - the CPU workers 172.16.10.205-207:8090;
    - **NTP to the host** 10.81.0.254:123. Host chrony serves `vmbrar` only (`agent-runner-chrony.conf`), and
      the VM's timesyncd points there (`agent-runner-timesyncd.conf`);
    - the **VPN endpoint** 38.107.232.37 udp 1196;
    - **GitHub on 443**, through the `[IPSET github]` set only.
  - **The GitHub set** holds GitHub's published `api`, `git` and `web` IPv4 ranges from `api.github.com/meta`,
    collapsed (68 ranges on 2026-10-30). `agent-runner-github-ipset.timer` refreshes it daily on the host. The set
    must be declared **before** `[RULES]`: PVE parses the file top-down, and a set declared after the rules
    gives `no such ipset` and silently drops the rule.
  - **Not allowed any more:** general 80/443/123, so `apt` and `npm` can't reach their mirrors. To update
    packages, add a temporary rule, or add the mirror to an ipset.

## Model access

- **Key:** scoped router key `agent-runner` (aliases `qwen3.8-nothink,qwen3.6`, TTL 336 h), file
  `/etc/agent-runner/router.key` on the VM (root:runner 640).
- **Renewal:** `agent-runner-key-renew.timer` on the **host** runs daily. Under 48 h left, it revokes and
  re-adds the key, pushes it into the VM without printing it, and checks `/v1/models` returns 200. Flags:
  `--dry-run`, `--force`.
- **Lanes:** scoped keys get `chat slots − 1`. That is 1 on qwen3.6 (2 × 128K), 2 on qwen3.8 (3 × 128K), and 0 on
  any other profile. They can never swap profiles (409 `profile_mismatch`). `agent-run` reads `/healthz` and picks
  `qwen3.6` or `qwen3.8-nothink`; any other profile exits 75 (lane closed).

## Agent tooling (VM)

- **Packages:** git 2.47, gh 2.46, python3 3.13, nodejs 20, ripgrep, jq, openvpn, qemu-guest-agent. opencode
  **1.18.34** (`npm install -g opencode-ai@1.18.34`), the same pin as the workforce harness.
- **Task tools, installed by `78 install-tools` and checked by `78 diff`** (added 2026-10-30):
  - **Debs:** `nftables` and `python-is-python3`, which gives `/usr/bin/python` (SDG-ARMA3#1 checks for it).
    The VM has no apt mirror, so the host runs `apt-get download` (verified against its signed index; both are
    trixie) and the VM runs `dpkg -i`.
  - **SQF-VM 2026.04.03** at `/opt/sdg-tools/sqfvm/2026.04.03/sqfvm`, the path `a3 lint` expects. The pin is in
    `agent-runner-tools.lock.json`, mirrored from SDG-ARMA3 `tools/lab/tools.lock.json`. The host downloads
    the zip, checks the zip's sha256 and the binary's sha256, then pushes it. No egress change.
  - **Verified** inside the SDG-ARMA3 sandbox as `agent-arma`: `sqfvm -a -i file.sqf` evaluates, with no
    missing libraries. Without `-a`, sqfvm drops into its REPL and hangs until the timeout.
  - **`python3-paramiko`** (+ `python3-nacl`) for SDG-RUST `tools/lab/ptero.py` (Wings SFTP). `dpkg -i`
    resolves nothing, so `VM_DEBS` lists every dependency the VM lacks. Verified in the SDG-RUST sandbox:
    `tools/lab/on-server.sh 156 rlogmark.sh` prints `@oxide_<date>.txt:<bytes>` (read-only on the server).
- **SDG-RUST plugin cache** (`.cache/umod/<sha1>.cs`, gitignored; `tools/build.py` checks each file against
  `plugins.lock`). The sandbox mounts the clone read-only, so the cache is seeded from a workstation:
  copy `raidable-bases-sdg/.cache/umod/` to the host, then run `78 seed-umod DIR`. It refuses any file that
  doesn't hash to its own name or that no `plugins.lock` on origin lists. `78 diff` checks that every plugin
  in origin/main's lock is there. Re-seed it when `plugins.lock` gains a plugin.
- **Locked profile** (`agent-runner-opencode.json`, installed at `/etc/agent-runner-agent/opencode.json`,
  outside the directory the sandbox hides):
  - only agent `lab-runner` is enabled (general and explore are disabled), with an 80-step cap;
  - default deny. Allowed: read-only commands (`ls`, `cat`, `head`, `tail`, `wc`, `rg`, `jq`, `date`,
    `sleep`, `git status|log|diff|show`), `python3 tools/*`, `bash tools/*`, `./tools/*` and `pytest`.
    Denied explicitly: `gh*`, `ssh*` and `git push*`;
  - 131072 context per model.
- **Wrapper** `agent-run DIR "MESSAGE"`: called by `runner`; it starts opencode as user `agent` in the sandbox
  below.

### Sandbox (hardening 2026-10-30)

The opencode allowlist only matches command *prefixes*. `cat > file` wrote a file despite `edit: deny`, and
`cat /etc/agent-runner/gh-token` followed by `gh issue comment` could have leaked the token. So the boundary is
now the kernel, not the allowlist:

| Layer | What it does |
|---|---|
| User `agent` | System user (uid 999), no sudo, not in group `runner`, no home, no GitHub config, no SSH keys |
| `systemd-run` per task | `ProtectSystem=strict`, `ProtectHome=tmpfs` (all of `/home` hidden), `PrivateTmp`, `PrivateDevices`, `NoNewPrivileges`, an empty capability set, `RuntimeMaxSec` = the task's limit |
| Network | `IPAddressDeny=any`, allowing only loopback `127.0.0.1`/`::1` (never "localhost": that includes resolved's stub 127.0.0.53, which leaked DNS), the router `192.168.6.153` and the repo's own lab endpoints from `egress.conf`. SDG-RUST: 10.60.40.10:80 and 10.60.20.12:2022. SDG-ARMA3: 10.60.40.10:22 (the panel). nft table `inet agent_egress` adds the per-uid port check, and agent-run refuses to start without it. `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`. No DNS, no GitHub, no general internet. The VM's GitHub access is for the dispatcher, outside the sandbox |
| Filesystem view | The checkout is **read-only** at `/work/<repo>`. The writable per-task `/results` is `/var/lib/agent-results/<repo>-<issue>-<UTC>` (root:runner 750 parent), plus private `/tmp`. `/etc/agent-runner` and `/run/agent-runner` are inaccessible |
| Secrets | No GitHub credentials at all: an empty `GH_CONFIG_DIR`, the token stays in `/etc/agent-runner` (root:runner 750). The router key arrives through a root-only `EnvironmentFile` that systemd reads before dropping to `agent`; it is deleted after the run |
| Per-repo lab secrets | The same `EnvironmentFile`, **for that repo's tasks only**, from a table in `agent-run` (`REPO_ENV_FILE` / `REPO_ENV_PREFIX`). Today: SDG-RUST ← `/etc/agent-runner/ptero-156.env` (root:runner 640; `SDG_PTERO_SFTP_USER`, `SDG_PTERO_SFTP_PASSWORD`, `SDG_PTERO_API_KEY` for the Rust test server 156 subuser). SDG-ARMA3 gets none. A file with any line that is not `PREFIX_*=value` (or a comment or blank) is refused, so it cannot inject `LD_PRELOAD`, `PATH` and the like. The dispatcher strips each value literally (values of 8+ characters) from anything it posts |
| Reporting | The agent **prints** its report block. The dispatcher (`runner`) takes it from the event log, removes the literal secret values plus token patterns, posts it, and edits the labels |

`scripts/78-vm-agent-runner.sh probe` proves the walls: a shell in the identical sandbox, then a real opencode
agent. **2026-10-30: 18/18 passed.**
- `cat > probe.txt` fails with "Read-only file system".
- Reading the gh token, the router key, `/etc/agent-runner`, runner's gh config and runner's panel key all fail.
- `gh auth status` says not logged in, and no GitHub token is in the environment.
- `sudo` is blocked by no-new-privileges.
- `/results` and `/tmp` are writable, and `git log -1` works.
- The agent's own attempts are blocked by the allowlist or by the read-only filesystem, and the token value
  appears nowhere in its event log.

**Residual risks, by design:**
- **Secrets the agent can read:** the router key, and for SDG-RUST the `SDG_PTERO_*` values. The agent needs
  them, and its processes can read them. The sandbox can reach only the router and its repo's own lab endpoints
  (`egress.conf`), so it cannot send them anywhere outside. What remains is the agent writing a secret into its
  printed report, into `/results`, or onto a lab server it can reach: the dispatcher strips their literal values
  from anything it posts, and `/results` stays on the VM.
- **Accepted risk: the router key stays in the agent's environment** (owner decision 2026-10-30). A loopback
  proxy that holds the key was considered and rejected; the gain does not pay for a new service on every task's
  path.
  - **Why the key is low-value:** it is scoped to the two local qwen aliases on one chat slot. It cannot swap
    profiles or call Claude, it expires every 14 days (renewed automatically), and revoking it is one
    `router-keys` command.
  - **Why a leak is hard to use:** the router is reachable only from the LAN, so using a leaked key needs a
    foothold there, which is worth far more than the key.
  - **Revisit only if** the key gains broader rights: owner scope, a `claude-*` alias, or the right to swap
    profiles.
- **`ssh panel` from SDG-ARMA3 tasks only, through an ssh-agent (2026-10-30).** The panel key stays in runner's
  `~/.ssh`, and sandbox users can't read it. Here is how a task uses it:
  - **The agent:** `agent-runner-panel-agent.service` runs `ssh-agent` **as agent-arma** with its socket at
    `/run/agent-ssh-arma/agent.sock` (dir 700). Root loads the key into it on stdin at start.
  - **Why other users can't use it:** ssh-agent only serves its own uid or root, so agent-rust can't use it.
    It also marks itself non-dumpable, so agent-arma can't read the key out of its memory.
  - **The task side:** `agent-run` sets `SSH_AUTH_SOCK` for SDG-ARMA3 only, and makes the socket dir
    inaccessible to every other repo's sandbox.
  - **The `panel` alias:** `/etc/ssh/ssh_config.d/agent-runner-panel.conf`. It uses the key's public half and
    the host key, both copied from runner's files at install.
  - **Readiness:** if the agent is down or holds no key, `agent-run` exits 76, so the dispatcher doesn't claim
    SDG-ARMA3 tasks.
  - **The gate that makes this safe:** SDG-IAC locks the key on the panel to a forced command (`sdg-lab-gate`:
    `sdg-lab-lock` and `sdg-lab-panel` for servers 158–160 only). Any other command, a shell and port
    forwarding are all refused. The key was root-equivalent on the panel before that gate, so the socket must
    not go live without it.
  - **Checks:** the probes test both sides from inside the sandbox. SDG-ARMA3: the key is held, `ssh panel id`
    authenticates and is refused, `-L` forwarding fails and the agent's `/proc` entry can't be read. SDG-RUST: no socket.
  - The opencode profile still denies `ssh*` to the model. Only repo tools (`python3 tools/*`, `./tools/*`)
    call `ssh panel`.
- **Playbooks must write their output under `/results`**, not into the repo. Repo tools that write into the
  tree (caches, build output) need a flag or an out-of-tree path.
- **GitHub:**
  - fine-grained token (account `mavlite`; repos SDG-IAC, SDG-RUST, SDG-ARMA3) in `/etc/agent-runner/gh-token`;
  - clones in `~/work/{SDG-RUST,SDG-ARMA3,SDG-IAC}` with their push URL disabled;
  - git identity `agent-runner <agent-runner@invalid>`.

## VPN and the panel (owned by SDG-IAC)

- **Tunnel:** OPNsense OpenVPN instance "Agent Runner VPN", udp 1196, client CN `agent-runner` pinned to
  **10.60.56.10**; it pushes only 10.60.40.10/32.
- **Reachable:** only the Pterodactyl panel's SSH (`ssh panel` = deploy@10.60.40.10, key restricted to
  `from="10.60.56.10"`, no forwarding, and since 2026-10-30 a forced command: `sdg-lab-gate`, which allows only
  `sdg-lab-lock` and `sdg-lab-panel` for servers 158–160).
- **Not reachable:** Wings nodes, RCON, game ports and everything else, until SDG-IAC opens a path on request.

## Ownership

| Part | Owner |
|---|---|
| VM 178, `vmbrar`, .78 SNAT, PVE firewall, router key + renewal timer, opencode profile + wrapper, dispatcher, router lanes | **local-gpu-cluster** (this repo) |
| OPNsense VPN instance, certificate, override and rules; 3HCloud; panel nftables and authorized_keys | SDG-IAC |
| Labels, issue form, playbook issues | SDG-RUST and SDG-ARMA3, each its own |

## Runbook

- **Start / stop:** `qm start 178` / `qm shutdown 178`. The VPN comes up on its own at boot.
- **Health:** check these four.
  - `qm agent 178 ping`.
  - On the VM: `ip -br -4 addr show tun0` (expect 10.60.56.10).
  - `curl -s http://192.168.6.153:8000/healthz | jq .active_chat_profile`, and `gh auth status`.
  - `agent-run ~/work "Reply with exactly the word: ready"`.
- **After a host reboot, check the NAT units.** On 2026-11-04 `agent-runner-net.service` failed at boot: `iptables`
  without a wait hit `/run/xtables.lock` (exit 4), so the 10.81 SNAT rule was missing and VM 178 had no GitHub or
  router. The dispatcher then fails every run with `error connecting to api.github.com` and `healthz unreadable`.
  - Check on the host: `systemctl --failed`, then `iptables -w -t nat -S POSTROUTING | grep 10.81`.
  - Fix: `systemctl restart agent-runner-net.service`, then from the VM
    `curl -s -o /dev/null -w '%{http_code}\n' http://192.168.6.153:8000/healthz`.
  - `agent-runner-net.sh`, `gamepilot-net.sh` and `tester-vm-dnat.sh` now use `iptables -w 60`. Tested by holding the
    lock by hand (the old command returns 4, the new script waits and succeeds); no real reboot has tested it yet.
    Restarting the unit drops the SNAT rule for a moment, so don't do it during a task.
- **Restore integration server 156 (pve) when it is frozen.** See the section below.
- **Drift:** `scripts/78-vm-agent-runner.sh diff` on the host.
- **Router key:** `systemctl list-timers agent-runner-key-renew.timer`. Force a renewal on the host with
  `/usr/local/sbin/agent-runner-key-renew --force`.
- **Allowlist change:**
  1. Edit `scripts/files/agent-runner-opencode.json` here.
  2. Run `78-vm-agent-runner.sh install-vm`; it validates with `opencode debug config`.
  3. Log the change. Never change the allowlist from inside an agent run.
- **Revoke everything (incident):**
  1. `pct exec 153 -- /usr/local/sbin/router-keys revoke --name agent-runner`.
  2. Revoke the GitHub token in GitHub settings.
  3. Revoke the `agent-runner` certificate in OPNsense (VPN revocation list) and disable its override.
  4. Remove the runner key from the panel through SDG-IAC's Ansible.
  5. `qm stop 178`.

### Integration server 156: freezes, recovery and deploys

156 is the pve integration server, and its normal content is `main`'s pve **test** build. The panel can say `running`
while the server is frozen, so don't trust the power state. These are the signs:

- `RUST_SERVER=156 bash tools/lab/server-alive.sh` prints `SERVER NOT RESPONDING`.
- The panel's `resources` show CPU near 3% (a healthy server sits far higher; 155 read 122%).
- The last line of `oxide/logs/oxide_<date>.txt` is `[CSharp] Started Oxide.Compiler`, and the compiler log ends
  `Pushing job N back to parent`.

Run the commands below in the SDG-RUST sandbox, never by hand on the panel, with the runner idle. `ptero` comes from
`. tools/lab/rust-servers.sh`.

```
AGENT_MAX_SECONDS=3000 AGENT_RUN_SHELL=1 agent-run /home/runner/work/SDG-RUST "<commands>"
```

#### 1. Capture evidence before any restart

A restart destroys most of this, and the cause is still open, so capture it first.

- From the sandbox: CPU and uptime from the panel's `resources`; the end of `oxide.compiler_<date>.log` and
  `oxide_<date>.txt`, read through the panel API as well as SFTP.
- From the node, which the sandbox can't reach (use SDG-IAC's route from the workstation): whether the `Oxide.Compiler`
  process is alive, the thread states of it and `RustDedicated`, `oom_kill` in the container's `memory.events`, and the
  node's `dmesg`.
- If the freeze followed an upload: when the upload finished, compared with the `Started Oxide.Compiler` line.

#### 2. Recover a frozen server (content already current)

Tested 2026-11-04.

1. `bash tools/lab/server-lock.sh acquire <owner> 156 1`. A heartbeat older than 30 minutes is stale and this breaks it.
   Release the lock when finished, on every exit path.
2. `mark=$(ptero 156 logmark)`, then `ptero 156 power restart`. `ensure-running.sh` refuses integration servers by
   design, so 156 needs this direct call. The new process appeared about 10 minutes after the restart command.
3. Verify (section 4), then release the lock.

#### 3. Deploy a new build: stop, upload, start

Use this instead of `rust-deploy-wt.sh`, which uploads to a running server. That route froze 156 on 2026-11-04: the
upload completed, then the helper printed `SERVER NOT RESPONDING` and exited 1 before its stale-file cleanup and config
reloads. A restart without re-uploading then booted cleanly.

**Tested once, 2026-11-04, at 397307c:** stop took 16 s and the 872-file upload 48 s; plugin init finished 3 minutes
after start, and all 10 SDG plugins loaded. The console still answered after a 5-minute soak. About 10 minutes in all,
and the stale check found nothing to remove. This route is meant to stop anything compiling mid-upload. The
freeze is intermittent, so one clean run supports that idea without proving it. `rust-deploy-wt.sh`, and so
`rust-integrate.sh`, still upload to a running server.

```
. tools/lab/rust-servers.sh; export RUST_SERVER=156
bash tools/lab/server-lock.sh acquire <owner> 156 1
ptero 156 read oxide/data/SdgProfile/profile.json   # must say pve: this route does no profile switch
b=$(mktemp -d); umod=$b/.cache/umod; [ -d .cache/umod ] && umod=$PWD/.cache/umod
RBGEN_UMOD_OFFLINE=1 RBGEN_POOL_CACHE=$b/.cache/pool RBGEN_UMOD_CACHE=$umod \
  python tools/build.py --profile pve --test --out-root "$b"
mark=$(ptero 156 logmark)
ptero 156 power stop                                # then wait until `bash tools/lab/rstate.sh` says "state offline"
ptero 156 put-tree "$b/dist-test-pve/oxide" oxide
```

The upload only adds and overwrites files, so remove what the new build no longer ships. Compare `Sdg*.cs` in
`ptero 156 ls oxide/plugins` with `$b/dist-test-pve/oxide/plugins`, and `ptero 156 ls oxide/data/RaidableBases/Profiles`
with the same folder in the build. Delete any extra with `ptero 156 rm <dir> <file>`. Then `ptero 156 power start`,
verify (section 4), and release the lock. Config reloads aren't needed, because the boot reads everything from disk.

#### 4. Verify

Don't treat the first console answer as ready: 156 answered about a minute after the process started, with plugin init
still running. Wait for these after the mark:

- `ptero 156 log "$mark" 'Grid initialization completed' 1` (end of plugin init);
- `ptero 156 log "$mark" 'Loaded plugin SDG' 20`: all 10 SDG plugins at the expected versions;
- `server-alive.sh` still `alive` 5 minutes later, past `Shutting down compiler because idle shutdown`;
- `oxide/data/SdgProfile/profile.json` shows `profile: pve` and the expected `built` sha;
- `ptero 156 ls oxide/data/RaidableBases/Profiles` has no stale profiles (on 2026-11-04: `RaidBases.json` and `t1_pool`
  to `t4_pool`).

#### What's known about the freeze

The cause is not established.

- **It's intermittent on 156.** It froze on 11-01, 11-03 and 11-04. Per the SDG-RUST session's record, the compiler also
  started fine several times on 11-01, with a 97/0 pve run in between. So 155's one clean upload (#9 run 7, its first
  real one) doesn't show the two servers differ; they share a node.
- **Probably not the plugin code.** Per the same record, plugins, config, data and `build.py` don't differ between
  3149642, which passed that run, and 397307c.
- **Where it stops:** on 11-04 the compiler finished job 1 (`compiled 1/1`, `AutomatedEvents.cs`, `Pushing job 1 back to
  parent`) and nothing followed. On 11-03 it stopped the same way after a 3/3 job 2. On 155's run 7, jobs ran straight on
  from job 1. That and the CPU near 3% suggest `RustDedicated` deadlocks handling a result, rather than the compiler dying
  mid-compile. This is unproven: process, thread and `oom_kill` data have not been captured for any freeze.
- **Possible trigger:** `put-tree` overwrites 872 files one at a time, so Oxide's watcher may start compiling mid-upload.
  On 11-04 job 1 held only the first file alphabetically, which fits. It doesn't explain the uploads that worked.

**How 156 got hit in #9:** until SDG-RUST #17, the runner's single SFTP login carried 156's short id, so file operations
for 155 went to 156 (runs 4-6), including 155's "frozen" console checks, which were reading 156's log. That also left a
shared lock named `checks-155` on 156. The probe "Wings SFTP lands on the target server" (`78 probe SDG-RUST`) now
catches that login mistake.

## Open items

From the infra summary's section 10, tracked here.

- [x] **The dispatcher** (queue spec section 6) and its tests: PR #52, live on the VM since 2026-10-30 with the
  timer enabled and a self-test PASS.
- [x] **`onboot` decided:** `qm set 178 --onboot 1` (2026-10-30). The drift check now expects it.
- [x] **Mark the workforce sandbox (VM 176) retired** in `docs/wf-sandbox-runbook.md`: PR #51.
- [x] **GitHub token expiry recorded:** **Mon 2027-01-25**, with a reminder issue in this repo.
- [ ] **Renew the GitHub token before 2027-01-25.** The owner creates a new fine-grained token (account `mavlite`;
  repos SDG-IAC, SDG-RUST, SDG-ARMA3), types it into `/etc/agent-runner/gh-token` (root:runner 640), then runs
  `gh auth login --with-token` as `runner`. Update the date above. The dispatcher also reads the expiry from
  GitHub's `Github-Authentication-Token-Expiration` response header. It logs a journal `WARNING` once a day
  when under 14 days remain, and `80 selftest` prints the days left.
- [ ] **VPN client certificate `agent-runner`** expires around **2027-11** (397 days from 2026-10-30). Renew
  through SDG-IAC.
- [ ] **Lab secrets** for playbooks (RCON passwords, panel API keys): the owner provisions them into
  `/etc/agent-runner/`, root:runner 640, once the Rust and Arma sessions say which they need. Each new file
  also needs a row in `agent-run`'s `REPO_ENV_FILE` table, and its path in the dispatcher's
  `DISPATCH_SECRET_FILES` defaults so its values get redacted.
  - [x] **2026-10-30:** `ptero-156.env` (SDG_PTERO_*) for SDG-RUST, wired in and probed.
- [ ] **Network openings beyond the panel:** SDG-IAC adds them on request, from a host:port list from each game
  repo.
- [ ] **All game testing is halted** by the owner as of 2026-10-30 ~12:00Z. No task that touches a server runs
  until it is lifted.
