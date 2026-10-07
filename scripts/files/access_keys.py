"""Who is calling the router, and what they may do (workforce design rev 2, §5.3).

Two kinds of principal:
  owner   -- ROUTER_API_KEY from /etc/router.env; full access, unchanged behaviour.
  scoped  -- per-run keys in ROUTER_KEYS_FILE (JSON, SHA-256 hashes only): may call only chat
             completions and the model list, only their allowlisted aliases, never server-side
             tools, never trigger a profile swap; expire at `expires` (epoch seconds, required).

The keys file is re-read whenever it is replaced or changes (inode, mtime or size), so `router-keys.py revoke` takes effect on the
next request. A missing or malformed file fails closed for scoped keys; the owner key still works. An entry that
fails validation (hash not 64 hex chars, aliases not a non-empty list of strings, expiry not a
finite number, reserved or unsafe name) is skipped; the other entries keep working.
Key file shape:
    {"keys": [{"name": "wf-run-1", "sha256": "<hex>", "aliases": ["qwen3.8-nothink"],
               "expires": 1791500000.0}]}
"""
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("access_keys")


@dataclass(frozen=True)
class Principal:
    name: str
    kind: str                       # "owner" | "scoped"
    aliases: frozenset
    expires: Optional[float]

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"


OWNER = Principal("owner", "owner", frozenset(), None)

# (method, path) a scoped key may call; everything else is refused.
SCOPED_ENDPOINTS = frozenset({("POST", "/v1/chat/completions"), ("GET", "/v1/models")})

# Same rule as router-keys.py: names appear in the access log, so "owner" is reserved.
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _parse_entry(e) -> tuple:
    """(sha256, Principal) for a valid keys-file entry; ValueError otherwise."""
    name, digest, aliases, expires = e["name"], e["sha256"], e["aliases"], e["expires"]
    if not (isinstance(name, str) and NAME_RE.fullmatch(name) and name != OWNER.name):
        raise ValueError("name")
    if not (isinstance(digest, str) and _SHA256_RE.fullmatch(digest.lower())):
        raise ValueError("sha256")
    if not (isinstance(aliases, list) and aliases and all(isinstance(a, str) and a for a in aliases)):
        raise ValueError("aliases")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
        raise ValueError("expires")
    return digest.lower(), Principal(name, "scoped", frozenset(aliases), float(expires))


class KeyStore:
    def __init__(self, path: str, owner_key: str, clock: Callable[[], float] = time.time):
        self._path = path
        self._owner = owner_key or ""
        self._clock = clock
        self._sig: Optional[tuple] = None
        self._entries: tuple = ()

    def _reload_if_changed(self) -> None:
        try:
            st = os.stat(self._path)
        except OSError:
            self._sig, self._entries = None, ()
            return
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)     # router-keys replaces the file: new inode
        if sig == self._sig:
            return
        self._sig = sig
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f).get("keys", [])
        except (OSError, ValueError, AttributeError) as e:
            log.error("router keys file unreadable, scoped keys disabled: %s", type(e).__name__)
            self._entries = ()
            return
        entries = []
        for e in raw if isinstance(raw, list) else []:
            try:
                entries.append(_parse_entry(e))
            except (KeyError, TypeError, ValueError):
                log.error("router keys file: skipping malformed entry")
        self._entries = tuple(entries)

    def authenticate(self, presented: str) -> Optional[Principal]:
        if not presented:
            return None
        if self._owner and hmac.compare_digest(presented.encode(), self._owner.encode()):
            return OWNER
        self._reload_if_changed()
        digest = hashlib.sha256(presented.encode()).hexdigest()
        match = None
        for stored, principal in self._entries:      # no early exit: same work whatever matches
            if hmac.compare_digest(digest, stored):
                match = principal
        if match is None:
            return None
        if match.expires is not None and self._clock() >= match.expires:
            return None
        return match


def endpoint_allowed(principal: Principal, method: str, path: str) -> bool:
    return principal.is_owner or (method.upper(), path) in SCOPED_ENDPOINTS


def chat_violation(principal: Principal, body: dict) -> Optional[str]:
    """None if the chat request is allowed for this principal, else an error code."""
    if principal.is_owner:
        return None
    model = body.get("model")
    if not isinstance(model, str) or model not in principal.aliases:
        return "model_not_allowed"
    if body.get("tool_execution") == "server":
        return "server_tools_not_allowed"
    return None


def apply_chat_policy(principal: Principal, body: dict) -> dict:
    """Scoped keys always run client-side tools, whatever TOOL_EXECUTION_DEFAULT says.
    Returns a new dict for scoped principals; the owner's body is returned unchanged."""
    if principal.is_owner:
        return body
    return {**body, "tool_execution": "client"}


def may_swap(principal: Principal) -> bool:
    return principal.is_owner
