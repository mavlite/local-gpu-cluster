#!/usr/bin/env bash
# toolbox-manifest — print the toolbox inventory (tool → version) for /opt/TOOLBOX.txt.
# Runs at image build time; agents can `cat /opt/TOOLBOX.txt` to see what is available.
set -u

# Some tools emit ANSI colour even when not on a tty; strip it so the manifest stays plain.
strip_ansi() { sed -r 's/\x1B\[[0-9;]*[A-Za-z]//g'; }

# first non-empty line of a command's output
first_line() { "$@" 2>&1 | strip_ansi | grep -m1 -v '^[[:space:]]*$' | cut -c1-90; }
# first line matching a pattern (for tools with banners)
match_line() { local pat=$1; shift; "$@" 2>&1 | strip_ansi | grep -m1 -iE "$pat" | cut -c1-90; }
# just the matched text (for tools whose version sits inside ASCII-art banners)
extract() { local pat=$1; shift; "$@" 2>&1 | strip_ansi | grep -m1 -oiE "$pat"; }
# present/absent marker for tools whose version flag is unreliable or slow
have() { command -v "$1" >/dev/null 2>&1 && echo "installed ($(command -v "$1"))" || echo "MISSING"; }

echo "ARTEX toolbox — offline (egress-locked): do not apt/pip install; use what is here."
echo "Route HTTP tools through the recording proxy so findings keep traffic evidence."
echo
echo "## Rate caps (enforced — the targets are live production services)"
echo "hydra -t<=4 | masscan --rate<=1000 | ffuf -rate<=50 -t<=10"
echo "feroxbuster --rate-limit<=50 --threads<=10 | nuclei -rl<=50 -c<=10"
echo "A higher value is silently lowered and noted on stderr; omitting the flag applies"
echo "the cap as the default. Real binaries: /opt/toolbox/real (do not call directly)."
echo

# netexec prints "[*] First time use detected" and builds its home dir on first run, which
# would otherwise be what the manifest records as its version. Warm it up first.
netexec --version >/dev/null 2>&1 || true

echo "## Access / credentials"
printf '%-14s %s\n' ssh        "$(first_line ssh -V)"
printf '%-14s %s\n' sshpass    "$(first_line sshpass -V)"
printf '%-14s %s\n' ssh-audit  "$(python3 -c 'import importlib.metadata as m; print("ssh-audit", m.version("ssh-audit"))' 2>/dev/null)"
printf '%-14s %s\n' hydra      "$(match_line '^hydra v' hydra -h)"
printf '%-14s %s\n' netexec    "$(first_line netexec --version)"
printf '%-14s %s\n' smbclient  "$(match_line 'version' smbclient --version)"
printf '%-14s %s\n' enum4linux-ng "$(have enum4linux-ng)"
# john is the only OFFLINE cracker here; hydra is online-only. Wordlists: /usr/share/seclists.
printf '%-14s %s\n' john       "$(match_line 'John the Ripper' john)"
echo

echo "## Web"
printf '%-14s %s\n' ffuf        "$(first_line ffuf -V)"
printf '%-14s %s\n' feroxbuster "$(first_line feroxbuster --version)"
printf '%-14s %s\n' httpx       "$(match_line 'version' httpx -version -duc -nc)"
printf '%-14s %s\n' katana      "$(match_line 'version' katana -version -duc -nc)"
printf '%-14s %s\n' nuclei      "$(match_line 'engine version' nuclei -version -duc -nc)"
printf '%-14s %s\n' nikto       "$(match_line '^nikto [0-9]' nikto -Version)"
printf '%-14s %s\n' sqlmap      "$(first_line sqlmap --version)"
printf '%-14s %s\n' dalfox      "$(extract 'dalfox v[0-9][0-9.]*' dalfox version)"
printf '%-14s %s\n' arjun       "$(have arjun)"
printf '%-14s %s\n' wafw00f     "$(have wafw00f)"
printf '%-14s %s\n' graphw00f   "$(have graphw00f)"
printf '%-14s %s\n' clairvoyance "$(have clairvoyance)"
printf '%-14s %s\n' git-dumper  "$(have git-dumper)"
printf '%-14s %s\n' testssl.sh  "$(match_line 'testssl.sh +[0-9]' testssl.sh --version)"
echo

echo "## Network / services"
printf '%-14s %s\n' nmap       "$(first_line nmap --version)"
printf '%-14s %s\n' masscan    "$(first_line masscan --version)"
printf '%-14s %s\n' ncat       "$(first_line ncat --version)"
printf '%-14s %s\n' socat      "$(match_line 'socat version' socat -V)"
printf '%-14s %s\n' snmpwalk   "$(match_line 'version' snmpwalk --version)"
printf '%-14s %s\n' onesixtyone "$(have onesixtyone)"
printf '%-14s %s\n' mysql/psql "$(have mysql) / $(have psql)"
printf '%-14s %s\n' redis-cli  "$(have redis-cli)"
# tcpdump captures evidence for NON-HTTP tests (ssh, game protocols) that the recording
# proxy cannot see. Needs CAP_NET_RAW; if it errors, say so in the fact rather than retrying.
printf '%-14s %s\n' tcpdump    "$(first_line tcpdump --version)"
echo

echo "## Exploitation"
printf '%-14s %s\n' msfconsole  "$(match_line 'metasploit|framework' msfconsole --version)"
printf '%-14s %s\n' searchsploit "$(have searchsploit) — local archive /opt/exploitdb"
echo

echo "## Scripting"
echo "python3        $(python3 --version 2>&1)"
python3 - <<'PY'
import importlib.metadata as m
mods = ("requests", "paramiko", "cryptography", "h2", "hpack", "websocket-client",
        "websockets", "aiohttp", "beautifulsoup4", "lxml", "pyjwt", "dnspython",
        "impacket", "python-a2s", "rcon")
for d in mods:
    try:
        print(f"  {d:18} {m.version(d)}")
    except m.PackageNotFoundError:
        print(f"  {d:18} MISSING")
PY
printf '%-14s %s\n' "shell utils" "$(first_line ps --version) / $(first_line xxd -v) / git $(git --version 2>&1 | awk '{print $3}')"
printf '%-14s %s\n' "loot/misc"  "7z $(have 7z) | xmlstarlet $(have xmlstarlet) | rlwrap $(have rlwrap) | jq $(have jq)"
echo

echo "## Data"
echo "nuclei templates  /root/nuclei-templates ($(find /root/nuclei-templates -name '*.yaml' 2>/dev/null | wc -l) templates)"
echo "exploit archive   /opt/exploitdb ($(find /opt/exploitdb/exploits -type f 2>/dev/null | wc -l) entries) — searchsploit <term>"
echo "wordlists         /usr/share/seclists ($(cat /usr/share/seclists/VERSION 2>/dev/null)); /usr/share/wordlists -> seclists"
find /usr/share/seclists \( -name '*.txt' -o -name '*.csv' \) | sed 's#^/usr/share/seclists/#  #' | sort
