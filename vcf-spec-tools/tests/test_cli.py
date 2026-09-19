import json

import pytest
import yaml

import vcfspec.cli as cli
from vcfspec.cli import main
from vcfspec.inventory import EXAMPLE_PATH

TEXT = EXAMPLE_PATH.read_text(encoding="utf-8")


def test_validate_exits_zero_for_the_example(capsys):
    assert main(["validate", str(EXAMPLE_PATH)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_validate_exits_one_and_names_the_rule(tmp_path, capsys):
    doc = yaml.safe_load(TEXT)
    doc["hosts"][1]["mgmtIp"] = doc["hosts"][0]["mgmtIp"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert main(["validate", str(path)]) == 1
    codes = [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
    assert "VCF-NET-DUPLICATE-IP" in codes


def test_render_writes_a_spec_to_stdout(capsys):
    assert main(["render", str(EXAMPLE_PATH)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["spec"]["hostSpecs"][0]["hostname"] == "esx01"


def test_missing_file_is_reported_not_raised(capsys):
    assert main(["validate", "does-not-exist.yaml"]) == 2
    assert "does-not-exist.yaml" in capsys.readouterr().err


def test_end_to_end_invalid_mtu_flows_from_yaml_to_finding(tmp_path, capsys):
    doc = yaml.safe_load(TEXT)
    doc["nsx"]["fabricMtu"] = 1500
    path = tmp_path / "mtu.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert main(["validate", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    finding = next(f for f in payload["findings"]
                   if f["code"] == "VCF-NSX-FABRIC-MTU-TOO-LOW")
    assert finding["path"] == "/nsx/fabricMtu"
    assert finding["source_url"].startswith("https://techdocs.broadcom.com/")


# --- Directive 2: the CLI must never be a way to bypass probe containment ---

def test_probe_without_allowlist_is_a_usage_error(capsys):
    """--probe with no --allowlist must fail loudly (exit 2), not run zero
    probes and exit as if it were a clean pass."""
    exit_code = main(["validate", str(EXAMPLE_PATH), "--probe"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--probe" in err and "allowlist" in err.lower()


def test_allowlist_alone_does_not_enable_probes(capsys):
    """Supplying --allowlist without --probe must not silently turn probes
    on -- probes only run when the operator explicitly asks with --probe."""
    exit_code = main(["validate", str(EXAMPLE_PATH), "--allowlist", "10.0.0.0/8"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "probes" not in payload["layers_run"]


def test_probe_with_allowlist_runs_the_probe_layer(monkeypatch, capsys):
    """With both --probe and --allowlist supplied, the probe layer actually
    runs (proving the CLI wires ProbeConfig through rather than dropping it)."""
    captured = {}

    def fake_validate_document(text, input_kind=None, probe_config=None, version=None):
        captured["probe_config"] = probe_config
        return {"valid": True, "findings": [], "layers_run": ["detect", "probes"],
               "layers_skipped": {}}

    monkeypatch.setattr(cli, "validate_document", fake_validate_document)
    exit_code = main(["validate", str(EXAMPLE_PATH), "--probe",
                      "--allowlist", "10.0.0.0/8", "--allowlist", "192.168.1.0/24"])
    assert exit_code == 0
    assert captured["probe_config"].allowlist == ("10.0.0.0/8", "192.168.1.0/24")


def test_probe_with_empty_string_allowlist_is_a_usage_error(capsys):
    """--allowlist "" passes a naive presence check ([""] is truthy) but
    every CIDR in it fails to parse, so it must be refused exactly like a
    missing --allowlist -- not silently accepted as containment that
    happens to block everything."""
    exit_code = main(["validate", str(EXAMPLE_PATH), "--probe", "--allowlist", ""])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "allowlist" in err.lower()


def test_probe_with_partially_invalid_allowlist_is_a_usage_error(capsys):
    """One bad CIDR among several good ones must refuse the whole run
    rather than silently probing only the entries that happened to parse
    -- an operator who typed 2 ranges and got checks against 1 has been
    told less than they asked for without being told anything went wrong."""
    exit_code = main(["validate", str(EXAMPLE_PATH), "--probe",
                      "--allowlist", "10.0.0.0/8", "--allowlist", "not-a-cidr"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not-a-cidr" in err


def test_bogus_flag_returns_two_instead_of_raising(capsys):
    """argparse's own usage-error path must not escape main() as an
    uncaught SystemExit -- main(argv) -> int is a contract for
    programmatic callers, not just the shell."""
    assert main(["--bogus"]) == 2


def test_no_arguments_returns_two_instead_of_raising(capsys):
    assert main([]) == 2


# --- Directive 3: exit codes are the contract; usage error vs invalid spec ---

def test_directory_path_is_a_usage_error(tmp_path, capsys):
    assert main(["validate", str(tmp_path)]) == 2
    assert str(tmp_path) in capsys.readouterr().err


def test_unparseable_yaml_is_invalid_not_a_usage_error(tmp_path, capsys):
    """A file that exists and is readable but fails to parse (or is
    oversized) is NOT a usage error -- api.py turns that into a structured
    finding, so it must exit 1, not 2."""
    path = tmp_path / "broken.yaml"
    path.write_text("hosts: [this is not closed", encoding="utf-8")
    assert main(["validate", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["findings"]


# --- Directive 4: never print a traceback; convert to a structured error ---

def test_unexpected_exception_is_caught_and_reported(monkeypatch, capsys):
    def boom(text, input_kind=None, probe_config=None, version=None):
        raise RuntimeError("simulated orchestrator failure")

    monkeypatch.setattr(cli, "validate_document", boom)
    exit_code = main(["validate", str(EXAMPLE_PATH)])
    assert exit_code == 1
    out, err = capsys.readouterr().out, capsys.readouterr().err
    payload = json.loads(out)
    assert payload["valid"] is False
    assert "Traceback" not in out
    assert "Traceback" not in err


# --- Directive 5: stdout is JSON only ---

@pytest.mark.parametrize("argv, expect_valid", [
    (["validate", str(EXAMPLE_PATH)], True),
    (["render", str(EXAMPLE_PATH)], True),
])
def test_stdout_is_pure_json_on_success(argv, expect_valid, capsys):
    main(argv)
    out = capsys.readouterr().out
    payload = json.loads(out)  # raises if anything human-oriented leaked in
    assert payload["valid"] is expect_valid


def test_stdout_is_pure_json_on_invalid_document(tmp_path, capsys):
    doc = yaml.safe_load(TEXT)
    doc["hosts"][1]["mgmtIp"] = doc["hosts"][0]["mgmtIp"]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    main(["validate", str(path)])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["valid"] is False


def test_allowlist_domain_is_threaded_through_to_probeconfig(monkeypatch, capsys):
    """An option the CLI parses but drops is indistinguishable from one it
    never had; probes.py fails closed without it, so a dropped
    --allowlist-domain would silently mean "resolve nothing"."""
    captured = {}

    def fake_validate_document(text, input_kind=None, probe_config=None, version=None):
        captured["probe_config"] = probe_config
        return {"valid": True, "findings": [], "layers_run": ["detect", "probes"],
               "layers_skipped": {}}

    monkeypatch.setattr(cli, "validate_document", fake_validate_document)
    exit_code = main(["validate", str(EXAMPLE_PATH), "--probe",
                      "--allowlist", "10.50.10.0/24",
                      "--allowlist-domain", "lab.local",
                      "--allowlist-domain", "lab2.local"])
    assert exit_code == 0
    assert captured["probe_config"].domain_allowlist == ("lab.local", "lab2.local")


def test_probe_with_no_allowlist_domain_defaults_to_resolving_nothing(
        monkeypatch, capsys):
    captured = {}

    def fake_validate_document(text, input_kind=None, probe_config=None, version=None):
        captured["probe_config"] = probe_config
        return {"valid": True, "findings": [], "layers_run": ["detect", "probes"],
               "layers_skipped": {}}

    monkeypatch.setattr(cli, "validate_document", fake_validate_document)
    main(["validate", str(EXAMPLE_PATH), "--probe", "--allowlist", "10.50.10.0/24"])
    assert captured["probe_config"].domain_allowlist == ()


def test_probe_with_an_allowlist_matching_nothing_does_not_exit_zero(capsys):
    """The exact reproduction: a valid, non-empty allowlist that matches
    none of the example lab's hosts. The CLI refuses an *empty* allowlist
    for precisely this reason, and this case used to exit 0 with
    valid: true, layers_run including "probes", and zero lookups made.
    """
    exit_code = main(["validate", str(EXAMPLE_PATH), "--probe",
                      "--allowlist", "203.0.113.0/24",
                      "--allowlist-domain", "lab.local"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["valid"] is False
    assert "VCF-PROBE-NOTHING-PERMITTED" in {f["code"] for f in payload["findings"]}


# --- Finding 12: the MCP server exposed input_kind on vcf_validate_spec
# but the CLI had no equivalent, and render_document could not be
# overridden on either surface. --input-kind closes both gaps, restricted
# to the same closed enum documents.py actually accepts.

def test_validate_input_kind_forces_kind_detection(tmp_path, capsys):
    # A rendered SddcSpec, force-validated as an inventory: auto-detection
    # would correctly call this sddc_spec, so seeing inventory-shaped
    # findings (not VCF-SCHEMA) is proof the override, not autodetection,
    # decided the kind.
    assert main(["render", str(EXAMPLE_PATH)]) == 0
    spec = json.loads(capsys.readouterr().out)["spec"]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    exit_code = main(["validate", str(spec_path), "--input-kind", "inventory"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["findings"][0]["code"] == "VCF-INV-SCHEMA"


def test_validate_input_kind_rejects_an_unrecognised_value(capsys):
    exit_code = main(["validate", str(EXAMPLE_PATH), "--input-kind", "bogus"])
    assert exit_code == 2
    assert "bogus" in capsys.readouterr().err.lower()


def test_render_input_kind_forces_wrong_kind_rejection_not_misdetection(capsys):
    # render only ever accepts an inventory; forcing sddc_spec on the real
    # inventory example must produce the explicit VCF-RENDER-WRONG-KIND
    # path, not a silent, uninspected pass.
    exit_code = main(["render", str(EXAMPLE_PATH), "--input-kind", "sddc_spec"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["findings"][0]["code"] == "VCF-RENDER-WRONG-KIND"
    assert "spec" not in payload


def test_render_input_kind_rejects_an_unrecognised_value(capsys):
    exit_code = main(["render", str(EXAMPLE_PATH), "--input-kind", "bogus"])
    assert exit_code == 2
