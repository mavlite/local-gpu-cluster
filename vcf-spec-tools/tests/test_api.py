import yaml

from vcfspec.api import render_document, subtree_blocked, validate_document
from vcfspec.findings import Finding, Severity
from vcfspec.inventory import EXAMPLE_PATH
from vcfspec.validate.probes import ProbeConfig

TEXT = EXAMPLE_PATH.read_text(encoding="utf-8")


def test_validates_the_example_and_reports_layers():
    out = validate_document(TEXT)
    assert out["valid"] is True
    assert "rules" in out["layers_run"]
    assert out["layers_skipped"]["probes"] == "no probe configuration supplied"


def test_unknown_document_stops_at_detection():
    out = validate_document("foo: bar\n")
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNRECOGNISED"
    assert out["layers_skipped"]["schema"] == "document kind unknown"


def test_schema_error_in_one_subtree_still_runs_rules_elsewhere():
    doc = yaml.safe_load(TEXT)
    doc["appliances"]["vsp"]["poolStart"] = 12345      # wrong type: schema error
    doc["nsx"]["fabricMtu"] = 1500                      # rule violation elsewhere
    out = validate_document(yaml.safe_dump(doc))
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-INV-SCHEMA" in codes
    assert "VCF-NSX-FABRIC-MTU-TOO-LOW" in codes


def test_malformed_network_does_not_raise_through_the_orchestrator():
    doc = yaml.safe_load(TEXT)
    doc["networks"]["vsan"] = {"vlan": "not-an-int"}
    out = validate_document(yaml.safe_dump(doc))
    assert out["valid"] is False          # reported, not raised


def test_oversized_document_is_a_finding_not_an_exception():
    out = validate_document("a: " + "x" * 2_000_001)
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_render_returns_spec_and_findings():
    out = render_document(TEXT)
    assert out["spec"]["sddcId"] == "lab01"
    assert out["valid"] is True


def test_render_of_an_invalid_inventory_still_emits_a_spec():
    doc = yaml.safe_load(TEXT)
    doc["nsx"]["fabricMtu"] = 1500
    out = render_document(yaml.safe_dump(doc))
    assert out["spec"]["sddcId"] == "lab01"
    assert out["valid"] is False


def test_output_is_redacted_even_if_a_secret_slips_in():
    leaky = TEXT.replace("${esx_root}", "RealPassword123!")
    assert "RealPassword123!" not in str(validate_document(leaky))


def test_render_output_is_redacted_even_if_a_secret_slips_in():
    leaky = TEXT.replace("${esx_root}", "RealPassword123!")
    assert "RealPassword123!" not in str(render_document(leaky))


def test_validate_is_independent_of_call_order():
    first = validate_document(TEXT)
    validate_document("foo: bar\n")
    assert validate_document(TEXT) == first


def test_no_files_are_written_during_validate_and_render(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    validate_document(TEXT)
    render_document(TEXT)
    assert list(tmp_path.iterdir()) == []


# --- Loader failures become findings, never exceptions --------------------
# One test per exception type documents lists under vcfspec.documents, plus
# InsecureCredentialError from the renderer. A caller must always get a
# Result, never a traceback, regardless of what is wrong with the input.

def test_document_too_large_is_a_finding_not_an_exception():
    out = validate_document("a: " + "x" * 2_000_001)
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"
    assert out["layers_skipped"]["schema"] == "document unreadable"


def test_document_too_deep_is_a_finding_not_an_exception():
    deep = "a:\n" + "".join(f"{' ' * (i + 1)}b{i}:\n" for i in range(60)) + " " * 61 + "c: 1"
    out = validate_document(deep)
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_document_too_deep_via_parser_stack_is_a_finding_not_an_exception():
    bomb = "a: " + "[" * 10000 + "]" * 10000
    out = validate_document(bomb)
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_alias_bomb_is_a_finding_not_an_exception():
    doc = "a: &x [1,2]\n" + "".join(f"b{i}: *x\n" for i in range(150))
    out = validate_document(doc)
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_malformed_yaml_is_a_finding_not_an_exception():
    out = validate_document("!!python/object/apply:os.system ['echo pwned']")
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_non_mapping_root_is_a_finding_not_an_exception():
    out = validate_document("- just\n- a\n- list\n")
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_loader_failures_are_findings_via_render_document_too():
    out = render_document("a: " + "x" * 2_000_001)
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"


def test_render_refuses_an_insecure_credential_without_raising():
    doc = yaml.safe_load(TEXT)
    doc["credentials"]["vcenterRoot"] = "Sup3rSecret!"
    out = render_document(yaml.safe_dump(doc))
    assert out["valid"] is False
    assert "Sup3rSecret!" not in str(out)
    assert "spec" not in out


# --- Subtree gating: pointer segments, not string prefixes -----------------

def test_subtree_blocked_collects_schema_finding_pointers():
    findings = (
        Finding(code="VCF-INV-SCHEMA", severity=Severity.ERROR,
                path="/networks/vsan", message="x", source="schema"),
        Finding(code="VCF-NSX-FABRIC-MTU-TOO-LOW", severity=Severity.ERROR,
                path="/nsx/fabricMtu", message="x", source="docs"),
    )
    assert subtree_blocked(findings) == {"/networks/vsan"}


def test_subtree_gate_blocks_the_pointer_itself_and_its_children():
    from vcfspec.api import _is_blocked
    blocked = {"/networks/vsan"}
    assert _is_blocked("/networks/vsan", blocked) is True
    assert _is_blocked("/networks/vsan/gateway", blocked) is True
    assert _is_blocked("/networks/vsan/pool/start", blocked) is True


def test_subtree_gate_does_not_match_a_sibling_with_a_shared_string_prefix():
    """The bug a naive `pointer.startswith(blocked)` would introduce: pointer
    "/networks/vsanWitness" starts with the raw string "/networks/vsan" even
    though it is a sibling, not a descendant. Segment comparison must not be
    fooled by this.
    """
    from vcfspec.api import _is_blocked
    blocked = {"/networks/vsan"}
    assert _is_blocked("/networks/vsanWitness", blocked) is False
    assert _is_blocked("/networks/management", blocked) is False


def test_validate_document_only_suppresses_rules_under_the_blocked_pointer():
    """Integration-level check: a schema error confined to one host's
    hardware section must not suppress a network rule violation living at
    an unrelated pointer.
    """
    doc = yaml.safe_load(TEXT)
    doc["hosts"][0]["hardware"]["ramGb"] = "not-a-number"   # schema error, /hosts/0/...
    doc["nsx"]["fabricMtu"] = 1500                           # rule elsewhere
    out = validate_document(yaml.safe_dump(doc))
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-INV-SCHEMA" in codes
    assert "VCF-NSX-FABRIC-MTU-TOO-LOW" in codes


# --- Probes: opt-in only, and only with zero critical findings -------------

def test_probes_run_when_configured_and_no_critical_findings():
    """An empty allowlist blocks every target before any DNS/network call is
    made (see validate/probes.py), so this exercises the orchestrator's
    probe-gating without touching the network.
    """
    out = validate_document(TEXT, probe_config=ProbeConfig())
    assert "probes" in out["layers_run"]
    assert "VCF-PROBE-TARGET-BLOCKED" in [f["code"] for f in out["findings"]]


def test_probes_skipped_when_critical_findings_present():
    leaky = TEXT.replace("${esx_root}", "RealPassword123!")
    out = validate_document(leaky, probe_config=ProbeConfig())
    assert "probes" not in out["layers_run"]
    assert out["layers_skipped"]["probes"] == "critical findings present"


def test_probes_skipped_with_no_config_even_though_it_would_otherwise_run():
    out = validate_document(TEXT)
    assert "probes" not in out["layers_run"]
    assert out["layers_skipped"]["probes"] == "no probe configuration supplied"
