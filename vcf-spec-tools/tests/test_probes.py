import pytest
from vcfspec.rules import load_catalogue
from vcfspec.validate.probes import ProbeConfig, run_probes

CONFIG = ProbeConfig(allowlist=("10.50.0.0/16",))


def resolver_for(answers):
    def resolve(name, want_reverse=False):
        return answers.get((name, want_reverse))
    return resolve


def all_good(inventory):
    answers = {}
    for host in inventory["hosts"]:
        fqdn = f"{host['name']}.lab.local"
        answers[(fqdn, False)] = host["mgmtIp"]
        answers[(host["mgmtIp"], True)] = fqdn
    return resolver_for(answers)


def test_clean_environment_produces_no_findings(inventory):
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True)
    assert result.findings == ()


def test_missing_reverse_record_is_reported(inventory):
    answers = {(f"{h['name']}.lab.local", False): h["mgmtIp"]
               for h in inventory["hosts"]}
    result = run_probes(inventory, CONFIG, resolver=resolver_for(answers),
                        connector=lambda *_: True)
    assert "VCF-PROBE-NO-REVERSE-DNS" in result.codes


def test_forward_mismatch_is_an_error(inventory):
    answers = {(f"{h['name']}.lab.local", False): "10.50.99.99"
               for h in inventory["hosts"]}
    result = run_probes(inventory, CONFIG, resolver=resolver_for(answers),
                        connector=lambda *_: True)
    assert "VCF-PROBE-FORWARD-MISMATCH" in result.codes


def test_blocked_target_is_neither_resolved_nor_contacted(inventory):
    resolved, contacted = [], []

    def resolver(name, want_reverse=False):
        resolved.append(name)
        return None

    def connector(host, port, timeout):
        contacted.append(host)
        return True

    inventory["hosts"] = [{"name": "evil", "mgmtIp": "8.8.8.8",
                           "vmnics": ["vmnic0", "vmnic1"], "hardware": {}}]
    result = run_probes(inventory, CONFIG, resolver=resolver, connector=connector)
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes
    assert resolved == [] and contacted == []


def test_blocked_target_resolver_is_never_invoked_even_if_it_would_raise(inventory):
    """A DNS lookup of an attacker-chosen name is itself the exfiltration
    channel. permits() must gate the path BEFORE the resolver/connector are
    called at all -- not just before their results are trusted. A resolver
    that raises on any call proves the seam sits in the right place: it must
    never be invoked for an off-allowlist target.
    """
    def resolver(name, want_reverse=False):
        raise AssertionError("resolver must not be called for a blocked target")

    def connector(host, port, timeout):
        raise AssertionError("connector must not be called for a blocked target")

    inventory["hosts"] = [{"name": "evil", "mgmtIp": "8.8.8.8",
                           "vmnics": ["vmnic0", "vmnic1"], "hardware": {}}]
    result = run_probes(inventory, CONFIG, resolver=resolver, connector=connector)
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes


def test_unreachable_target_is_info_not_failure(inventory):
    inventory["hosts"] = inventory["hosts"][:1]
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: False)
    assert result.valid is True
    assert "VCF-PROBE-UNKNOWN" in result.codes


def test_empty_allowlist_blocks_everything(inventory):
    result = run_probes(inventory, ProbeConfig(), resolver=all_good(inventory),
                        connector=lambda *_: True)
    assert set(result.codes) == {"VCF-PROBE-TARGET-BLOCKED"}


@pytest.mark.parametrize("code", ["VCF-PROBE-NO-REVERSE-DNS",
                                  "VCF-PROBE-FORWARD-MISMATCH",
                                  "VCF-PROBE-TARGET-BLOCKED", "VCF-PROBE-UNKNOWN"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()
