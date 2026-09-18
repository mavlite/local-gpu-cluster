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
