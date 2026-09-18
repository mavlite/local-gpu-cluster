import json

import jsonschema
import yaml

from vcfspec.inventory import EXAMPLE_PATH
from vcfspec.mcp_server import (HANDLERS, TOOLS, call_handler, tool_diff_spec,
                                 tool_explain_finding, tool_render_spec,
                                 tool_spec_schema, tool_validate_spec)

TEXT = EXAMPLE_PATH.read_text(encoding="utf-8")


def test_tools_and_handlers_agree():
    assert set(TOOLS) == set(HANDLERS)
    assert len(TOOLS) == 5


def test_spec_schema_lists_required_sections_and_ships_the_example():
    out = tool_spec_schema({})
    assert {"instance", "networks", "nsx", "hosts"} <= set(out["required"])
    assert out["example"].lstrip().startswith("apiVersion: vcfspec/v1")


def test_validate_tool_returns_the_envelope():
    out = tool_validate_spec({"document": TEXT})
    assert out["valid"] is True and "findings" in out


def test_render_tool_returns_a_spec():
    assert tool_render_spec({"document": TEXT})["spec"]["sddcId"] == "lab01"


def test_explain_finding_uses_the_catalogue():
    out = tool_explain_finding({"code": "VCF-NSX-FABRIC-MTU-TOO-LOW"})
    assert out["severity"] == "error"
    assert "1600" in out["summary"] and out["source_url"]


def test_explain_covers_codes_raised_outside_the_rule_modules():
    for code in ("VCF-INPUT-UNRECOGNISED", "VCF-CRED-NOT-A-REFERENCE",
                 "VCF-RENDER-UNMAPPED", "INTERNAL"):
        assert tool_explain_finding({"code": code})["severity"]


def test_explain_unknown_code_is_a_finding_not_an_exception():
    out = tool_explain_finding({"code": "NOPE"})
    assert out["findings"][0]["code"] == "VCF-EXPLAIN-UNKNOWN-CODE"


def test_diff_keys_hosts_by_name():
    changed = TEXT.replace("10.50.10.13", "10.50.10.99")
    out = tool_diff_spec({"left": TEXT, "right": changed})
    assert any("esx03" in entry["path"] for entry in out["changes"])


def test_diff_ignores_host_reordering():
    doc = yaml.safe_load(TEXT)
    doc["hosts"] = list(reversed(doc["hosts"]))
    out = tool_diff_spec({"left": TEXT, "right": yaml.safe_dump(doc)})
    assert out["changes"] == []


def test_handler_exception_becomes_an_internal_finding():
    out = call_handler("vcf_validate_spec", {})       # missing 'document'
    assert out["findings"][0]["code"] == "INTERNAL"
    assert out["valid"] is False


def test_unknown_tool_name_is_a_finding():
    assert call_handler("nope", {})["findings"][0]["code"] == "INTERNAL"


# --- Directive 1: no probe path anywhere in this server ---------------------

def test_no_tool_accepts_a_probe_config():
    for name, spec in TOOLS.items():
        assert "probe_config" not in spec["inputSchema"].get("properties", {})
        assert "probe" not in spec["inputSchema"].get("properties", {})


# --- Directive 3: an unexpected exception must never leak its own text -----

def test_handler_exception_never_leaks_unredacted_secret_text(monkeypatch):
    # A secret-shaped literal in an exotic shape redact() does not
    # recognise (no 'key=value' framing, no quoted-jsonschema framing).
    secret = "zzz-unrecognised-shape-9f8e7d6c5b4a"

    def boom(_args):
        raise RuntimeError(f"internal failure while holding {secret}")

    monkeypatch.setitem(HANDLERS, "vcf_validate_spec", boom)
    out = call_handler("vcf_validate_spec", {"document": TEXT})
    blob = json.dumps(out)
    assert secret not in blob
    assert out["findings"][0]["code"] == "INTERNAL"
    assert "RuntimeError" in blob


# --- Directive 4: TOOLS/HANDLERS cannot drift apart, structurally ----------

def test_every_advertised_tool_has_a_callable_handler():
    for name in TOOLS:
        assert callable(HANDLERS[name])


# --- Directive 5: schemas are real JSON Schema and close the door on typos -

def test_tool_schemas_are_valid_json_schema_and_closed():
    for name, spec in TOOLS.items():
        schema = spec["inputSchema"]
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_unknown_argument_is_rejected_not_silently_ignored():
    out = call_handler("vcf_explain_finding",
                        {"code": "INTERNAL", "bogus_typo": "x"})
    assert out["findings"][0]["code"] == "INTERNAL"


# --- Directive 6: a diff must never echo a changed credential value --------

def test_diff_never_echoes_a_changed_credential_value():
    left = yaml.safe_load(TEXT)
    left["credentials"]["esxRoot"] = "OldSuperSecret123!"
    right = yaml.safe_load(TEXT)
    right["credentials"]["esxRoot"] = "NewSuperSecret456!"
    out = tool_diff_spec({"left": yaml.safe_dump(left),
                          "right": yaml.safe_dump(right)})
    blob = json.dumps(out)
    assert "OldSuperSecret123!" not in blob
    assert "NewSuperSecret456!" not in blob
    assert any(entry["path"] == "/credentials/esxRoot" for entry in out["changes"])
