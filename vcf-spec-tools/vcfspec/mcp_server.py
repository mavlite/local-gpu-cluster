"""MCP wrapper. Holds no VCF knowledge: it only plumbs the core library.

No tool here accepts a probe_config, and none performs network I/O. That is
deliberate, not an oversight: the probe layer bounds a hung DNS lookup by
abandoning a daemon thread, which leaks that thread until the OS resolver
eventually returns. In the CLI that is harmless because the process exits
immediately afterwards. In a long-lived MCP server, repeated hung lookups
across many tool calls would grow threads without bound. If a future tool
genuinely needs probing, that needs its own design, not a quiet parameter
added here.

Every handler below is a plain function of (args) -> dict, so it is testable
without a live MCP client. call_handler() is the one boundary every call
actually reaches; it never lets an exception escape, and it never puts a raw
exception message in a finding -- only the exception's class name, because
redact() only masks shapes it recognises and arbitrary exception text raised
by application code is not one of them.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable, NamedTuple

from jsonschema import Draft202012Validator

from .api import render_document, validate_document
from .documents import load_document
from .findings import Finding
from .inventory import EXAMPLE_PATH, REFERENCE_RE, load_inventory_schema
from .redact import CREDENTIAL_KEY_RE, MASK, redact
from .rules import finding_for, load_catalogue
from .schema import DEFAULT_VERSION

_STRING = {"type": "string"}


def _schema(required: tuple[str, ...] = (), **properties: dict) -> dict:
    out: dict = {"type": "object", "properties": properties,
                "additionalProperties": False}
    if required:
        out["required"] = list(required)
    return out


# --- Tool bodies -------------------------------------------------------------

def tool_spec_schema(args: dict) -> dict:
    schema = load_inventory_schema()
    return {"required": schema["required"],
            "properties": sorted(schema["properties"]),
            "example": EXAMPLE_PATH.read_text(encoding="utf-8")}


def tool_validate_spec(args: dict) -> dict:
    return validate_document(args["document"], input_kind=args.get("input_kind"),
                             version=args.get("vcf_version", DEFAULT_VERSION))


def tool_render_spec(args: dict) -> dict:
    return render_document(args["document"],
                           version=args.get("vcf_version", DEFAULT_VERSION))


def tool_explain_finding(args: dict) -> dict:
    code = args.get("code", "")
    meta = load_catalogue().get(code)
    if meta is None:
        unknown = load_catalogue()["VCF-EXPLAIN-UNKNOWN-CODE"]
        finding = Finding(code=unknown.code, severity=unknown.severity, path="/",
                          message=f"No rule with code {code!r}.",
                          fix=unknown.fix, source=unknown.source,
                          source_url=unknown.source_url)
        return {"findings": [asdict(finding)]}
    return {"code": meta.code, "severity": str(meta.severity), "summary": meta.summary,
            "fix": meta.fix, "source": meta.source, "source_url": meta.source_url}


def tool_diff_spec(args: dict) -> dict:
    left = load_document(args["left"])
    right = load_document(args["right"])
    changes = _diff(left, right, "")
    return redact({"changes": changes, "changed": len(changes)})


# --- Diff engine --------------------------------------------------------

def _diff(left: object, right: object, path: str) -> list[dict]:
    if isinstance(left, dict) and isinstance(right, dict):
        out: list[dict] = []
        for key in sorted(set(left) | set(right)):
            out += _diff(left.get(key), right.get(key), f"{path}/{key}")
        return out
    if isinstance(left, list) and isinstance(right, list):
        return _diff_lists(left, right, path)
    if left != right:
        return [_change_entry(path, left, right)]
    return []


def _diff_lists(left: list, right: list, path: str) -> list[dict]:
    key = _list_key(left) or _list_key(right)
    if key:
        lmap = {item.get(key): item for item in left if isinstance(item, dict)}
        rmap = {item.get(key): item for item in right if isinstance(item, dict)}
        out: list[dict] = []
        for name in sorted(set(lmap) | set(rmap), key=str):
            out += _diff(lmap.get(name), rmap.get(name), f"{path}/{name}")
        return out
    if left != right:
        return [_change_entry(path, left, right)]
    return []


def _list_key(items: list) -> str | None:
    for candidate in ("name", "hostname", "networkType"):
        if items and isinstance(items[0], dict) and candidate in items[0]:
            return candidate
    return None


def _has_credentials_segment(path: str) -> bool:
    """True if any segment of path, at any depth, is 'credentials'
    (case-insensitive) -- a whole-segment match, not a prefix test, so a
    merely similar key like 'credentialsBackup' does not match.

    Depth matters here specifically because vcf_diff_spec never validates
    its input -- it diffs whatever it is handed, by design. A schema-
    conformant inventory can only have a free-form 'credentials' block at
    the document root (every other object in v1.schema.json sets
    additionalProperties: false), so "root-only" would be a sound rule for
    valid documents. But this tool's actual inputs are not guaranteed
    valid: a mid-edit draft, a hand-written fragment, or a document a
    previous pipeline step mangled can carry a 'credentials' segment
    nested anywhere, and the diff tool will render it regardless. Matching
    at any depth is what keeps that gap closed for the inputs this tool
    genuinely receives, not just the ones the schema would accept.
    """
    return any(segment.lower() == "credentials"
              for segment in path.split("/") if segment)


def _change_entry(path: str, left: object, right: object) -> dict:
    """Build one leaf diff entry, masking anything under a 'credentials'
    segment at any depth.

    redact() only masks a value that sits next to a credential-named dict
    key -- a diff entry instead stores the changed value under
    "left"/"right", with the field name living only in the JSON pointer, so
    a changed credential would otherwise slip straight through redact()'s
    key-based check.

    Masking here is keyed off *structural position* (this value sits under
    a 'credentials' segment), not the key's own name. A name-based check
    was tried first and missed credentials.vcenterRoot and
    credentials.sddcManagerRoot: CREDENTIAL_KEY_RE has no entry for either
    name, and the inventory schema declares no closed set of credential key
    names to draw one from -- `credentials` is `{"type": "object",
    "minProperties": 1}`, deliberately free-form (see inventory.py's
    _credential_findings, which likewise checks every entry in that block
    regardless of its name, never a fixed list). Structural position is the
    only source of truth this codebase actually has, so it is what covers
    every current and future credential name without needing to be taught
    each one individually.

    CREDENTIAL_KEY_RE is kept as a second, independent check for a
    credential-shaped key living outside any 'credentials' segment -- e.g.
    a stray "password" field elsewhere in the document.

    Known remaining limitation, stated plainly rather than papered over: a
    secret sitting under a key that looks nothing like a credential name,
    in a place the schema would forbid a free-form object at all, is not
    caught by either check. Nothing short of re-validating every diff
    input against the schema (which this tool deliberately does not do --
    it diffs, it does not validate) would close that, and doing so would
    change what this tool is.
    """
    key = path.rsplit("/", 1)[-1]
    if _has_credentials_segment(path) or CREDENTIAL_KEY_RE.search(key):
        return {"path": path or "/", "left": _mask_credential_value(left),
                "right": _mask_credential_value(right)}
    return {"path": path or "/", "left": left, "right": right}


def _mask_credential_value(value: object) -> object:
    """Mask every leaf of a value known to sit under a 'credentials' segment.

    Recurses through dicts and lists so a whole credentials block added or
    removed on one side (left is None, right is the entire dict, or vice
    versa) is masked entry by entry rather than emitted as one opaque
    unmasked blob or, worse, left unmasked because it never reached a
    dict-key check at all.
    """
    if isinstance(value, dict):
        return {k: _mask_credential_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_credential_value(v) for v in value]
    if value is None:
        return value
    if isinstance(value, str) and REFERENCE_RE.match(value):
        return value
    return MASK


# --- Tool registry: TOOLS and HANDLERS are derived from one list, so they
# cannot drift apart -- there is no second place to remember to update. ----

class ToolDef(NamedTuple):
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], dict]


_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef("vcf_spec_schema",
            "What a lab inventory needs, with a worked example.",
            _schema(),
            tool_spec_schema),
    ToolDef("vcf_render_spec",
            "Render a lab inventory into VCF Installer SddcSpec JSON.",
            _schema(("document",), document=_STRING, vcf_version=_STRING),
            tool_render_spec),
    ToolDef("vcf_validate_spec",
            "Validate an inventory or SddcSpec document; returns findings.",
            _schema(("document",), document=_STRING, vcf_version=_STRING,
                     input_kind=_STRING),
            tool_validate_spec),
    ToolDef("vcf_explain_finding",
            "Explain one finding code, with its severity, fix and documentation source.",
            _schema(("code",), code=_STRING),
            tool_explain_finding),
    ToolDef("vcf_diff_spec",
            "Semantic diff of two inventories or specs; credential values are masked.",
            _schema(("left", "right"), left=_STRING, right=_STRING),
            tool_diff_spec),
)

TOOLS = {t.name: {"description": t.description, "inputSchema": t.schema}
        for t in _TOOL_DEFS}
HANDLERS = {t.name: t.handler for t in _TOOL_DEFS}


def _internal_finding(exc: Exception) -> dict:
    """The tool itself failed -- an agent should not blindly retry this.

    Only the exception's class name reaches the message, never str(exc):
    redact() only masks shapes it recognises (jsonschema echoes,
    key=/secret= patterns), and arbitrary text raised by a handler body
    operating on operator data could carry spec content straight through it.
    """
    finding = finding_for("INTERNAL", "/", detail=type(exc).__name__)
    return redact({"valid": False, "findings": [asdict(finding)]})


def _bad_args_finding(tool: str, detail: str) -> dict:
    """The *call* was malformed -- an agent should fix its arguments and
    retry, unlike an INTERNAL finding. Distinct from INTERNAL (severity
    critical) so a caller can tell "you called this wrong" apart from "this
    tool is broken"; both this and _internal_finding only ever pass a class
    name or a tool name through, never exception text or instance data --
    a jsonschema.ValidationError's own .message can echo the offending
    value verbatim (that is the whole reason schema_layer.py redacts it
    before use), and that is exactly the operator data this boundary must
    never leak.
    """
    finding = finding_for("VCF-MCP-BAD-ARGS", "/", tool=tool, detail=detail)
    return redact({"valid": False, "findings": [asdict(finding)]})


def call_handler(name: str, arguments: dict) -> dict:
    """Every tool call goes through here, so no traceback -- and no raw
    exception or validation-error text -- ever reaches the caller.

    Two failure classes are told apart deliberately: an unknown tool name
    or arguments that don't match the advertised schema (missing required
    field, wrong type, unrecognised property -- every schema sets
    additionalProperties: False) are VCF-MCP-BAD-ARGS, a retryable usage
    error. Anything a handler itself raises once called with schema-valid
    arguments is INTERNAL, a non-retryable tool failure.
    """
    if name not in HANDLERS:
        return _bad_args_finding(name, "unknown tool name")
    args = arguments or {}
    try:
        Draft202012Validator(TOOLS[name]["inputSchema"]).validate(args)
    except Exception as exc:
        return _bad_args_finding(name, type(exc).__name__)
    try:
        return HANDLERS[name](args)
    except Exception as exc:
        return _internal_finding(exc)


# --- Transport: only reached when the optional 'mcp' extra is installed ----

def build_server():
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server = Server("vcfspec")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name=name, description=spec["description"],
                     inputSchema=spec["inputSchema"])
                for name, spec in TOOLS.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return [TextContent(type="text",
                            text=json.dumps(call_handler(name, arguments), indent=2))]

    return server


def main() -> int:
    import asyncio

    from mcp.server.stdio import stdio_server

    async def run() -> None:
        server = build_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
