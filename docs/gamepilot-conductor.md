# GamePilot conductor (CT 160): cluster side

The GamePilot conductor runs one cycle every 15 minutes. It reads the game repos and runner issues on GitHub, creates
and labels issues, and calls `claude -p` for triage. Its code, units and owner runbook live in
`DraconisCluster/SDG-GAMEPILOT` (`deploy/`, `docs/runbooks/conductor-vm.md`). This page covers what the cluster
provides. Request: #66.

## Shape (owner decision 2026-10-31)

| | |
|---|---|
| Container | CT 160 `gamepilot`: unprivileged LXC, Debian 12, 2 cores, 4 GB, 512 MB swap, 20 GB on `local-lvm`, onboot |
| Network | bridge `vmbrgp` 10.82.0.254/24 (in `/etc/network/interfaces`), CT at 10.82.0.10, SNAT to 192.168.6.175 (`gamepilot-net.service`) |
| Egress | **only** the allowlisting proxy `192.168.6.155:3129` on LXC 155 (`gamepilot-proxy.service`, a second tinyproxy instance next to the redteam one on 3128) |
| Firewall | `160.fw`: inbound DROP, outbound REJECT, one rule: tcp to 192.168.6.155:3129. No DNS, no LAN, no router, no runner (10.81) |
| Allowlist | `github.com`, `api.github.com`, `api.anthropic.com`, `claude.ai`, `console.anthropic.com`, `deb.debian.org`, `security.debian.org` |
| Software | `python3`, `python3-yaml`, `git`, `gh` (Debian 2.23), `curl`, `sudo`; `claude` pinned in `gamepilot-tools.lock.json` at `/usr/local/bin/claude` |
| Code | `/opt/gamepilot` = SDG-GAMEPILOT `main`, origin set to GitHub. `deploy/install.sh` was run; **the timer is not enabled** |

**Why an LXC, not the VM #66 asked for:** every service here runs in an LXC. VM 178 is the one exception, for
untrusted agent shell work. The conductor has no GPU or kernel needs.

**Why one proxy instead of IP rules:** the firewall works on IPs, but `claude.ai`, `console.anthropic.com` and the
Debian mirrors sit behind CDNs with changing addresses. So every connection goes through the proxy, and all the
hostname rules live in one allowlist file.

**Why a separate proxy instance:** tinyproxy's filter is global. Sharing the redteam instance would have widened
both allowlists.

**How the proxy reaches each tool:**
- systemd units, including the conductor's own unit and the runbook's `systemd-run` checks: the `DefaultEnvironment`
  in `system.conf.d`.
- `sudo` sessions: a sudoers `env_file` and `env_keep`.
- Interactive shells: `/etc/profile.d`.
- apt: `apt.conf.d`.

**claude CLI settings:** `DISABLE_AUTOUPDATER=1` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The binary is
updated from the host; telemetry hosts are not on the allowlist.

**Budget:** `claude -p` on this CT uses the Claude login of the `gamepilot` user, the owner's Max subscription. It
does **not** go through the router's escalation broker, so the broker's $7/day and $150/month caps do not apply.

## Reaching it

From the Proxmox host: `pct enter 160`, or `pct exec 160 -- <command>`. Inbound is dropped, so there is no SSH into it.

## Operating

```
scripts/81-lxc-gamepilot.sh diff        # read-only drift check (files on host/155/CT, bridge, SNAT, CT settings)
scripts/81-lxc-gamepilot.sh verify      # allowed hosts tunnel, others get 403, direct routes are blocked
scripts/81-lxc-gamepilot.sh provision   # re-apply proxy settings, packages, the pinned claude
```

- **Allow a new host** (the panel VPN and Discord come later): add a line to `files/gamepilot-proxy-allowlist`, run
  `81 proxy`, then `81 verify`. Proxy log on LXC 155: `/var/log/tinyproxy/gamepilot.log`. A refused host shows
  there as `Filtered`.
- **Update claude:**
  1. Bump `version`, `url` and `sha256` in `files/gamepilot-tools.lock.json`, from
     `https://downloads.claude.ai/claude-code-releases/<version>/manifest.json`, platform `linux-x64`.
  2. Run `81 provision`.
- **Re-clone the code:** the repo is private and the CT has no GitHub credentials until the owner writes
  `/etc/gamepilot/gh-token`. So `81 clone BUNDLE SHA` takes a `git bundle` made on a machine with access. After the
  token exists, `git fetch` inside the CT works through the proxy.

## Build log (2026-10-31)

`81 bridge` added `vmbrgp`. As known, `ifreload -a` re-appended the SDN's `10.78.0.0/24` SNAT rule, and the duplicate
was removed by hand; NAT is back to the 4 rules (runner, tester, lab, gamepilot). Then `81 host`, `81 proxy`,
`81 create`, `81 provision`, `81 clone`. `install.sh` printed "Installed. The timer is not enabled". `81 verify`
passed everything and `81 diff` showed no drift.

**Owner steps, not done here (see the GamePilot runbook):**
- the `claude` login as `gamepilot`;
- `/etc/gamepilot/gh-token`;
- `gh auth setup-git`;
- the game clones;
- `/etc/gamepilot/config.yaml`;
- enabling the timer.
