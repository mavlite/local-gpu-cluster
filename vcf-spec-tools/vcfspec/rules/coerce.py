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


def as_sequence(value: object) -> list:
    """Return value's items when it is a list or tuple, else [].

    rules/platform.py carried a private `_sequence()` that did exactly this;
    this is the one definition it (and every other rules/provision/probes
    call site that used the `or []` idiom instead) now imports.

    `or []` only rescues a *falsy* wrong value -- a truthy non-iterable
    (`hosts: 5`, `hosts: true`) sails straight through into enumerate() or
    len() and raises TypeError, reaching the operator as a traceback
    instead of a finding. This coerces every wrong shape uniformly, so
    "value that isn't a list or tuple" always means "treat as no entries",
    never "maybe crash, maybe not, depending on truthiness".

    A `str` (or `bytes`) is deliberately in the "else []" branch, not
    treated as iterable: iterating a string yields its characters, so
    `hosts: "esx01"` would otherwise report 5 phantom hosts fed straight
    into capacity arithmetic -- the same garbling class already fixed for
    dns.nameservers, nsx.managers and the resolver-config reader (see
    `_address_strings()` in vcfspec/validate/probes.py). That helper
    returns None instead of [] for an unusable shape, because it is
    guarding an injected caller *seam* whose contract is undocumented and
    it has to tell "broken seam" apart from "seam answered, empty". This
    helper only ever handles DOCUMENT data -- a value straight out of
    yaml.safe_load -- where a malformed shape is a validation fact, not an
    integration failure, so collapsing it to [] (rather than returning a
    third state the rules layer would have to check for) is the right
    call: "no usable entries" is exactly what every caller here wants to
    do with it.
    """
    return list(value) if isinstance(value, (list, tuple)) else []
