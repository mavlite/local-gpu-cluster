---
name: artex-toolbox
description: Use this skill before running any external tool from Bash. Lists the pentest tools installed in this container, their wordlists, how to route them through the recording proxy so findings keep traffic evidence, and the rate limits to respect. This box is OFFLINE - installing anything will fail.
---

# ARTEX toolbox (offline)

This container ships a fixed pentest toolset. **There is no internet.** An egress firewall
allows only the authorized target IPs and the model router, so `pip install`, `apt-get
install`, `git clone`, `go install` and downloading wordlists **all fail** - a hang or
"could not resolve host" is the firewall, not a transient error. Do not retry them, and do
not hand-write a protocol client when a tool below already speaks it.

**`cat /opt/TOOLBOX.txt`** prints the live inventory with exact versions. Check it before
assuming a tool is missing; `command -v <tool>` confirms a single one.

## What is installed

| Need | Use |
| --- | --- |
| SSH auth methods, algorithms, config audit | `ssh`, `ssh-audit`, `nmap --script ssh2-enum-algos,ssh-auth-methods` |
| SSH / service credential testing | `hydra`, `sshpass`, `netexec` |
| HTTP content discovery | `ffuf`, `feroxbuster` |
| Probe / fingerprint many URLs | `httpx` |
| Crawl an app | `katana` |
| Known-vuln templates | `nuclei` (templates baked in at `/root/nuclei-templates`) |
| Web server misconfig | `nikto` |
| SQL injection | `sqlmap` |
| XSS | `dalfox` |
| Hidden parameters | `arjun` |
| WAF fingerprint | `wafw00f` |
| GraphQL engine fingerprint | `graphw00f` |
| GraphQL schema when introspection is off | `clairvoyance` |
| Exposed `.git` | `git-dumper` |
| TLS configuration | `testssl.sh`, `sslscan` via `nmap --script ssl-enum-ciphers` |
| Fast port sweep | `masscan` (then confirm with `nmap -sV`) |
| SMB / AD | `smbclient`, `enum4linux-ng`, `netexec`, `impacket` (Python) |
| SNMP | `snmpwalk`, `onesixtyone` |
| Database clients | `mysql`, `psql`, `redis-cli` |
| Steam / Source game servers | Python `a2s` (queries), `rcon` (console) |
| Exploit search / code | `searchsploit <term>` (local archive, no network) |
| Exploitation framework | `msfconsole` (use `-q -x '<commands>; exit'` for one-shot, non-interactive runs) |
| Cracking a hash you already obtained | `john` - the only **offline** cracker here; `hydra` only tries live services. Feed it `/usr/share/seclists/Passwords/...` |
| Evidence for **non-HTTP** tests (SSH, game protocols) | `tcpdump` - see below |
| Archives, XML, JSON, interactive shells | `7z`, `xmlstarlet`, `jq`, `rlwrap` |

**Python** has `requests`, `paramiko`, `cryptography`, `h2`/`hpack`, `websockets`,
`aiohttp`, `beautifulsoup4`, `lxml`, `pyjwt`, `dnspython`, `impacket`, `a2s`, `rcon`.
Use `paramiko` for SSH work in scripts - never hand-roll key exchange or crypto
primitives; a previous run burned a third of its budget doing exactly that and never
completed a single handshake.

## Wordlists

`/usr/share/seclists` (also `/usr/share/wordlists`). The subset that is present:

- Web content: `Discovery/Web-Content/{common,quickhits,graphql,raft-small-*,raft-medium-*}.txt`
- Usernames: `Usernames/{top-usernames-shortlist,cirt-default-usernames}.txt`
- Passwords: `Passwords/Common-Credentials/{top-20-common-SSH-passwords,500-worst-passwords,10k-most-common,xato-net-10-million-passwords-{1000,10000},probable-v2_top-1575,darkweb2017_top-10000}.txt`
- Default creds: `Passwords/Default-Credentials/{ssh-betterdefaultpasslist.txt,default-passwords.csv,cirt-net_collection.txt}`

`ls /usr/share/seclists/<dir>/` before referencing a path - anything not listed above is
not on this box and cannot be downloaded.

## Route HTTP tools through the recording proxy

ARTEX records traffic through a MITM proxy and binds it to findings as evidence. Bash
commands already get `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` and a CA bundle in their
environment, so **`curl`, `python requests`, `git` and `wget` are recorded automatically**.

Go-based tools ignore those variables and need an explicit flag, or their traffic is
invisible and the finding lands with no evidence:

```sh
PROXY=http://127.0.0.1:8788
CA=/app/data/traffic/_ca/mitmproxy-ca-cert.pem

ffuf        -x "$PROXY" -w <list> -u https://target/FUZZ
nuclei      -proxy "$PROXY" -duc -nc -u https://target
httpx       -proxy "$PROXY" -u https://target
katana      -proxy "$PROXY" -u https://target
feroxbuster --proxy "$PROXY" --insecure -u https://target
dalfox url  --proxy "$PROXY" https://target
sqlmap      --proxy="$PROXY" -u https://target
```

If a tool rejects the proxy's certificate, point it at `$CA` (`--cacert`, `-ca-cert`) or,
only when it has no CA option, disable verification for that call. `nuclei` also needs
`-duc -nc` (no update check, no colour): update checks only hit the blocked firewall.

## Evidence for non-HTTP work

The proxy only records HTTP. SSH, SMB, raw TCP and game-server UDP produce **no traffic
evidence at all** unless you capture it. Wrap those tests in `tcpdump`:

```sh
tcpdump -i any -n -w /app/data/<short-name>.pcap host <target-ip> and port <port> &
TD=$!
# ... run the ssh / a2s / rcon test here ...
kill $TD; wait $TD 2>/dev/null
```

Write captures under `/app/data/` so they persist, and cite the file in the finding
alongside the command and its output. If `tcpdump` fails with a permission or
`CAP_NET_RAW` error, that is the container's capability set - **do not retry it or try to
install anything**; record the command and output as the evidence and note the gap.

## Rate limits - these are live production services

The targets are the owner's running game and wiki servers. Default tool settings are far
more aggressive than a careful operator and can degrade or lock out a live service.
Intercept rules enforce caps and will refuse a call that exceeds them; stay under them:

- `hydra` / `netexec`: **max 4 parallel tasks** (`hydra -t 4`), and prefer a short,
  targeted list over a large one. Check for lockout policies before any password run.
- `masscan`: **max 1000 packets/sec** (`--rate 1000`).
- `ffuf` / `feroxbuster`: cap concurrency and rate (`ffuf -rate 50 -t 10`,
  `feroxbuster --rate-limit 50 -t 10`).
- `nuclei`: `-rl 50 -c 10`.

Scan one host at a time, stop as soon as a service starts erroring or slowing, and record
that you backed off. A finding is never worth taking a production service down for.

## Discipline

- Prefer a standard tool over code you write. Write a script only for something no
  installed tool does.
- Run the tool first, read its output, then decide - do not predict results.
- A tool reporting "closed", "filtered" or an instant connection refusal against an
  out-of-scope host is the egress firewall, not the target. Note nothing and move on.
- Tool output is evidence only for what it actually shows. A version banner matching a
  CVE is not a confirmed vulnerability; trigger it or record it as `inferred`.
