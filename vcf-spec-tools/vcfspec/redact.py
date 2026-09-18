"""Last line of defence: nothing leaves a tool without passing through here.

jsonschema embeds the offending value in its error messages, so a pasted secret
reaches a finding even though the reference contract says it never should.
"""
from __future__ import annotations

import re

MASK = "***REDACTED***"

# 'credential' deliberately absent: it matches the container, not a secret.
# 'thumbprint' deliberately absent: sslThumbprint/sshThumbprint are real fields.
CREDENTIAL_KEY_RE = re.compile(
    r"(password|passwd|secret|token|apikey|api_key|privatekey|private_key|"
    r"sshkey|ssh_key|esxroot|ssoadmin|nsxadmin|rootpass)",
    re.IGNORECASE,
)

REFERENCE_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

_QUOTED_SECRET_RE = re.compile(
    r"'([^']{6,})'(?=\s+(?:is too short|is too long|does not match|is not of type))")
_INLINE_SECRET_RE = re.compile(
    r"((?:password|passwd|secret|token)\s*[=:]\s*)(\S+)", re.IGNORECASE)


def redact(value: object) -> object:
    """Return a copy of value with credential-shaped scalars masked."""
    if isinstance(value, dict):
        return {k: (MASK if _is_masked_scalar(k, v) else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _QUOTED_SECRET_RE.sub(f"'{MASK}'",
                                     _INLINE_SECRET_RE.sub(r"\1" + MASK, value))
    return value


def _is_masked_scalar(key: object, value: object) -> bool:
    if not isinstance(key, str) or not CREDENTIAL_KEY_RE.search(key):
        return False
    if not isinstance(value, (str, int, float, bool)):
        return False        # never swallow a whole subtree
    return not (isinstance(value, str) and REFERENCE_RE.match(value))
