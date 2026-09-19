"""Last line of defence: nothing leaves a tool without passing through here.

jsonschema embeds the offending value in its error messages, so a pasted secret
reaches a finding even though the reference contract says it never should.
"""
from __future__ import annotations

import re

from .inventory import REFERENCE_RE

MASK = "***REDACTED***"

# 'credential' deliberately absent: it matches the container, not a secret.
# 'thumbprint' deliberately absent: sslThumbprint/sshThumbprint are real fields.
CREDENTIAL_KEY_RE = re.compile(
    r"(password|passwd|secret|token|apikey|api_key|privatekey|private_key|"
    r"sshkey|ssh_key|esxroot|ssoadmin|nsxadmin|rootpass)",
    re.IGNORECASE,
)

# REFERENCE_RE used to be redefined here, byte-identical to inventory.py's
# copy (finding 13) -- imported from there instead, the same pattern
# credentials.py already follows for the same regex.

# No length floor. An earlier {6,} bound let "'Ab3!x' is too short" through
# verbatim -- a five-character ESXi root password is still a password, and
# a masker cannot tell a secret from a non-secret by counting characters.
# The cost is that a short non-secret ("'1610' is not of type 'integer'")
# is masked too; the finding's JSON pointer is the actionable part and is
# never masked.
_QUOTED_SECRET_RE = re.compile(
    r"'([^']+)'(?=\s+(?:is too short|is too long|does not match|is not of type))")

# The key may itself be quoted, because a Python repr of a containing dict
# writes `'password': 'x'` -- the quote between the name and the colon
# defeated the previous `password\s*[=:]` pattern. The value is matched in
# quoted form as well as bare, so the mask replaces the value and not the
# quotes around it.
_INLINE_SECRET_RE = re.compile(
    r"(?P<key>(?:password|passwd|secret|token|apikey|api_key)['\"]?\s*[=:]\s*)"
    r"(?P<value>'[^']*'|\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)


def _mask_inline(match: "re.Match[str]") -> str:
    value = match.group("value")
    quote = value[:1] if value[:1] in ("'", '"') else ""
    inner = value[1:-1] if quote else value
    if REFERENCE_RE.match(inner):
        return match.group(0)        # ${references} are not secrets
    return f"{match.group('key')}{quote}{MASK}{quote}"


def redact(value: object) -> object:
    """Return a copy of value with credential-shaped scalars masked."""
    if isinstance(value, dict):
        return {k: (MASK if _is_masked_scalar(k, v) else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _QUOTED_SECRET_RE.sub(f"'{MASK}'",
                                     _INLINE_SECRET_RE.sub(_mask_inline, value))
    return value


def _is_masked_scalar(key: object, value: object) -> bool:
    if not isinstance(key, str) or not CREDENTIAL_KEY_RE.search(key):
        return False
    if not isinstance(value, (str, int, float, bool)):
        return False        # never swallow a whole subtree
    return not (isinstance(value, str) and REFERENCE_RE.match(value))
