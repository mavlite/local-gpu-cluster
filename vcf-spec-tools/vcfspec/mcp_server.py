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
from .inventory import EXAMPLE_PATH, REFERENCE_RE, load_inventory_schema
from .redact import CREDENTIAL_KEY_RE, MASK, redact
from .rules import finding_for, load_catalogue
from .schema import DEFAULT_VERSION, known_versions

# Every string argument carries a maxLength, enforced by the
# Draft202012Validator in call_handler() before any handler body runs.
#
# MAX_DOCUMENT_CHARS is the server-surface answer to finding 5. The
# library's own documents.MAX_BYTES (2 MB) stays where it is -- the CLI
# reads an operator's own file under their own account and a big one there
# costs only their own patience -- but the MCP server is long-lived,
# single-process and asyncio-driven, and vcf_diff_spec parses *two*
# documents per call. A real 3-host inventory is ~1.5 KB and the largest
# rendered SddcSpec this package produces is a few tens of KB, so 256 K
# characters is ~170x headroom over any legitimate input while cutting the
# worst-case parse to a fraction of a second. Note this bounds characters,
# not bytes; documents.MAX_BYTES remains the byte-side backstop underneath.
MAX_DOCUMENT_CHARS = 262_144
MAX_VERSION_CHARS = 32          # longest vendored version is 7 characters
MAX_CODE_CHARS = 128            # longest catalogue code is ~32 characters

_STRING = {"type": "string"}
_DOCUMENT = {"type": "string", "maxLength": MAX_DOCUMENT_CHARS}
_VERSION = {"type": "string", "maxLength": MAX_VERSION_CHARS}
_CODE = {"type": "string", "maxLength": MAX_CODE_CHARS}
# A closed enum, not a bare string: the input_kind documents.detect_kind()
# actually accepts is exactly {"inventory", "sddc_spec"} (see documents.py's
# _VALID_OVERRIDES). Declaring that here means a bad value -- "unknown"
# included, which used to parse as DocumentKind.UNKNOWN and silently skip
# every layer -- is rejected at this boundary as VCF-MCP-BAD-ARGS before
# the handler body ever runs, not discovered several layers in.
_INPUT_KIND = {"type": "string", "enum": ["inventory", "sddc_spec"]}


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
    version = args.get("vcf_version", DEFAULT_VERSION)
    result = validate_document(args["document"], input_kind=args.get("input_kind"),
                               version=version)
    return _reclassify_unknown_version(result, "vcf_validate_spec", version)


def tool_render_spec(args: dict) -> dict:
    version = args.get("vcf_version", DEFAULT_VERSION)
    result = render_document(args["document"], version=version,
                             input_kind=args.get("input_kind"))
    return _reclassify_unknown_version(result, "vcf_render_spec", version)


def _reclassify_unknown_version(result: dict, tool: str, version: str) -> dict:
    """VCF-SCHEMA-VERSION-UNKNOWN (from api.py) means the caller asked for a
    VCF release this package has nothing vendored for -- a bad argument to
    retry with a real version, not a broken tool. INTERNAL tells an agent
    "do not retry this"; that is the wrong message here, since retrying
    with a real vcf_version would work fine. This is the same distinction
    VCF-MCP-BAD-ARGS exists for (see call_handler's docstring), so a
    version-not-vendored result is translated to it here, uniformly for
    both tools that accept vcf_version, rather than left to surface
    through render_document/validate_document's own generic framing (or,
    before api.py grew its own guard for this, an uncaught
    FileNotFoundError that call_handler could only label INTERNAL).

    Only fires when the version was actually consulted and found missing
    (the code is present in the real findings) -- an inventory-kind
    validate call with an irrelevant, unused vcf_version is not rejected
    just for carrying one, because that argument was never actually acted
    on for that document.

    Translates that ONE finding and keeps every other finding the layers
    below already computed -- it used to replace the whole envelope with
    a single VCF-MCP-BAD-ARGS finding, discarding real schema/rules
    findings the same call had already earned (e.g. vcf_render_spec on a
    valid inventory with an unknown vcf_version used to report exactly
    `['VCF-MCP-BAD-ARGS']`, throwing away VCF-LIC-EVALUATION and anything
    else the render pipeline found). The translation happens in place, so
    a caller inspecting findings[i] for the position they last saw still
    finds the same finding, just recoded.
    """
    findings = result.get("findings", [])
    if not any(f["code"] == "VCF-SCHEMA-VERSION-UNKNOWN" for f in findings):
        return result
    # `detail` names the vendored versions, never the one the caller asked
    # for. Findings 1/2: the rejection must read identically for every
    # rejected version, and echoing an arbitrary caller string into an
    # envelope an agent reads is a surface worth not having. `version` is
    # still a parameter because the caller-facing signature has not
    # changed and the value is used for nothing else here.
    bad_args = asdict(finding_for(
        "VCF-MCP-BAD-ARGS", "/", tool=tool,
        detail="requested VCF version is not vendored; vendored: "
               + (", ".join(sorted(known_versions())) or "(none)")))
    new_findings = [bad_args if f["code"] == "VCF-SCHEMA-VERSION-UNKNOWN" else f
                   for f in findings]
    valid = not any(f["severity"] in ("critical", "error") for f in new_findings)
    return {**result, "findings": new_findings, "valid": valid}


def tool_explain_finding(args: dict) -> dict:
    """Always the same flat shape -- {code, severity, summary, fix, source,
    source_url} -- whether or not the code was found. Before this, the
    found path returned that flat shape and the not-found path wrapped a
    Finding in {"findings": [...]}: two different shapes from one tool
    depending on its own argument, which is the self-inconsistency finding
    8 calls out by name. An unrecognised code is not a protocol error (the
    call was well-formed; the catalogue just has nothing under that key),
    so it is reported the same way a found code is: as an explanation --
    here, an explanation of VCF-EXPLAIN-UNKNOWN-CODE itself, with the
    caller's own (invalid) code named in its summary.

    No `valid` key, on this path or the found one: this tool explains a
    code, it does not validate a document, and forcing a `valid` value
    here would be answering a question nobody asked (see call_handler's
    envelope-shaping for the same call on this tool's own bad-args path).
    """
    code = args.get("code", "")
    meta = load_catalogue().get(code)
    if meta is None:
        unknown = load_catalogue()["VCF-EXPLAIN-UNKNOWN-CODE"]
        return {"code": unknown.code, "severity": str(unknown.severity),
                "summary": f"No rule with code {code!r}.",
                "fix": unknown.fix, "source": unknown.source,
                "source_url": unknown.source_url}
    return {"code": meta.code, "severity": str(meta.severity), "summary": meta.summary,
            "fix": meta.fix, "source": meta.source, "source_url": meta.source_url}


def tool_diff_spec(args: dict) -> dict:
    """Structural diff of two documents, with a uniform findings/valid
    envelope: `valid` here means "both inputs were readable", not "these
    are valid VCF specs" -- vcf_diff_spec never validates its input, by
    design (see _change_entry's docstring on why it deliberately does
    not), so `valid: false` never means anything more than "one side
    could not be parsed" and `findings` never carries anything but that.

    Before this, an unparseable 'left' or 'right' fell through to
    call_handler's catch-all and came back as INTERNAL -- "the tool is
    broken, do not retry" -- for the identical condition
    vcf_validate_spec reports as VCF-INPUT-UNREADABLE, a retryable bad
    argument (finding 7). load_document() is called here, the same way
    api.py's own loader boundary calls it, so the same exception family
    (DocumentTooLarge, DocumentTooDeep, DocumentTooManyAliases,
    DocumentTooManyNodes, a yaml.YAMLError, a non-mapping root) becomes
    the same code an agent already knows how to act on. The path names
    which side failed ("/left" or "/right"), which single-document tools
    have no equivalent need for.
    """
    documents: dict[str, dict] = {}
    for side in ("left", "right"):
        try:
            documents[side] = load_document(args[side])
        except Exception as exc:
            finding = finding_for("VCF-INPUT-UNREADABLE", f"/{side}",
                                  exc_type=type(exc).__name__)
            return redact({"valid": False, "findings": [asdict(finding)]})
    changes = _diff(documents["left"], documents["right"], "")
    return redact({"valid": True, "findings": [], "changes": changes,
                   "changed": len(changes)})


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
    # Envelope shape (finding 8): reports_valid is False for the two tools
    # that validate nothing (vcf_spec_schema, vcf_explain_finding) -- for
    # those, `valid` is never present, on success OR failure, rather than
    # being absent on success and `false` on failure, which is the shape
    # a consumer cannot safely branch on (see _error_envelope). reports_layers
    # is True only for the two tools whose pipeline actually has layers.
    reports_valid: bool = True
    reports_layers: bool = False


_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef("vcf_spec_schema",
            "What a lab inventory needs, with a worked example.",
            _schema(),
            tool_spec_schema,
            reports_valid=False),
    ToolDef("vcf_render_spec",
            "Render a lab inventory into VCF Installer SddcSpec JSON.",
            _schema(("document",), document=_DOCUMENT, vcf_version=_VERSION,
                     input_kind=_INPUT_KIND),
            tool_render_spec,
            reports_layers=True),
    ToolDef("vcf_validate_spec",
            "Validate an inventory or SddcSpec document; returns findings.",
            _schema(("document",), document=_DOCUMENT, vcf_version=_VERSION,
                     input_kind=_INPUT_KIND),
            tool_validate_spec,
            reports_layers=True),
    ToolDef("vcf_explain_finding",
            "Explain one finding code, with its severity, fix and documentation source.",
            _schema(("code",), code=_CODE),
            tool_explain_finding,
            reports_valid=False),
    ToolDef("vcf_diff_spec",
            "Semantic diff of two inventories or specs; credential values are masked.",
            _schema(("left", "right"), left=_DOCUMENT, right=_DOCUMENT),
            tool_diff_spec),
)

TOOLS = {t.name: {"description": t.description, "inputSchema": t.schema}
        for t in _TOOL_DEFS}
HANDLERS = {t.name: t.handler for t in _TOOL_DEFS}
_TOOL_DEFS_BY_NAME = {t.name: t for t in _TOOL_DEFS}


def _error_envelope(tool: ToolDef | None, finding) -> dict:
    """Build a failure envelope shaped like the tool it failed for --
    finding 8's fix. `valid` is added exactly when the tool has a validity
    concept at all (every tool but vcf_spec_schema and vcf_explain_finding,
    which validate nothing); `tool is None` means the name itself did not
    match a real tool, so there is no profile to consult and `valid` is
    included as a conservative default, matching every other tool's shape.
    `layers_run`/`layers_skipped` are added only for the two tools whose
    pipeline actually has layers (vcf_validate_spec, vcf_render_spec), and
    on THIS failure path too -- not just their success path -- so
    `result["layers_run"]` never raises on a failed call, which is exactly
    what the README teaches an operator to read before trusting a result.
    """
    envelope: dict = {"findings": [asdict(finding)]}
    if tool is None or tool.reports_valid:
        envelope["valid"] = False
    if tool is not None and tool.reports_layers:
        envelope["layers_run"] = []
        envelope["layers_skipped"] = {}
    return redact(envelope)


def _internal_finding(tool: ToolDef | None, exc: Exception) -> dict:
    """The tool itself failed -- an agent should not blindly retry this.

    Only the exception's class name reaches the message, never str(exc):
    redact() only masks shapes it recognises (jsonschema echoes,
    key=/secret= patterns), and arbitrary text raised by a handler body
    operating on operator data could carry spec content straight through it.
    """
    finding = finding_for("INTERNAL", "/", detail=type(exc).__name__)
    return _error_envelope(tool, finding)


def _bad_args_finding(tool: ToolDef | None, tool_name: str, detail: str) -> dict:
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
    finding = finding_for("VCF-MCP-BAD-ARGS", "/", tool=tool_name, detail=detail)
    return _error_envelope(tool, finding)


def call_handler(name: str, arguments: dict) -> dict:
    """Every tool call goes through here, so no traceback -- and no raw
    exception or validation-error text -- ever reaches the caller.

    Two failure classes are told apart deliberately: an unknown tool name
    or arguments that don't match the advertised schema (missing required
    field, wrong type, unrecognised property -- every schema sets
    additionalProperties: False) are VCF-MCP-BAD-ARGS, a retryable usage
    error. Anything a handler itself raises once called with schema-valid
    arguments is INTERNAL, a non-retryable tool failure.

    Dispatch still goes through the mutable HANDLERS dict (not
    tool.handler) so a caller can substitute a handler for testing;
    _TOOL_DEFS_BY_NAME is consulted only to shape the error envelope for
    the real tool being called (finding 8), which is a separate concern
    from which function actually runs.
    """
    tool = _TOOL_DEFS_BY_NAME.get(name)
    if name not in HANDLERS:
        return _bad_args_finding(None, name, "unknown tool name")
    args = arguments or {}
    try:
        Draft202012Validator(TOOLS[name]["inputSchema"]).validate(args)
    except Exception as exc:
        return _bad_args_finding(tool, name, type(exc).__name__)
    try:
        return HANDLERS[name](args)
    except Exception as exc:
        return _internal_finding(tool, exc)


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
