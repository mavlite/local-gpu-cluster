import json

import jsonschema
import pytest
import yaml

from vcfspec.inventory import EXAMPLE_PATH
from vcfspec.mcp_server import (HANDLERS, TOOLS, call_handler, tool_diff_spec,
                                 tool_explain_finding, tool_render_spec,
                                 tool_spec_schema, tool_validate_spec)
from vcfspec.rules import load_catalogue

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


def test_explain_unknown_code_is_explained_not_an_exception():
    # Finding 8: the not-found path returns the same flat shape as the
    # found path (code/severity/summary/fix/source/source_url), not a
    # {"findings": [...]} wrapper -- tool_explain_finding used to be
    # inconsistent with itself, returning a different shape depending on
    # whether the code existed.
    out = tool_explain_finding({"code": "NOPE"})
    assert out["code"] == "VCF-EXPLAIN-UNKNOWN-CODE"
    # This used to assert `"NOPE" in out["summary"]`. Inverted on
    # 2026-09-19: the requested code is no longer echoed back. It handed
    # an agent up to 128 caller-chosen characters inside a message it
    # reads, for no benefit -- the caller already knows what it asked.
    assert "NOPE" not in out["summary"]
    assert out["summary"]
    assert out["severity"]
    assert "findings" not in out
    assert "valid" not in out


def test_diff_keys_hosts_by_name():
    changed = TEXT.replace("10.50.10.13", "10.50.10.99")
    out = tool_diff_spec({"left": TEXT, "right": changed})
    assert any("esx03" in entry["path"] for entry in out["changes"])


def test_diff_ignores_host_reordering():
    doc = yaml.safe_load(TEXT)
    doc["hosts"] = list(reversed(doc["hosts"]))
    out = tool_diff_spec({"left": TEXT, "right": yaml.safe_dump(doc)})
    assert out["changes"] == []


def test_missing_required_arg_becomes_a_bad_args_finding():
    # Fix round 1, recommendation 2: a malformed call is a retryable usage
    # error (VCF-MCP-BAD-ARGS), not INTERNAL -- INTERNAL is reserved for a
    # handler that raised after being called with schema-valid arguments.
    out = call_handler("vcf_validate_spec", {})       # missing 'document'
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"
    assert out["valid"] is False


def test_unknown_tool_name_is_a_bad_args_finding():
    out = call_handler("nope", {})
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"


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
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"


# --- Fix round 1, recommendation 2: a distinct, catalogued usage-error code -

def test_bad_args_code_exists_in_the_catalogue_at_error_severity():
    meta = load_catalogue()["VCF-MCP-BAD-ARGS"]
    assert str(meta.severity) == "error"


def test_bad_args_finding_never_leaks_the_offending_value():
    # jsonschema.ValidationError.message can echo the instance value
    # verbatim (schema_layer.py's own docstring calls this out) -- a wrong-
    # typed 'document' carrying structured secret-shaped data must not
    # leak through the bad-args path either.
    secret = "TotallyLeakedSecretXYZ999"
    out = call_handler("vcf_validate_spec",
                       {"document": {"credentials": {"password": secret}}})
    blob = json.dumps(out)
    assert secret not in blob
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"


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


# --- Fix round 3: input_kind is a closed enum, and an unvendored
# vcf_version is a usage error, not an internal failure, for both tools
# that accept one. Both used to reach the operator as "valid: true, zero
# findings" (input_kind="unknown") or a bare INTERNAL finding
# (vcf_version) respectively -- a wrong, confidently-delivered answer in
# the first case, and a "do not retry" signal for something retrying with
# a real version would fix in the second.

def test_input_kind_unknown_is_rejected_at_the_mcp_boundary():
    # "unknown" is DocumentKind's own internal sentinel, not a value
    # documents.detect_kind() accepts as an override (see
    # test_documents.py::test_override_literally_spelled_unknown_is_also_a_finding).
    # The tool schema's enum must reject it before the handler body even
    # runs, exactly like any other malformed call.
    out = call_handler("vcf_validate_spec", {"document": TEXT, "input_kind": "unknown"})
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"
    assert out["valid"] is False


def test_input_kind_typo_is_rejected_at_the_mcp_boundary():
    out = call_handler("vcf_validate_spec", {"document": TEXT, "input_kind": "inventroy"})
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"


def test_input_kind_enum_is_declared_on_the_real_tool_schema():
    schema = TOOLS["vcf_validate_spec"]["inputSchema"]
    assert schema["properties"]["input_kind"]["enum"] == ["inventory", "sddc_spec"]


def test_unvendored_vcf_version_is_a_bad_args_finding_for_validate():
    # vcf_validate_spec only ever consults vcf_version for an SDDC_SPEC-kind
    # document (see api.py) -- build a real, valid one first (via the
    # render tool, which never fails on the bundled example) so the
    # version is the *only* thing wrong with the call.
    #
    # Finding 10: the bad-args finding is translated in place, not left as
    # the *only* finding -- so this checks membership, not position, and a
    # sibling test below (test_reclassify_keeps_every_other_finding) checks
    # that a real finding computed alongside it survives.
    spec_text = json.dumps(tool_render_spec({"document": TEXT})["spec"])
    out = call_handler("vcf_validate_spec",
                       {"document": spec_text, "input_kind": "sddc_spec",
                        "vcf_version": "9.9.9.9"})
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-MCP-BAD-ARGS" in codes
    assert "INTERNAL" not in codes
    bad_args = next(f for f in out["findings"] if f["code"] == "VCF-MCP-BAD-ARGS")
    # Security review findings 1/2: this assertion used to be
    # `"9.9.9.9" in bad_args["message"]`. It is now its inverse, on
    # purpose. Every rejected version must produce an identical envelope
    # (see test_security_version.py), which a message quoting the
    # caller's own string cannot do -- and reflecting arbitrary caller
    # text into an envelope an AI agent reads is a surface worth not
    # having. The vendored set is named instead, which is the thing a
    # legitimate caller actually needs.
    assert "9.9.9.9" not in bad_args["message"]
    assert "9.1.1.0" in bad_args["message"]


def test_unvendored_vcf_version_is_a_bad_args_finding_for_render():
    # vcf_render_spec only accepts a lab inventory (an SddcSpec document
    # is the wrong kind entirely, a separate case -- see
    # test_api.py::test_render_document_refuses_a_wrong_kind_document...),
    # so this must use a real inventory to isolate the version as the only
    # problem, the same way the validate case above isolates it.
    out = call_handler("vcf_render_spec", {"document": TEXT, "vcf_version": "9.9.9.9"})
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-MCP-BAD-ARGS" in codes
    assert "INTERNAL" not in codes
    bad_args = next(f for f in out["findings"] if f["code"] == "VCF-MCP-BAD-ARGS")
    # Inverted deliberately -- see the validate-side twin above.
    assert "9.9.9.9" not in bad_args["message"]
    assert "9.1.1.0" in bad_args["message"]


# --- Finding 10: reclassification translates one finding, does not discard
# every other finding the layers below already computed. Before this fix,
# `vcf_render_spec` on the bundled example inventory with an unknown
# `vcf_version` returned findings that were exactly `['VCF-MCP-BAD-ARGS']`
# -- the schema and rules layers had both already run and found nothing
# wrong (a clean render), but VCF-LIC-EVALUATION (the always-present
# licensing-mode finding every render/validate call reports) was thrown
# away along with everything else, replaced wholesale by the one
# reclassified finding.

def test_reclassify_keeps_every_other_finding():
    out = call_handler("vcf_render_spec", {"document": TEXT, "vcf_version": "0.0.0"})
    codes = [f["code"] for f in out["findings"]]
    assert codes != ["VCF-MCP-BAD-ARGS"]
    assert "VCF-LIC-EVALUATION" in codes
    assert "VCF-MCP-BAD-ARGS" in codes
    assert out["valid"] is False


def test_unvendored_vcf_version_does_not_reject_an_unrelated_inventory_call():
    # vcf_version only matters when the document turns out to be an
    # SddcSpec (see api.py's SDDC_SPEC branch); an inventory-kind
    # validate call never consults it at all, so a bogus value alongside
    # one must not be rejected just for being present and unused.
    out = call_handler("vcf_validate_spec", {"document": TEXT, "vcf_version": "9.9.9.9"})
    assert out["valid"] is True
    assert "VCF-MCP-BAD-ARGS" not in [f["code"] for f in out["findings"]]


# --- Fix round 1, CRITICAL: every credential key is masked, by structural
# position under /credentials -- not by matching its name against a list.
# The inventory schema declares no closed set of credential key names
# (`credentials` is `{"type": "object", "minProperties": 1}`), so a
# name-list approach can never be complete; it previously missed
# 'vcenterRoot' and 'sddcManagerRoot' specifically because CREDENTIAL_KEY_RE
# has no entry for either. Parametrized over the real names render.py
# reads (vcenterRoot, sddcManagerRoot, ssoAdmin, nsxAdmin, esxRoot) *and* an
# invented name that matches no regex at all, to prove the property is
# structural rather than a longer list with the same failure mode.

@pytest.mark.parametrize("cred_name", [
    "esxRoot", "vcenterRoot", "ssoAdmin", "nsxAdmin", "sddcManagerRoot",
    "customBackupOperator",       # not in CREDENTIAL_KEY_RE at all
])
def test_diff_masks_every_credential_regardless_of_key_name(cred_name):
    left = yaml.safe_load(TEXT)
    right = yaml.safe_load(TEXT)
    left["credentials"][cred_name] = f"OldSecretFor-{cred_name}"
    right["credentials"][cred_name] = f"NewSecretFor-{cred_name}"
    out = tool_diff_spec({"left": yaml.safe_dump(left),
                          "right": yaml.safe_dump(right)})
    blob = json.dumps(out)
    assert f"OldSecretFor-{cred_name}" not in blob
    assert f"NewSecretFor-{cred_name}" not in blob
    assert any(entry["path"] == f"/credentials/{cred_name}"
              for entry in out["changes"])


def test_diff_masks_a_wholesale_added_credentials_block():
    # The reviewer's own repro shape: one side has no 'credentials' key at
    # all (an invalid document, but load_document/tool_diff_spec never
    # validates -- it only diffs), so the whole dict arrives as a single
    # leaf value rather than being recursed into key by key.
    doc = yaml.safe_load(TEXT)
    doc["credentials"] = {
        "esxRoot": "PlainEsxSecret111",
        "vcenterRoot": "PlainVcenterSecret222",
        "sddcManagerRoot": "PlainSddcSecret333",
    }
    left = {k: v for k, v in doc.items() if k != "credentials"}
    right = doc
    out = tool_diff_spec({"left": yaml.safe_dump(left),
                          "right": yaml.safe_dump(right)})
    blob = json.dumps(out)
    assert "PlainEsxSecret111" not in blob
    assert "PlainVcenterSecret222" not in blob
    assert "PlainSddcSecret333" not in blob
    assert any(entry["path"] == "/credentials" for entry in out["changes"])


# --- Fix round 2: a 'credentials' segment must mask at ANY depth, because
# vcf_diff_spec never validates its input -- a schema-conformant document
# can only have a free-form object at the root /credentials (every other
# object in v1.schema.json sets additionalProperties: false), but this
# tool's actual inputs are not guaranteed to be schema-conformant.

def test_diff_masks_a_nested_credentials_segment_at_any_depth():
    left = yaml.safe_load(TEXT)
    right = yaml.safe_load(TEXT)
    # Not schema-valid (hosts entries have no 'credentials' property), but
    # tool_diff_spec never validates -- it diffs whatever it is handed,
    # e.g. a mid-edit draft or a document a previous step mangled.
    left["hosts"][0]["credentials"] = {"customBackupOperator": "OldNestedSecret"}
    right["hosts"][0]["credentials"] = {"customBackupOperator": "NewNestedSecret"}
    out = tool_diff_spec({"left": yaml.safe_dump(left),
                          "right": yaml.safe_dump(right)})
    blob = json.dumps(out)
    assert "OldNestedSecret" not in blob
    assert "NewNestedSecret" not in blob
    assert any(entry["path"].endswith("/credentials/customBackupOperator")
              for entry in out["changes"])


def test_diff_masks_a_credentials_segment_case_insensitively():
    left = yaml.safe_load(TEXT)
    right = yaml.safe_load(TEXT)
    left["hosts"][0]["Credentials"] = {"weird": "OldCasedSecret"}
    right["hosts"][0]["Credentials"] = {"weird": "NewCasedSecret"}
    out = tool_diff_spec({"left": yaml.safe_dump(left),
                          "right": yaml.safe_dump(right)})
    blob = json.dumps(out)
    assert "OldCasedSecret" not in blob
    assert "NewCasedSecret" not in blob


def test_diff_does_not_mask_a_merely_similar_key_name():
    # 'credentialsBackup' shares a prefix with 'credentials' but is a
    # different segment entirely -- whole-segment comparison must not
    # sweep it in.
    left = yaml.safe_load(TEXT)
    right = yaml.safe_load(TEXT)
    left["credentialsBackup"] = "NotASecretValueLeft"
    right["credentialsBackup"] = "NotASecretValueRight"
    out = tool_diff_spec({"left": yaml.safe_dump(left),
                          "right": yaml.safe_dump(right)})
    entry = next(e for e in out["changes"] if e["path"] == "/credentialsBackup")
    assert entry["left"] == "NotASecretValueLeft"
    assert entry["right"] == "NotASecretValueRight"


# --- Finding 7: an unparseable document must mean the same thing on every
# tool. vcf_validate_spec/vcf_render_spec route a bad document through
# api.py's own VCF-INPUT-UNREADABLE finding (retryable: fix the argument
# and call again). vcf_diff_spec used to call load_document() bare and let
# every DocumentTooLarge/DocumentTooDeep/DocumentTooManyAliases/
# DocumentTooManyNodes/YAMLError fall through to call_handler's catch-all,
# which reports INTERNAL -- "the tool is broken, do not retry", the
# opposite advice for the identical condition.

def test_diff_unreadable_document_is_bad_input_not_internal():
    out = call_handler("vcf_diff_spec", {"left": "][", "right": "]["})
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"
    assert "INTERNAL" not in [f["code"] for f in out["findings"]]
    assert out["valid"] is False


def test_diff_unreadable_document_names_which_side_failed():
    # A good left and a bad right must still report VCF-INPUT-UNREADABLE
    # (not INTERNAL), and the path should point at the side that actually
    # failed to parse.
    out = call_handler("vcf_diff_spec", {"left": TEXT, "right": "]["})
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"
    assert out["findings"][0]["path"] == "/right"


def test_diff_unreadable_document_is_reported_the_same_way_as_validate():
    # Mutation-provable: the exact code an agent sees for the same bad
    # input must agree across tools, or an agent's retry policy for one
    # tool gives the wrong advice for the other.
    bad = "]["
    validate_code = call_handler("vcf_validate_spec", {"document": bad})["findings"][0]["code"]
    diff_code = call_handler("vcf_diff_spec", {"left": bad, "right": bad})["findings"][0]["code"]
    assert validate_code == diff_code == "VCF-INPUT-UNREADABLE"


# --- Finding 8: envelope uniformity. `valid` is present on every path for
# the three tools with a real validity concept (vcf_validate_spec,
# vcf_render_spec, vcf_diff_spec), and absent on every path -- success and
# failure alike -- for the two that validate nothing (vcf_spec_schema,
# vcf_explain_finding), so a consumer never sees `valid` appear only when
# something went wrong. layers_run/layers_skipped are present on every
# path, success or failure, for the two tools with an actual layered
# pipeline, so reading result["layers_run"] can never KeyError on exactly
# the call where an operator most needs it.

@pytest.mark.parametrize("tool,args", [
    ("vcf_validate_spec", {"document": TEXT}),
    ("vcf_render_spec", {"document": TEXT}),
    ("vcf_diff_spec", {"left": TEXT, "right": TEXT}),
])
def test_valid_is_present_on_success_for_tools_with_a_validity_concept(tool, args):
    out = call_handler(tool, args)
    assert "valid" in out
    assert isinstance(out["valid"], bool)


@pytest.mark.parametrize("tool,bad_args", [
    ("vcf_validate_spec", {}),                       # missing required 'document'
    ("vcf_render_spec", {}),                         # missing required 'document'
    ("vcf_diff_spec", {"left": TEXT}),               # missing required 'right'
])
def test_valid_is_present_on_failure_for_tools_with_a_validity_concept(tool, bad_args):
    out = call_handler(tool, bad_args)
    assert "valid" in out and out["valid"] is False


@pytest.mark.parametrize("tool,args", [
    ("vcf_spec_schema", {}),
    ("vcf_explain_finding", {"code": "INTERNAL"}),
])
def test_valid_is_absent_on_success_for_tools_with_no_validity_concept(tool, args):
    assert "valid" not in call_handler(tool, args)


@pytest.mark.parametrize("tool,bad_args", [
    ("vcf_spec_schema", {"bogus": 1}),               # additionalProperties: False
    ("vcf_explain_finding", {}),                     # missing required 'code'
])
def test_valid_is_absent_on_failure_too_for_tools_with_no_validity_concept(tool, bad_args):
    # This is the exact asymmetry finding 8 named: before the fix, both
    # of these returned {"valid": False, "findings": [...]} on a bad call
    # even though their own success path never carries "valid" at all --
    # a key present only on failure is worse than one consistently absent.
    out = call_handler(tool, bad_args)
    assert "valid" not in out
    assert out["findings"][0]["code"] == "VCF-MCP-BAD-ARGS"


@pytest.mark.parametrize("tool,bad_args", [
    ("vcf_validate_spec", {}),
    ("vcf_render_spec", {}),
])
def test_layers_run_never_keyerrors_even_on_a_bad_args_failure(tool, bad_args):
    out = call_handler(tool, bad_args)
    assert out["layers_run"] == []
    assert out["layers_skipped"] == {}


def test_layers_run_never_keyerrors_on_an_internal_failure(monkeypatch):
    def boom(_args):
        raise RuntimeError("boom")

    monkeypatch.setitem(HANDLERS, "vcf_validate_spec", boom)
    out = call_handler("vcf_validate_spec", {"document": TEXT})
    assert out["findings"][0]["code"] == "INTERNAL"
    assert out["layers_run"] == []
    assert out["layers_skipped"] == {}


def test_diff_and_schema_tools_never_carry_layers_run():
    # vcf_diff_spec and vcf_spec_schema have no layered pipeline at all --
    # layers_run is not merely empty for them, it is absent, the same
    # judgement call that keeps `valid` off vcf_spec_schema/
    # vcf_explain_finding: a key that cannot mean anything for a tool
    # should not be forced onto it just for uniformity's sake.
    assert "layers_run" not in call_handler("vcf_diff_spec", {"left": TEXT, "right": TEXT})
    assert "layers_run" not in call_handler("vcf_spec_schema", {})


def test_server_advertises_this_packages_version_not_the_sdks():
    """Server(name) with no version makes the SDK advertise its OWN version
    as ours, so a client sees a vcfspec release that does not exist. Only a
    real handshake shows it -- every handler test here passes either way --
    so the wiring is pinned rather than trusted.
    """
    pytest.importorskip("mcp")
    from importlib import metadata

    from vcfspec.mcp_server import build_server

    server = build_server()
    assert server.version == metadata.version("vcfspec")
    assert server.version != metadata.version("mcp")
