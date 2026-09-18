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
from .rules import load_catalogue
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


def _change_entry(path: str, left: object, right: object) -> dict:
    """Build one leaf diff entry, masking a credential-shaped value.

    redact() only masks a value that sits next to a credential-named dict
    key (findings text, a rendered spec) -- a diff entry instead stores the
    changed value under "left"/"right", with the field name living only in
    the JSON pointer. That means a changed password would slip straight
    through redact()'s key-based check. The credential name is available
    here as the pointer's last segment, so the masking has to happen at the
    point the entry is built.
    """
    key = path.rsplit("/", 1)[-1]
    if CREDENTIAL_KEY_RE.search(key):
        return {"path": path or "/", "left": _mask_credential(left),
                "right": _mask_credential(right)}
    return {"path": path or "/", "left": left, "right": right}


def _mask_credential(value: object) -> object:
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
    meta = load_catalogue()["INTERNAL"]
    finding = Finding(code=meta.code, severity=meta.severity, path="/",
                      message=f"{meta.summary} ({type(exc).__name__}).",
                      fix=meta.fix, source=meta.source, source_url=meta.source_url)
    return redact({"valid": False, "findings": [asdict(finding)]})


def call_handler(name: str, arguments: dict) -> dict:
    """Every tool call goes through here, so no traceback -- and no raw
    exception text -- ever reaches the caller.

    Unknown tool names, malformed arguments (including an unrecognised
    property: every schema sets additionalProperties: False) and a missing
    or wrong-typed value all land in the same except clause. That is a
    deliberate simplification, not sloppiness: the INTERNAL catalogue entry
    already covers "the tool failed", and inventing a second, uncatalogued
    code for "the tool was called wrong" would need its own review.
    """
    try:
        handler = HANDLERS[name]
        args = arguments or {}
        Draft202012Validator(TOOLS[name]["inputSchema"]).validate(args)
        return handler(args)
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
