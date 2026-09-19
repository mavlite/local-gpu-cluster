"""Defensive type coercion shared by every rule module.

Rules run on documents that may have already failed schema validation, so
every lookup into them has to tolerate the wrong shape turning up where a
mapping, an address or a network was expected -- a malformed entry is
skipped, not raised on. rules/network.py and rules/platform.py each
carried a byte-identical private copy of these three helpers (finding 13);
this is the one definition both now import.
"""
from __future__ import annotations

import ipaddress


def as_mapping(value: object) -> dict:
    """Return value when it is a mapping, else an empty one."""
    return value if isinstance(value, dict) else {}


def as_address(value: object):
    try:
        return ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return None


def as_network(value: object):
    try:
        return ipaddress.ip_network(value, strict=False)
    except (TypeError, ValueError):
        return None
