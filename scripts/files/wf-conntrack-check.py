#!/usr/bin/env python3
"""wf-conntrack-check.py ALLOWED... < `conntrack -L -s 10.79.0.0/24` (host side; Plan B).

The PVE firewall accepts ESTABLISHED/RELATED traffic before a guest's own rules, so a flow opened
in `build` mode would survive the switch to `locked` (security review HIGH). `75 locked` requires the
VM to be stopped and flushes the sandbox's conntrack entries; this check proves the table afterwards:
every tracked flow from the sandbox must be TCP/UDP to an allowed "ip:port" (original direction).
Exit 0 = clean, 1 = a disallowed flow (printed), 2 = an entry this script cannot parse.
"""
import re
import sys

_KV = re.compile(r"(\w+)=(\S+)")


def original(line):
    """(proto, dst, dport) of the original direction, or None for a non-entry line."""
    fields = line.split()
    if not fields or fields[0] not in ("tcp", "udp", "icmp", "icmpv6", "sctp", "udplite", "gre", "unknown"):
        return None
    first = {}
    for k, v in _KV.findall(line):
        first.setdefault(k, v)                        # the first src/dst/dport is the original direction
    if "dst" not in first:
        raise ValueError(f"unparseable conntrack entry: {line.strip()}")
    return fields[0], first["dst"], first.get("dport")


def main(argv):
    allowed = set(argv[1:])
    bad = []
    for line in sys.stdin:
        try:
            entry = original(line)
        except ValueError as e:
            print(e)
            return 2
        if entry is None:
            continue
        proto, dst, dport = entry
        target = f"{dst}:{dport}" if dport else dst
        if proto not in ("tcp", "udp") or target not in allowed:
            bad.append(f"{proto} {target}")
    for b in bad:
        print(f"disallowed sandbox flow: {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
