import json

import yaml

from vcfspec import api
from vcfspec.api import render_document, subtree_blocked, validate_document
from vcfspec.findings import Finding, Result, Severity
from vcfspec.inventory import EXAMPLE_PATH
from vcfspec.schema import DEFAULT_VERSION
from vcfspec.validate import probes
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
    """A literal (non-${reference}) credential never actually demonstrates
    boundary redaction: it's caught upstream by VCF-CRED-NOT-A-REFERENCE /
    InsecureCredentialError, both of which already withhold the value in
    their own message, so that scenario passes even with redact() deleted.
    inventory.py's validate_inventory() does NOT redact internally (unlike
    schema_layer.py), so a jsonschema pattern-mismatch on an ordinary field
    genuinely echoes the raw value -- this is the case that actually
    exercises _envelope's redact() call.
    """
    doc = yaml.safe_load(TEXT)
    doc["instance"]["sddcId"] = "hunter2pass!"     # fails the sddcId pattern
    out = validate_document(yaml.safe_dump(doc))
    assert "hunter2pass!" not in str(out)


def test_render_output_is_redacted_even_if_a_secret_slips_in():
    """Same gap as above, exercised through render_document(): the naming
    rule (rules/platform.py) deliberately does not redact by design, so a
    non-lowercase FQDN containing a secret-shaped string genuinely reaches
    a finding message verbatim before _envelope's redact() call runs.
    """
    doc = yaml.safe_load(TEXT)
    doc["appliances"]["vsp"]["platformFqdn"] = "Secret:hunter2pass"
    out = render_document(yaml.safe_dump(doc))
    assert "Secret:hunter2pass" not in str(out)


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


# render() itself is not defensive the way rules/network.py and
# rules/platform.py are: it does `int(entry["vlan"])` and `entry["vlan"]`
# with no guard, so ordinary schema-invalid input reaches it (schema
# findings do not stop render_document from calling render()) and can
# raise ValueError, KeyError, or anything else. All of it must become a
# finding, not a traceback -- the exception surface is not enumerable in
# advance, so the catch at that boundary must be broad, not a type list.

def test_render_document_converts_a_type_error_from_render_to_a_finding():
    doc = yaml.safe_load(TEXT)
    doc["networks"]["vsan"]["vlan"] = "not-an-int"
    out = render_document(yaml.safe_dump(doc))
    assert out["valid"] is False
    assert "spec" not in out
    assert out["layers_skipped"]["render"] == "render() raised an unexpected exception"


def test_render_document_converts_a_key_error_from_render_to_a_finding():
    doc = yaml.safe_load(TEXT)
    del doc["networks"]["vsan"]["vlan"]
    out = render_document(yaml.safe_dump(doc))
    assert out["valid"] is False
    assert "spec" not in out
    assert out["layers_skipped"]["render"] == "render() raised an unexpected exception"


def test_render_document_converts_any_unexpected_exception_to_a_finding(monkeypatch):
    """No enumerable list of exception types can be complete -- prove the
    boundary catches an exception type it has never seen before, not just
    the two reproducers found above.
    """
    import vcfspec.api as api

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom: totally unrelated to anything render() actually raises")

    monkeypatch.setattr(api, "render", boom)
    out = api.render_document(TEXT)
    assert out["valid"] is False
    assert "spec" not in out
    assert "VCF-RENDER-FAILED" in [f["code"] for f in out["findings"]]


def test_render_document_never_echoes_exception_text_redact_would_not_catch(monkeypatch):
    """redact() is a pattern masker (jsonschema-echo shapes, key=/secret=
    shapes), not a sanitizer: it cannot promise anything about text it has
    never seen. An exception raised by arbitrary code operating on
    operator data could contain anything, including a value redact()'s
    patterns do not match at all. Prove the boundary never puts str(exc)
    in output in the first place, rather than relying on redact() to
    catch it: use a literal that is deliberately shaped so that NONE of
    redact()'s patterns fire on it (no quotes, no 'password='-style
    prefix, not under a credential-shaped key).
    """
    import vcfspec.api as api

    unrecognised_secret = "TotallyUnrecognizedSecretPattern9000"
    assert unrecognised_secret in str(unrecognised_secret)  # sanity: redact() is a no-op on it
    from vcfspec.redact import redact as real_redact
    assert real_redact(unrecognised_secret) == unrecognised_secret  # confirms redact() misses it

    def boom(*_args, **_kwargs):
        raise ValueError(f"bad value: {unrecognised_secret}")

    monkeypatch.setattr(api, "render", boom)
    out = api.render_document(TEXT)
    assert out["valid"] is False
    assert unrecognised_secret not in str(out)


def test_render_document_does_not_gate_rules_so_a_suppressed_warning_never_hides_a_bad_spec():
    """A schema type-error at /appliances/vsp/poolStart must not suppress
    the VSP pool-crosses-subnet rule finding derived from that same bad
    value: render_document has no subtree gating (unlike validate_document)
    precisely because render() does not respect gating on its output
    either -- filtering only the finding would leave an operator with a
    rendered spec that still carries the bad value and fewer findings
    explaining why.
    """
    doc = yaml.safe_load(TEXT)
    doc["appliances"]["vsp"]["poolStart"] = 12345
    out = render_document(yaml.safe_dump(doc))
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-VSP-POOL-CROSSES-SUBNET" in codes
    # No layer was skipped: every one of detect/schema/rules/render/verify
    # ran, and no rule finding was filtered. (The bad value also fails the
    # rendered spec against the vendored schema, so the spec itself is
    # withheld -- that is the verify layer doing its job, not gating.)
    assert "rules" not in out["layers_skipped"]
    assert out["layers_run"] == ["detect", "schema", "rules", "render", "verify"]


# --- Subtree gating: pointer segments, not string prefixes -----------------

def test_subtree_blocked_collects_schema_finding_pointers():
    findings = (
        Finding(code="VCF-INV-SCHEMA", severity=Severity.ERROR,
                path="/networks/vsan", message="x", source="schema"),
        Finding(code="VCF-NSX-FABRIC-MTU-TOO-LOW", severity=Severity.ERROR,
                path="/nsx/fabricMtu", message="x", source="docs"),
    )
    assert subtree_blocked(findings) == {"/networks/vsan"}


def test_subtree_blocked_excludes_both_root_spellings():
    """"/" and "" both denote a root-level error. Either one left in the
    blocked set is an empty-segment prefix of every pointer, which would
    silently suppress every rule finding in the document -- so both must
    be excluded, not just "/".
    """
    findings = (
        Finding(code="VCF-INV-SCHEMA", severity=Severity.ERROR, path="/",
                message="x", source="schema"),
        Finding(code="VCF-INV-SCHEMA", severity=Severity.ERROR, path="",
                message="x", source="schema"),
    )
    assert subtree_blocked(findings) == set()


def test_empty_string_pointer_does_not_block_everything():
    from vcfspec.api import _is_blocked
    assert _is_blocked("/anything/at/all", {""}) is False


def test_pointer_segments_are_rfc6901_unescaped_before_comparison():
    """Per RFC 6901, '~1' encodes a literal '/' and '~0' encodes a literal
    '~' within one pointer segment; '~1' must be decoded before '~0' or a
    literal "~01" mis-decodes. A blocked pointer recorded with an escaped
    key must still gate the real (unescaped) key it represents, and vice
    versa -- comparing the raw, still-escaped strings would treat
    "vsan/legacy" (escaped as "vsan~1legacy") as a different segment than
    the same key spelled with a real '/' delimiter.
    """
    from vcfspec.api import _is_blocked, _segments
    assert _segments("/networks/vsan~1legacy") == ("networks", "vsan/legacy")
    assert _segments("/a~01b") == ("a~1b",)          # ~01 -> ~1, not '/'1

    blocked = {"/networks/vsan~1legacy"}
    assert _is_blocked("/networks/vsan~1legacy/gateway", blocked) is True
    assert _is_blocked("/networks/vsan", blocked) is False


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


# --- Fix round 3: unknown vcf_version never raises, and render() rejects a
# document of the wrong kind explicitly instead of reporting a silent,
# uninspected "valid: true" -----------------------------------------------

def test_validate_document_never_raises_on_an_unvendored_version():
    # Before this fix, validate_against_schema(doc, version) raised
    # FileNotFoundError straight out of validate_document() for an
    # SDDC_SPEC-kind document -- a real violation of this module's own
    # documented promise that nothing here ever lets an exception reach
    # the caller (see the module docstring). Only the CLI's and MCP's own
    # outer safety nets kept that from crashing a real caller.
    spec_text = json.dumps(render_document(TEXT)["spec"])
    out = validate_document(spec_text, input_kind="sddc_spec", version="9.9.9.9")
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-SCHEMA-VERSION-UNKNOWN"


def test_render_document_never_raises_on_an_unvendored_version():
    out = render_document(TEXT, version="9.9.9.9")
    assert out["valid"] is False
    assert "spec" not in out
    assert out["findings"][-1]["code"] == "VCF-SCHEMA-VERSION-UNKNOWN"
    # Not VCF-RENDER-FAILED: that code means render() itself broke: an
    # unvendored version is a bad argument, not a tool bug, and the two
    # must stay distinguishable for the same reason VCF-MCP-BAD-ARGS is
    # kept separate from INTERNAL at the MCP boundary.
    assert "VCF-RENDER-FAILED" not in [f["code"] for f in out["findings"]]


def test_render_document_refuses_a_wrong_kind_document_instead_of_reporting_valid():
    # A document render_document correctly detects as SDDC_SPEC (not
    # UNKNOWN -- sniffing it right is not an error) used to reach here
    # with an *empty* Result: valid=True, zero findings, layers_run only
    # ever ["detect"]. Nothing about the document was actually checked --
    # render() never ran, schema/rules never ran -- yet the answer was
    # "yes, safe", exactly the "confidently wrong" failure mode this round
    # of fixes exists to close.
    spec_text = json.dumps(render_document(TEXT)["spec"])
    out = render_document(spec_text)
    assert out["valid"] is False
    assert out["layers_run"] == ["detect"]
    assert out["findings"][0]["code"] == "VCF-RENDER-WRONG-KIND"
    assert "spec" not in out


# --- Finding 11: an irrelevant vcf_version must not be silently ignored --
# validate_document only ever consults `version` for an SDDC_SPEC-kind
# document (see the branch above); for an inventory, it never reads it.
# Before this, `vcf_validate_spec` on the bundled inventory with
# `vcf_version: "0.0.0"` returned `valid: True` with nothing telling the
# caller that "0.0.0" was never actually checked against anything -- a
# confidently-wrong-shaped result, the same failure family as finding 1.
# The fix must not reject the call (fix A already established that an
# inventory-kind call carrying an irrelevant vcf_version is legitimate),
# only stop it from being silently misleading about what ran.

def test_irrelevant_version_on_an_inventory_gets_an_informational_note():
    out = validate_document(TEXT, version="0.0.0")
    assert out["valid"] is True          # not rejected -- fix A's promise holds
    assert "VCF-VERSION-NOT-CONSULTED" in [f["code"] for f in out["findings"]]
    note = next(f for f in out["findings"] if f["code"] == "VCF-VERSION-NOT-CONSULTED")
    assert note["severity"] == "info"    # advisory only, never blocks
    # This assertion used to be `"0.0.0" in note["message"]`. Inverted on
    # 2026-09-19: the note must say that a version was supplied and
    # ignored, and name the vendored versions, WITHOUT echoing the
    # caller's own string -- the same rule the rejection path follows.
    # See tests/test_security_version.py::test_no_finding_message_ever_
    # reflects_caller_supplied_text.
    assert "0.0.0" not in note.get("message", "")
    assert DEFAULT_VERSION in note.get("message", "")
    assert "not consulted" in note.get("message", "")


def test_default_version_on_an_inventory_gets_no_note():
    # The common case -- no vcf_version supplied, or the one already in
    # effect -- must stay exactly as quiet as before this fix: nobody who
    # never mentioned a version needs to be told it was not consulted.
    out = validate_document(TEXT)
    assert "VCF-VERSION-NOT-CONSULTED" not in [f["code"] for f in out["findings"]]
    out = validate_document(TEXT, version=DEFAULT_VERSION)
    assert "VCF-VERSION-NOT-CONSULTED" not in [f["code"] for f in out["findings"]]


def test_relevant_version_on_an_sddc_spec_gets_no_note():
    # The note is specific to the inventory branch, where version really
    # is unused -- an SDDC_SPEC document does consult it, so no note.
    spec_text = json.dumps(render_document(TEXT)["spec"])
    out = validate_document(spec_text, input_kind="sddc_spec", version="0.0.0")
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-VERSION-NOT-CONSULTED" not in codes
    assert "VCF-SCHEMA-VERSION-UNKNOWN" in codes


# --- Finding 12: render_document could not override kind on either
# surface -- only validate_document accepted input_kind. Mirrors the same
# override validate_document already had.

def test_render_document_accepts_an_input_kind_override():
    out = render_document(TEXT, input_kind="inventory")
    assert out["valid"] is True
    assert out["spec"]["sddcId"] == "lab01"


def test_render_document_forcing_sddc_spec_kind_reports_wrong_kind():
    # Forcing "sddc_spec" on a real inventory must not silently render it
    # as one -- render() only ever accepts a LabInventory, so the override
    # must still hit the same explicit VCF-RENDER-WRONG-KIND path a
    # correctly auto-detected SddcSpec document hits above, rather than
    # being misread as the inventory it actually is.
    out = render_document(TEXT, input_kind="sddc_spec")
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-RENDER-WRONG-KIND"
    assert "spec" not in out


def test_render_document_bad_input_kind_override_is_a_finding_not_a_crash():
    out = render_document(TEXT, input_kind="not-a-real-kind")
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-BAD-KIND"


# --- Fix round 3: the general invariant -- valid=True requires at least
# one substantive layer to have actually run. Tested directly against the
# helper, independent of any one caller's skip logic, per the coordinator's
# instruction not to test it only through the input_kind case that
# motivated it.

def test_no_layers_ran_invariant_forces_invalid_and_attaches_a_finding():
    from vcfspec.api import _refuse_unvalidated_success

    synthetic_valid_but_empty = Result(())
    out = _refuse_unvalidated_success(synthetic_valid_but_empty, ["detect"])
    assert out.valid is False
    assert out.codes == ("VCF-NO-VALIDATION-RAN",)


def test_no_layers_ran_invariant_is_a_no_op_once_a_substantive_layer_ran():
    from vcfspec.api import _refuse_unvalidated_success

    synthetic_valid = Result(())
    out = _refuse_unvalidated_success(synthetic_valid, ["detect", "schema", "rules"])
    assert out.valid is True
    assert out.findings == ()


def test_no_layers_ran_invariant_does_not_touch_an_already_invalid_result():
    from vcfspec.api import _refuse_unvalidated_success

    already_invalid = Result((Finding(code="VCF-INPUT-UNRECOGNISED",
                                      severity=Severity.CRITICAL, path="/",
                                      message="x", source="schema"),))
    out = _refuse_unvalidated_success(already_invalid, ["detect"])
    assert out.codes == ("VCF-INPUT-UNRECOGNISED",)   # unchanged, not doubled up


def test_the_invariant_is_actually_wired_into_the_envelope():
    """The three tests above call the helper directly, so all of them pass
    even if _envelope() stops calling it -- the guard would still be
    correct, and no longer protect anything. That is the defect this
    branch's final review found in its most serious form (a schema walk
    that worked perfectly and had no production call site), so the call
    site gets pinned here rather than trusted.
    """
    from vcfspec.api import _envelope

    out = _envelope(Result(()), ["detect"], {})
    assert out["valid"] is False
    assert [f["code"] for f in out["findings"]] == ["VCF-NO-VALIDATION-RAN"]


# --- Finding 6: probes must never be listed as run over a shape they
# cannot read ---------------------------------------------------------------

def test_probes_are_skipped_with_a_reason_for_an_sddc_spec_document(monkeypatch):
    """run_probes is written against the inventory shape (dns.subdomain,
    hosts[].name, hosts[].mgmtIp). An SddcSpec spells those dnsSpec and
    hostSpecs[].hostname and has no per-host management IP at all, so
    inventory.get("hosts") returned None, the loop body never ran, and the
    layer reported success by saying nothing -- while layers_run listed
    "probes" and layers_skipped said nothing about it. Both halves of the
    envelope's honesty mechanism agreed a layer had run that had inspected
    nothing. Either it works on that shape or it records a skip; silent
    inclusion is the one unacceptable option.
    """
    attempted = []
    monkeypatch.setattr(probes, "_default_resolver",
                        lambda *a, **k: attempted.append(("resolve", a)))
    monkeypatch.setattr(probes, "_default_connector",
                        lambda *a, **k: attempted.append(("connect", a)))

    spec_text = json.dumps(render_document(TEXT)["spec"])
    out = validate_document(spec_text, input_kind="sddc_spec",
                            probe_config=ProbeConfig(allowlist=("10.50.10.0/24",),
                                                     domain_allowlist=("lab.local",)))
    assert "probes" not in out["layers_run"]
    assert "probes" in out["layers_skipped"]
    assert "inventory" in out["layers_skipped"]["probes"]
    assert attempted == []


def test_probes_still_run_for_an_inventory(monkeypatch):
    """The skip above must be about the document kind, not a blanket
    disabling of the layer."""
    out = validate_document(TEXT, probe_config=ProbeConfig())
    assert "probes" in out["layers_run"]
    assert "probes" not in out["layers_skipped"]


# --- Finding 1: the rendered spec is validated, and a bad one is withheld ---

def _render_with(**overrides):
    doc = yaml.safe_load(TEXT)
    for dotted, value in overrides.items():
        node = doc
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return render_document(yaml.safe_dump(doc))


def test_the_happy_path_still_renders_and_records_the_verify_layer():
    out = render_document(TEXT)
    assert out["valid"] is True
    assert out["layers_run"] == ["detect", "schema", "rules", "render", "verify"]
    assert out["spec"]["sddcId"] == "lab01"


def test_a_garbage_gateway_renders_into_a_schema_invalid_spec_and_is_caught():
    """The review's reproduction. networks.management.gateway: "nope" passes
    the inventory schema (which types gateway as a bare string), skips the
    rule layer (_address() returns None for an unparseable address), and is
    copied straight into networkSpecs[0].gateway -- which the vendored
    VMware schema rejects. This used to exit 0 with valid: true and a
    "spec" key holding it.
    """
    out = _render_with(**{"networks.management.gateway": "nope"})
    assert out["valid"] is False
    assert "verify" in out["layers_run"]
    schema_paths = [f["path"] for f in out["findings"] if f["code"] == "VCF-SCHEMA"]
    assert "/networkSpecs/0/gateway" in schema_paths


def test_a_schema_invalid_render_does_not_return_a_spec():
    """The entire purpose is preventing a bad spec reaching VCF, so a spec
    that fails validation is not handed back with a warning attached -- an
    operator or an agent pipes `.spec` into a file and never reads the
    findings. Same shape as the insecure-credential path."""
    out = _render_with(**{"networks.management.gateway": "nope"})
    assert "spec" not in out
    assert out["layers_skipped"]["spec"]


def test_a_rule_finding_about_the_input_still_returns_a_spec():
    """The line the withholding draws: the verify layer withholds, the rule
    layer does not. A rule finding describes the *inventory*, and an
    operator fixes it by iterating on the render, so the render is still
    handed over."""
    out = _render_with(**{"nsx.fabricMtu": 1500})
    assert out["valid"] is False
    assert "VCF-NSX-FABRIC-MTU-TOO-LOW" in {f["code"] for f in out["findings"]}
    assert "spec" in out


def test_the_vsp_pool_case_from_the_same_sweep_is_caught_too():
    out = _render_with(**{"appliances.vsp.poolStart": "zzz"})
    assert out["valid"] is False
    assert "spec" not in out


def test_walk_declared_properties_is_wired_in_not_just_tested(monkeypatch):
    """walk_declared_properties existed, worked, was well tested, and had
    zero production call sites -- it ran against one bundled fixture and
    nothing else. No $def in the vendored schema sets
    additionalProperties: false, so an invented key validates cleanly at
    every level and jsonschema cannot catch it; this walk is the only thing
    that can. Inject one at render time and confirm render_document
    reports it, which is a claim about the *wiring*, not the walk.
    """
    real_render = api.render

    def render_with_an_invented_key(inventory, version=DEFAULT_VERSION):
        spec, result = real_render(inventory, version)
        spec = {**spec, "hostSpecs": [{**spec["hostSpecs"][0],
                                       "bogusInventedField": "x"}]}
        return spec, result

    monkeypatch.setattr(api, "render", render_with_an_invented_key)
    out = api.render_document(TEXT)
    assert out["valid"] is False
    assert "spec" not in out
    undeclared = [f for f in out["findings"]
                  if f["code"] == "VCF-RENDER-UNDECLARED-FIELD"]
    assert [f["path"] for f in undeclared] == ["/hostSpecs/0/bogusInventedField"]


def test_verify_never_raises_for_a_version_with_defaults_but_no_schema(monkeypatch):
    """api.py promises no exception reaches the caller. The verify layer
    loads the vendored schema, which render() does not, so it has its own
    way to fail."""
    def missing(version=DEFAULT_VERSION):
        raise FileNotFoundError(version)

    monkeypatch.setattr(api, "load_schema", missing)
    out = api.render_document(TEXT)
    assert out["valid"] is False
    assert "spec" not in out
    assert "VCF-SCHEMA-VERSION-UNKNOWN" in {f["code"] for f in out["findings"]}
