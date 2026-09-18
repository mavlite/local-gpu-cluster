import time

import pytest
from vcfspec.findings import Result
from vcfspec.rules import load_catalogue
from vcfspec.validate.probes import ProbeConfig, run_probes

# Both gates have to be configured for a probe run to do anything: the IP
# allowlist for the target, and the domain allowlist for the name a forward
# lookup would resolve. Both fail closed, so a config that names only one
# of them is a config that checks half of what the operator asked for.
CONFIG = ProbeConfig(allowlist=("10.50.0.0/16",),
                     domain_allowlist=("lab.local",))


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
    assert set(result.codes) == {"VCF-PROBE-TARGET-BLOCKED",
                                 "VCF-PROBE-NOTHING-PERMITTED"}


def test_dns_resolution_is_bounded_by_timeout(inventory):
    """resolve() must never be allowed to hang past ~timeout_s, even though
    the real gethostbyname/gethostbyaddr do not honour socket timeouts
    reliably across platforms. A resolver that sleeps well past the
    deadline must still return control to run_probes near the deadline,
    not near the sleep -- and the call must come back as an ordinary
    Result/finding, never an exception or an indefinite wait.
    """
    inventory["hosts"] = inventory["hosts"][:1]
    config = ProbeConfig(allowlist=("10.50.0.0/16",), timeout_s=0.2,
                         domain_allowlist=("lab.local",))

    def slow_resolver(name, want_reverse=False):
        time.sleep(5)
        return None

    start = time.monotonic()
    result = run_probes(inventory, config, resolver=slow_resolver,
                        connector=lambda *_: True)
    elapsed = time.monotonic() - start

    assert isinstance(result, Result)
    # Two resolve() calls per host (forward + reverse), each bounded by
    # timeout_s -- elapsed should sit near ~2*timeout_s, nowhere near the
    # resolver's 5s sleep.
    assert elapsed < 2.0
    assert "VCF-PROBE-UNKNOWN" in result.codes


@pytest.mark.parametrize("code", ["VCF-PROBE-NO-REVERSE-DNS",
                                  "VCF-PROBE-FORWARD-MISMATCH",
                                  "VCF-PROBE-TARGET-BLOCKED", "VCF-PROBE-UNKNOWN",
                                  "VCF-PROBE-NAME-BLOCKED",
                                  "VCF-PROBE-NOTHING-PERMITTED"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()


# --- The axis the existing containment tests never varied -------------------
#
# test_blocked_target_is_neither_resolved_nor_contacted and
# test_blocked_target_resolver_is_never_invoked_even_if_it_would_raise both
# set mgmtIp: 8.8.8.8 (off-allowlist) with a harmless name. They test the IP
# gate, which worked. Neither varies the *name* while keeping the IP
# allowlisted -- the only shape that exercises the threat the module
# docstring actually states. Every test below holds the IP constant, inside
# the allowlist, and varies the hostname and the subdomain.

def tracking_resolver(calls: list):
    """Records every call, then raises.

    Both halves are load-bearing. Raising is the directive's own proof that
    the seam sits before the call rather than after it -- but raising alone
    is NOT a detector here, because _bounded_resolve runs the resolver in a
    worker thread that catches Exception and returns None, so an
    AssertionError from inside it is swallowed and the run still passes.
    The recorded list is what actually fails the test, so assert on it.
    """
    def resolve(name, want_reverse=False):
        calls.append((name, want_reverse))
        raise AssertionError(f"resolver must not be called: {name!r}")
    return resolve


def forward_calls(calls: list) -> list:
    return [name for name, want_reverse in calls if not want_reverse]


def exfil_inventory(name="stolen-data-abc123", subdomain="exfil.attacker.example"):
    return {"dns": {"subdomain": subdomain},
            "hosts": [{"name": name, "mgmtIp": "10.50.10.11"}]}


def test_an_attacker_chosen_name_on_an_allowlisted_ip_is_never_resolved():
    """The reproduction from the review, exactly: the mgmtIp is inside the
    allowlist (it need not even be real), so the IP gate passes, and the
    forward lookup then carried "stolen-data-abc123.exfil.attacker.example"
    to the attacker's authoritative nameserver.
    """
    calls: list = []
    result = run_probes(exfil_inventory(),
                        ProbeConfig(allowlist=("10.50.0.0/16",),
                                    domain_allowlist=("lab.local",)),
                        resolver=tracking_resolver(calls),
                        connector=lambda *_: True)
    assert forward_calls(calls) == []
    assert "VCF-PROBE-NAME-BLOCKED" in result.codes
    assert "VCF-PROBE-FORWARD-MISMATCH" not in result.codes


def test_no_domain_allowlist_issues_no_forward_lookup_at_all():
    """Fails closed. An operator who configures only an IP allowlist gets
    zero forward lookups, not lookups of whatever the document names."""
    resolved = []

    def resolver(name, want_reverse=False):
        resolved.append((name, want_reverse))
        return None

    result = run_probes(exfil_inventory(subdomain="lab.local"),
                        ProbeConfig(allowlist=("10.50.0.0/16",)),
                        resolver=resolver, connector=lambda *_: True)
    assert "VCF-PROBE-NAME-BLOCKED" in result.codes
    # The reverse lookup still runs: it takes the allowlisted IP, not a
    # name out of the document, so it carries nothing to exfiltrate.
    assert [name for name, reverse in resolved if not reverse] == []
    assert [name for name, reverse in resolved if reverse] == ["10.50.10.11"]


def test_an_attacker_chosen_subdomain_under_a_permitted_hostname_is_blocked():
    """Varying only dns.subdomain, with a completely ordinary host name."""
    calls: list = []
    result = run_probes(exfil_inventory(name="esx01"),
                        ProbeConfig(allowlist=("10.50.0.0/16",),
                                    domain_allowlist=("lab.local",)),
                        resolver=tracking_resolver(calls),
                        connector=lambda *_: True)
    assert forward_calls(calls) == []
    assert "VCF-PROBE-NAME-BLOCKED" in result.codes


def test_a_suffix_match_is_on_whole_labels_not_characters():
    """'lab.local' must not permit 'evil-lab.local', which is a different
    zone with a different authoritative nameserver -- the same
    segment-versus-character distinction api._is_blocked makes for JSON
    pointers."""
    config = ProbeConfig(allowlist=("10.50.0.0/16",),
                         domain_allowlist=("lab.local",))
    assert config.permits_name("esx01.lab.local") is True
    assert config.permits_name("lab.local") is True
    assert config.permits_name("esx01.evil-lab.local") is False
    assert config.permits_name("lab.local.attacker.example") is False
    assert config.permits_name("") is False
    assert config.permits_name(None) is False


def test_a_permitted_name_on_an_allowlisted_ip_still_resolves(inventory):
    """The gate must not be a blanket refusal: the whole point is that a
    correctly configured run still does the work."""
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True)
    assert result.findings == ()


# --- An allowlist that matches nothing is not a clean pass ------------------

def test_an_allowlist_matching_no_host_is_a_blocking_finding(inventory):
    """--probe --allowlist 203.0.113.0/24 against a 10.50.10.0/24 lab used
    to exit 0 with valid: true and layers_run including "probes", having
    made zero lookups. Non-emptiness was the wrong check."""
    calls: list = []
    result = run_probes(inventory,
                        ProbeConfig(allowlist=("203.0.113.0/24",),
                                    domain_allowlist=("lab.local",)),
                        resolver=tracking_resolver(calls),
                        connector=lambda *_: True)
    assert calls == []
    assert "VCF-PROBE-NOTHING-PERMITTED" in result.codes
    assert result.valid is False


def test_partial_blocking_is_legitimate_and_still_probes_the_permitted_hosts(inventory):
    """A mixed allowlist must NOT be treated as the all-blocked case: the
    hosts inside it are really checked, and the verdict is not poisoned by
    the ones outside it."""
    inventory["hosts"] = inventory["hosts"][:2]
    inventory["hosts"][1]["mgmtIp"] = "203.0.113.9"
    probed = []

    def connector(host, port, timeout):
        probed.append(host)
        return True

    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=connector)
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes
    assert "VCF-PROBE-NOTHING-PERMITTED" not in result.codes
    assert probed == [inventory["hosts"][0]["mgmtIp"]]
    assert result.valid is True


def test_a_document_with_no_hosts_at_all_is_not_reported_as_all_blocked():
    """Nothing was refused, so there is nothing to warn about -- the
    finding means "your allowlist matched none of them", not "there were
    none"."""
    result = run_probes({"dns": {"subdomain": "lab.local"}, "hosts": []},
                        CONFIG, resolver=tracking_resolver([]),
                        connector=lambda *_: True)
    assert result.findings == ()
