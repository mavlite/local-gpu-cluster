"""The one structural rule behind "these tools never hold a real secret".

A credential field holds a `${reference}`; a literal value is refused. That
constraint used to be enforced by a loop over the *lab inventory's* root
`credentials` block, which meant it ran on inventories and silently did
not run on `sddc_spec` documents -- and an SddcSpec is a first-class,
advertised input (`vcf_validate_spec` accepts `input_kind: "sddc_spec"`).
The vendored VMware schema is perfectly happy with a real password, since
real passwords are what it is for, so nothing else in that branch objected.

So the rule lives here instead, as a structural walk that both document
kinds share. Two independent ways a position counts as credential-bearing,
neither of which needs to be taught each new field name one at a time:

1. **Structural position** -- the value sits inside a mapping reached
   through a `credentials` segment, at any depth. This is what covers the
   inventory's own free-form block, whose schema is
   `{"type": "object", "minProperties": 1}` with no declared key names at
   all: `vcenterRoot` and `sddcManagerRoot` are real credential keys that
   CREDENTIAL_KEY_RE has never heard of, and a hand-written inventory may
   invent more. It also covers `hostSpecs[].credentials` on the SddcSpec
   side. This is the same reasoning mcp_server._change_entry records for
   the diff tool: structural position is the only source of truth this
   codebase actually has.
2. **A credential-shaped key name** -- CREDENTIAL_KEY_RE, anywhere in the
   document. This is what covers the SddcSpec's credential fields that
   live *outside* any credentials block: `rootVcenterPassword`,
   `adminUserSsoPassword`, `rootNsxtManagerPassword`, `nsxtAdminPassword`,
   `sddcManagerSpec.rootPassword`, and the rest. Every credential-bearing
   property name in the vendored 9.1.1.0 schema matches it (verified by
   tests/test_credentials.py::test_every_vendored_credential_property_is_covered),
   and the inventory's `esxRoot`/`ssoAdmin`/`nsxAdmin` do too.

`username` is the one name exempted under a `credentials` segment. It is
not a guess: `SddcCredentials` in the vendored schema declares exactly
`username` and `password`, a username is a login name rather than a
secret, and render.py emits `"username": "root"` as a literal on purpose.
Without the exemption the renderer's own correct output would be reported
as an insecure credential.
"""
from __future__ import annotations

from .findings import Finding, Result, Severity
from .inventory import REFERENCE_RE
from .redact import CREDENTIAL_KEY_RE

# Declared, non-secret members of a credentials block. Compared lowercased.
NON_SECRET_CREDENTIAL_KEYS = frozenset({"username"})

_CREDENTIALS_SEGMENT = "credentials"


def _escape_segment(segment: str) -> str:
    """RFC 6901 encoding: '~' before '/', or an escaped slash is re-escaped."""
    return segment.replace("~", "~0").replace("/", "~1")


def _is_reference(value: object) -> bool:
    return isinstance(value, str) and bool(REFERENCE_RE.match(value))


def _finding(pointer: str, name: str) -> Finding:
    return Finding(
        code="VCF-CRED-NOT-A-REFERENCE", severity=Severity.CRITICAL,
        path=pointer,
        message=(f"Credential '{name}' is not a reference. These tools "
                 "never hold secrets."),
        fix="Use ${name}, e.g. ${esx_root}; resolve it at submit time.",
        source="docs")


def _walk(node: object, pointer: str, in_credentials: bool,
          out: dict[str, Finding]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            child = f"{pointer}/{_escape_segment(name)}"
            under = in_credentials or name.lower() == _CREDENTIALS_SEGMENT
            if _is_credential_leaf(name, value, in_credentials):
                out.setdefault(child, _finding(child, name))
                continue
            _walk(value, child, under, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{pointer}/{index}", in_credentials, out)


def _is_credential_leaf(name: str, value: object, in_credentials: bool) -> bool:
    """True when this entry must hold a ${reference} and does not.

    Inside a credentials block a container is reported rather than
    descended into: `credentials: {esxRoot: {...}}` is a malformed
    credential whichever way you read it, and reporting it is what
    inventory validation has always done.

    Outside one, a credential-shaped *key name* only flags a scalar. A
    container under such a name is descended into instead, for the same
    reason redact() refuses to mask a subtree by key name alone: a name
    like `passwordPolicy` is a settings object, not a secret, and
    swallowing it whole would report a finding about a value that does not
    exist.
    """
    if in_credentials and name.lower() in NON_SECRET_CREDENTIAL_KEYS:
        return False
    if in_credentials:
        return not _is_reference(value)
    if not CREDENTIAL_KEY_RE.search(name):
        return False
    if not isinstance(value, (str, int, float, bool)):
        return False
    return not _is_reference(value)


def credential_findings(doc: object) -> Result:
    """Every position in `doc` that holds a literal instead of a ${reference}.

    Pure: `doc` is never mutated. Findings are ordered by JSON pointer so
    the output is stable regardless of mapping order.
    """
    out: dict[str, Finding] = {}
    _walk(doc, "", False, out)
    return Result(tuple(out[key] for key in sorted(out)))
