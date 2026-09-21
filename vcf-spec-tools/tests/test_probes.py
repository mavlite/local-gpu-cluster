import time

import pytest
from vcfspec.findings import Result, Severity
from vcfspec.rules import load_catalogue
from vcfspec.validate.probes import ProbeConfig, run_probes

# Both gates have to be configured for a probe run to do anything: the IP
# allowlist for the target, and the domain allowlist for the name a forward
# lookup would resolve. Both fail closed, so a config that names only one
# of them is a config that checks half of what the operator asked for.
CONFIG = ProbeConfig(allowlist=("10.50.0.0/16",),
                     domain_allowlist=("vcf.lab.knowledgeondemand.net",))


def resolver_for(answers, canonical=None):
    """A fake resolver over a dict of {(name, want_reverse): answer}.

    The third mode answers the single combined forward query the appliance
    path issues: `(canonical name, primary address)`, or None when the
    name does not resolve. The default canonical name is the queried name
    itself -- what a zone with no CNAME returns -- so a test that does not
    care about canonicalisation does not have to say so.
    """
    canonical = canonical or {}

    def resolve(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            address = answers.get((name, False))
            return None if address is None else (canonical.get(name, name), address)
        return answers.get((name, want_reverse))
    return resolve


def appliance_fqdns(inventory) -> tuple[str, ...]:
    """Every appliance name the probe layer will ask about.

    Composed the way probes.py composes them: by *field*, never by
    inspecting the value for a dot. vipFqdn/platformFqdn/instanceFqdn are
    already fully qualified; vcenter.hostname, sddcManager.hostname and
    nsx.managers[] are short names that take the subdomain.
    """
    subdomain = inventory["dns"]["subdomain"]
    appliances = inventory.get("appliances") or {}
    nsx = inventory.get("nsx") or {}
    vsp = appliances.get("vsp") or {}
    short = [(appliances.get("vcenter") or {}).get("hostname"),
             (appliances.get("sddcManager") or {}).get("hostname"),
             *(nsx.get("managers") or ())]
    qualified = [nsx.get("vipFqdn"), vsp.get("platformFqdn"), vsp.get("instanceFqdn")]
    return tuple([f"{name}.{subdomain}" for name in short if name]
                 + [name for name in qualified if name])


def all_good(inventory):
    """A healthy lab: forward, reverse and combined-forward answers for
    every host **and** every appliance name. Every name is its own
    canonical name here -- no CNAMEs.

    One helper, not a host-only one plus an appliance-aware twin. The
    moment appliances are probed, a host-only fake makes
    test_clean_environment_produces_no_findings red for a reason that has
    nothing to do with a defect, and the honest fix is to make the fake
    describe the whole environment rather than to weaken the assertion.
    """
    answers = {}
    for host in inventory["hosts"]:
        fqdn = f"{host['name']}.vcf.lab.knowledgeondemand.net"
        answers[(fqdn, False)] = host["mgmtIp"]
        answers[(host["mgmtIp"], True)] = fqdn
    # Distinct addresses, inside the same allowlisted /16: two names
    # sharing one address would make each one's PTR the other's mismatch.
    for offset, fqdn in enumerate(appliance_fqdns(inventory)):
        ip = f"10.50.10.{40 + offset}"
        answers[(fqdn, False)] = ip
        answers[(ip, True)] = fqdn
    return resolver_for(answers)


def test_clean_environment_produces_no_findings(inventory):
    # inventory declares dns.nameservers: ["10.50.10.5"]; inject a reader
    # that agrees with it so the resolver-vantage check stays silent and
    # this assertion tests what it says it tests, not the runner's own
    # /etc/resolv.conf (which does not even exist on every platform this
    # suite runs on).
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    assert result.findings == ()


def test_missing_reverse_record_is_reported(inventory):
    answers = {(f"{h['name']}.vcf.lab.knowledgeondemand.net", False): h["mgmtIp"]
               for h in inventory["hosts"]}
    result = run_probes(inventory, CONFIG, resolver=resolver_for(answers),
                        connector=lambda *_: True)
    assert "VCF-PROBE-NO-REVERSE-DNS" in result.codes


def test_forward_mismatch_is_an_error(inventory):
    answers = {(f"{h['name']}.vcf.lab.knowledgeondemand.net", False): "10.50.99.99"
               for h in inventory["hosts"]}
    result = run_probes(inventory, CONFIG, resolver=resolver_for(answers),
                        connector=lambda *_: True)
    assert "VCF-PROBE-FORWARD-MISMATCH" in result.codes


def test_blocked_target_is_neither_resolved_nor_contacted(inventory):
    resolved, contacted = [], []

    def resolver(name, want_reverse=False, want_canonical=False):
        resolved.append(name)
        return None

    def connector(host, port, timeout):
        contacted.append(host)
        return True

    appliance_names = set(appliance_fqdns(inventory))
    inventory["hosts"] = [{"name": "evil", "mgmtIp": "8.8.8.8",
                           "vmnics": ["vmnic0", "vmnic1"], "hardware": {}}]
    result = run_probes(inventory, CONFIG, resolver=resolver, connector=connector)
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes
    # Equality, not `<=`. A subset assertion over an empty set proves
    # nothing, and that is exactly what this became when the resolver
    # contract grew a third argument: this fake still had two parameters,
    # every appliance call died of TypeError inside the worker thread, and
    # `resolved` was []. The test stayed green while checking nothing. So
    # it now requires that the appliance names DO appear, as well as that
    # nothing belonging to the blocked host does.
    assert set(resolved) == appliance_names
    assert contacted == []


def test_blocked_target_resolver_is_never_invoked_even_if_it_would_raise(inventory):
    """A DNS lookup of an attacker-chosen name is itself the exfiltration
    channel. permits() must gate the path BEFORE the resolver/connector are
    called at all -- not just before their results are trusted. A resolver
    that raises on any call proves the seam sits in the right place: it must
    never be invoked for an off-allowlist target.
    """
    def resolver(name, want_reverse=False, want_canonical=False):
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
    # Exact-set assertion below, so the resolver-vantage check needs a
    # reader that agrees with the inventory's declared nameserver -- same
    # reasoning as test_clean_environment_produces_no_findings.
    result = run_probes(inventory, ProbeConfig(), resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    # The hosts are stopped by the IP gate. The appliances have no declared
    # address for that gate to read, so the empty *domain* allowlist is what
    # stops them -- one refusal per appliance name, before any lookup.
    assert set(result.codes) == {"VCF-PROBE-TARGET-BLOCKED",
                                 "VCF-PROBE-NAME-BLOCKED",
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
                         domain_allowlist=("vcf.lab.knowledgeondemand.net",))

    def slow_resolver(name, want_reverse=False, want_canonical=False):
        # Accepts the third parameter deliberately. A two-parameter fake
        # would make the appliance path raise TypeError inside the worker
        # thread, return None instantly, and quietly stop being bounded by
        # anything -- so this test would pass while testing the host path
        # alone.
        time.sleep(5)
        return None

    start = time.monotonic()
    result = run_probes(inventory, config, resolver=slow_resolver,
                        connector=lambda *_: True)
    elapsed = time.monotonic() - start

    assert isinstance(result, Result)
    # Two resolve() calls for the one host (forward + reverse) plus one
    # forward per appliance name -- the appliance forward answer is None,
    # so no reverse or alias call follows it. Every one of them is bounded
    # by timeout_s, so elapsed sits near calls*timeout_s. A single
    # unbounded call would alone blow past this, since the resolver
    # sleeps 5s.
    calls = 2 + len(appliance_fqdns(inventory))
    assert elapsed < calls * config.timeout_s + 1.0
    assert "VCF-PROBE-UNKNOWN" in result.codes


@pytest.mark.parametrize("code", ["VCF-PROBE-NO-REVERSE-DNS",
                                  "VCF-PROBE-FORWARD-MISMATCH",
                                  "VCF-PROBE-TARGET-BLOCKED", "VCF-PROBE-UNKNOWN",
                                  "VCF-PROBE-NAME-BLOCKED",
                                  "VCF-PROBE-NOTHING-PERMITTED",
                                  "VCF-PROBE-RESOLVER-MISMATCH",
                                  "VCF-PROBE-SEAM-UNUSABLE"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()


# --- VCF-PROBE-RESOLVER-MISMATCH: the vantage-point note --------------------
#
# Rev 3's answer was to query the declared nameservers directly, which needs
# dnspython and drags in the whole containment problem this module exists to
# solve. Rev 4 reports the mismatch instead: it compares the runner's own
# resolver configuration (read from /etc/resolv.conf) against what the
# inventory declared, and says nothing stronger than "these answers came
# from somewhere else" -- info, not error, because a corporate forwarder or
# systemd-resolved trips this while resolving perfectly.

def test_resolver_mismatch_is_info_and_fires_once(inventory):
    inventory["dns"]["nameservers"] = ["10.50.10.5"]
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    hits = [f for f in result.findings if f.code == "VCF-PROBE-RESOLVER-MISMATCH"]
    assert len(hits) == 1                      # once per run, not once per host
    assert hits[0].severity is Severity.INFO
    assert hits[0].path == "/dns/nameservers"


def test_no_mismatch_when_a_declared_nameserver_is_in_use(inventory):
    inventory["dns"]["nameservers"] = ["10.50.10.5", "10.50.10.6"]
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.6",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes


def test_unreadable_resolv_conf_is_unknown_not_mismatch(inventory):
    def boom():
        raise OSError("no such file")
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True, resolv_conf_reader=boom)
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes
    assert "VCF-PROBE-UNKNOWN" in result.codes


def test_no_declared_nameservers_means_nothing_to_compare(inventory):
    inventory["dns"].pop("nameservers", None)
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes


def test_a_bare_string_nameservers_is_skipped_not_iterated_as_characters(inventory):
    """On a schema-invalid document dns.nameservers may be a scalar string.
    Iterating it directly yields its characters, so a "declared" list of
    ('1', '0', '.', '5', '0', ...) would reach the operator-facing message.
    A wrong-typed section is skipped, never garbled.
    """
    inventory["dns"]["nameservers"] = "10.50.10.5"
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes
    assert not any("','" in f.message or "1, 0, ." in f.message
                   for f in result.findings)


def test_a_dict_nameservers_is_skipped_not_iterated_as_its_keys(inventory):
    inventory["dns"]["nameservers"] = {"primary": "10.50.10.5"}
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes
    assert not any("primary" in f.message for f in result.findings)


def test_a_reader_raising_something_other_than_oserror_still_yields_unknown(inventory):
    """resolv_conf_reader is a public keyword parameter, so the exception it
    raises is not this module's to assume. A reader raising ValueError must
    still come back as an ordinary Result carrying one VCF-PROBE-UNKNOWN,
    never an exception escaping run_probes -- the same discipline
    _bounded_resolve already applies to the resolver/connector seams.
    """
    def boom():
        raise ValueError("not a resolv.conf line")
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True, resolv_conf_reader=boom)
    hits = [f for f in result.findings if f.code == "VCF-PROBE-UNKNOWN"
            and f.path == "/dns/nameservers"]
    assert len(hits) == 1
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes


def test_the_reader_is_not_built_at_import_or_as_a_default(monkeypatch):
    # The real reader touches the filesystem. Importing this module, and
    # calling run_probes with probes that never need it, must not.
    import builtins
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: (opened.append(a[0]), real_open(*a, **k))[1])
    import importlib
    import vcfspec.validate.probes as probes_module
    importlib.reload(probes_module)
    assert not any("resolv.conf" in str(p) for p in opened)

    # Extended: a real run_probes call, on a document that declares no
    # nameservers at all, must not need the reader either -- pin the
    # behaviour, not just the module-load check above.
    no_nameservers = {"dns": {"subdomain": "vcf.lab.knowledgeondemand.net"},
                      "hosts": []}
    probes_module.run_probes(no_nameservers, ProbeConfig())
    assert not any("resolv.conf" in str(p) for p in opened)


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
    def resolve(name, want_reverse=False, want_canonical=False):
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
                                    domain_allowlist=("vcf.lab.knowledgeondemand.net",)),
                        resolver=tracking_resolver(calls),
                        connector=lambda *_: True)
    assert forward_calls(calls) == []
    assert "VCF-PROBE-NAME-BLOCKED" in result.codes
    assert "VCF-PROBE-FORWARD-MISMATCH" not in result.codes


def test_no_domain_allowlist_issues_no_forward_lookup_at_all():
    """Fails closed. An operator who configures only an IP allowlist gets
    zero forward lookups, not lookups of whatever the document names."""
    resolved = []

    def resolver(name, want_reverse=False, want_canonical=False):
        resolved.append((name, want_reverse))
        return None

    result = run_probes(exfil_inventory(subdomain="vcf.lab.knowledgeondemand.net"),
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
                                    domain_allowlist=("vcf.lab.knowledgeondemand.net",)),
                        resolver=tracking_resolver(calls),
                        connector=lambda *_: True)
    assert forward_calls(calls) == []
    assert "VCF-PROBE-NAME-BLOCKED" in result.codes


def test_a_suffix_match_is_on_whole_labels_not_characters():
    """'vcf.lab.knowledgeondemand.net' must not permit 'evil-vcf.lab.knowledgeondemand.net', which is a different
    zone with a different authoritative nameserver -- the same
    segment-versus-character distinction api._is_blocked makes for JSON
    pointers."""
    config = ProbeConfig(allowlist=("10.50.0.0/16",),
                         domain_allowlist=("vcf.lab.knowledgeondemand.net",))
    assert config.permits_name("esx01.vcf.lab.knowledgeondemand.net") is True
    assert config.permits_name("vcf.lab.knowledgeondemand.net") is True
    assert config.permits_name("esx01.evil-vcf.lab.knowledgeondemand.net") is False
    assert config.permits_name("vcf.lab.knowledgeondemand.net.attacker.example") is False
    assert config.permits_name("") is False
    assert config.permits_name(None) is False


def test_a_permitted_name_on_an_allowlisted_ip_still_resolves(inventory):
    """The gate must not be a blanket refusal: the whole point is that a
    correctly configured run still does the work."""
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    assert result.findings == ()


# --- An allowlist that matches nothing is not a clean pass ------------------

def test_an_allowlist_matching_no_host_is_a_blocking_finding(inventory):
    """--probe --allowlist 203.0.113.0/24 against a 10.50.10.0/24 lab used
    to exit 0 with valid: true and layers_run including "probes", having
    made zero lookups. Non-emptiness was the wrong check."""
    calls: list = []
    result = run_probes(inventory,
                        ProbeConfig(allowlist=("203.0.113.0/24",),
                                    domain_allowlist=("vcf.lab.knowledgeondemand.net",)),
                        resolver=tracking_resolver(calls),
                        connector=lambda *_: True)
    # No host name and no host address reached the resolver. The appliance
    # names did: an IP allowlist cannot gate a name (that is this module's
    # whole thesis), so they are gated by permits_name, and it is their
    # *answer* that permits() then gates.
    host_targets = ({f"{h['name']}.vcf.lab.knowledgeondemand.net"
                     for h in inventory["hosts"]}
                    | {h["mgmtIp"] for h in inventory["hosts"]})
    assert not (host_targets & {name for name, _ in calls})
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
    result = run_probes({"dns": {"subdomain": "vcf.lab.knowledgeondemand.net"}, "hosts": []},
                        CONFIG, resolver=tracking_resolver([]),
                        connector=lambda *_: True)
    assert result.findings == ()


def test_reverse_dns_pointing_at_a_different_host_is_a_finding(inventory):
    """A PTR that exists but names someone else is the failure this tool was
    built for: VCF validates both directions, and a RouterOS-style
    auto-generated PTR is exactly how a lab acquires a wrong one. Found
    against real dnsmasq -- the old check only asked whether reverse
    returned anything, so an impostor name passed silently.
    """
    answers = {}
    for host in inventory["hosts"]:
        fqdn = f"{host['name']}.vcf.lab.knowledgeondemand.net"
        answers[(fqdn, False)] = host["mgmtIp"]
        answers[(host["mgmtIp"], True)] = fqdn
    # esx03's PTR names a different host entirely.
    answers[(inventory["hosts"][2]["mgmtIp"], True)] = "impostor.vcf.lab.knowledgeondemand.net"

    result = run_probes(inventory, CONFIG, resolver=resolver_for(answers),
                        connector=lambda *_: True)
    assert "VCF-PROBE-REVERSE-MISMATCH" in result.codes
    assert [f.path for f in result.findings
            if f.code == "VCF-PROBE-REVERSE-MISMATCH"] == ["/hosts/2"]
    assert not result.valid


def test_reverse_dns_match_is_case_and_trailing_dot_insensitive(inventory):
    """dnsmasq and BIND both return a trailing dot; DNS is case-insensitive.
    Neither may be read as a mismatch or every real lab fails validation.
    """
    answers = {}
    for host in inventory["hosts"]:
        fqdn = f"{host['name']}.vcf.lab.knowledgeondemand.net"
        answers[(fqdn, False)] = host["mgmtIp"]
        answers[(host["mgmtIp"], True)] = f"{host['name'].upper()}.VCF.LAB.KNOWLEDGEONDEMAND.NET."
    result = run_probes(inventory, CONFIG, resolver=resolver_for(answers),
                        connector=lambda *_: True)
    assert "VCF-PROBE-REVERSE-MISMATCH" not in result.codes


# --- The appliance names ----------------------------------------------------
#
# Four things make an appliance different from a host, and all four are
# defects if missed:
#
#   (a) A host is gated on `mgmtIp`, an address the *operator* declared. An
#       appliance declares no address, so that gate cannot fire and the
#       address comes from whoever controls the zone. permits_name() gates
#       the forward lookup, and permits() must then gate the answer before
#       anything else is done with it.
#   (b) An appliance is never connected to. Pre-Installer it does not exist,
#       so a 443 probe is guaranteed noise -- and it would be a TCP
#       connection to an address we did not choose.
#   (c) Composition is by field name. Three of the six fields already hold
#       an FQDN; appending the subdomain to those queries
#       nsx.vcf.lab.knowledgeondemand.net.vcf.lab.knowledgeondemand.net.
#       Never decide this by looking for a dot in the value: these rules run
#       on schema-invalid documents, so a value's shape proves nothing.
#   (d) gethostbyaddr() returns the CANONICAL name, and appliance names are
#       the ones most likely to be CNAMEs, so the reverse answer must be
#       accepted against the queried name or any of its aliases.


def test_short_names_are_composed_and_fqdns_are_not(inventory):
    asked = []

    def resolver(name, want_reverse=False, want_canonical=False):
        asked.append(name)
        return None

    run_probes(inventory, CONFIG, resolver=resolver, connector=lambda *_: True,
               resolv_conf_reader=lambda: ())
    # The NSX VIP is already an FQDN. Composing it again is the bug.
    assert "nsx.vcf.lab.knowledgeondemand.net" in asked
    assert not any(n.count("vcf.lab.knowledgeondemand.net") > 1 for n in asked)


def test_clean_environment_with_appliances_produces_no_findings(inventory):
    """Zero findings, *and* the appliance names were really asked about.

    Both halves matter: without the second assertion this test would pass
    just as happily against a probe layer that ignores appliances
    completely, which is exactly the state this task starts from.
    """
    asked = []
    healthy = all_good(inventory)

    def resolver(name, want_reverse=False, want_canonical=False):
        asked.append(name)
        return healthy(name, want_reverse, want_canonical)

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    assert result.findings == ()
    assert set(appliance_fqdns(inventory)) <= set(asked)


def test_an_appliance_is_never_connected_to(inventory):
    contacted = []
    run_probes(inventory, CONFIG, resolver=all_good(inventory),
               connector=lambda host, port, timeout: (contacted.append(host), True)[1],
               resolv_conf_reader=lambda: ())
    host_ips = {h["mgmtIp"] for h in inventory["hosts"]}
    # Equality, not `<=`. A subset assertion is satisfied by an empty set,
    # so on its own it cannot tell "no appliance was contacted" from "the
    # run contacted nothing at all" -- the shape that had already hollowed
    # out test_blocked_target_is_neither_resolved_nor_contacted.
    assert set(contacted) == host_ips


def test_an_appliance_resolving_outside_the_allowlist_is_blocked(inventory):
    # The zone -- not the operator -- chose this address. It must not be
    # reverse-resolved and must not be connected to.
    reversed_names, contacted = [], []

    def resolver(name, want_reverse=False, want_canonical=False):
        if want_reverse:
            reversed_names.append(name)
            return None
        return (name, "203.0.113.9") if want_canonical else "203.0.113.9"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda h, p, t: (contacted.append(h), True)[1],
                        resolv_conf_reader=lambda: ())
    assert "203.0.113.9" not in reversed_names
    assert "203.0.113.9" not in contacted
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes


#
# The CNAME case lives in test_a_legitimate_cname_is_still_not_a_reverse_mismatch
# under "Fix round 1", together with the two tests that pin what the
# accept-set may NOT contain. The version that used to sit here returned
# the queried name as the *address*, so the appliance was target-blocked
# before the comparison and the test passed without ever reaching it.


def test_a_genuine_reverse_mismatch_on_an_appliance_is_still_reported(inventory):
    def resolver(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            return (name, "10.50.10.40")
        if want_reverse:
            return "someone-else.vcf.lab.knowledgeondemand.net"
        return "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert any(f.code == "VCF-PROBE-REVERSE-MISMATCH" and f.path.startswith("/appliances")
               for f in result.findings)


def test_vip_like_names_are_exempt_from_requiring_a_ptr(inventory):
    def resolver(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            return (name, "10.50.10.40")
        return None if want_reverse else "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    no_ptr = {f.path for f in result.findings if f.code == "VCF-PROBE-NO-REVERSE-DNS"}
    assert "/nsx/vipFqdn" not in no_ptr
    assert "/appliances/vsp/platformFqdn" not in no_ptr
    assert "/appliances/vcenter/hostname" in no_ptr


def test_appliances_do_not_count_toward_nothing_permitted(inventory):
    # Every host blocked, appliances resolvable: the /hosts message must
    # still fire and still say "none of the N hosts".
    blocked = ProbeConfig(allowlist=("203.0.113.0/24",),
                          domain_allowlist=("vcf.lab.knowledgeondemand.net",))
    result = run_probes(inventory, blocked, resolver=all_good(inventory),
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    hit = [f for f in result.findings if f.code == "VCF-PROBE-NOTHING-PERMITTED"]
    assert len(hit) == 1 and hit[0].path == "/hosts"
    assert str(len(inventory["hosts"])) in hit[0].message


# --- Fix round 1 ------------------------------------------------------------

def test_a_wrong_typed_dns_section_does_not_raise(inventory):
    """`dns: "vcf.lab.example.net"` is schema-invalid, and these rules run
    on schema-invalid documents. `(inventory.get("dns") or {}).get(...)`
    raised AttributeError straight through api.py's unguarded run_probes
    call, so a mistyped one-line section reached the operator as a
    traceback instead of a finding.
    """
    healthy = all_good(inventory)
    for broken in ("vcf.lab.knowledgeondemand.net", ["vcf.lab.knowledgeondemand.net"], 7):
        inventory["dns"] = broken
        result = run_probes(inventory, CONFIG, resolver=healthy,
                            connector=lambda *_: True, resolv_conf_reader=lambda: ())
        assert isinstance(result, Result)


def test_a_wrong_typed_dns_section_does_not_raise_through_validate_document():
    """The same defect at the surface that would actually show it: the
    orchestrator does not wrap run_probes, so anything raised there is the
    caller's traceback, not a finding.
    """
    import copy
    import json

    from vcfspec.api import validate_document
    from vcfspec.inventory import load_example

    doc = copy.deepcopy(load_example())
    doc["dns"] = "vcf.lab.knowledgeondemand.net"
    out = validate_document(json.dumps(doc), probe_config=ProbeConfig())
    # Non-vacuous: the probe layer really ran over the broken document.
    assert "probes" in out["layers_run"]
    assert isinstance(out["findings"], list)


def test_a_wrong_typed_managers_section_is_skipped_not_iterated(inventory):
    """enumerate() over a bare string yields its *characters*, so
    `managers: "nsx01"` becomes five document-derived forward queries --
    n.<subdomain>, s.<subdomain>, x.<subdomain>, 0.<subdomain>,
    1.<subdomain> -- every one of them inside the allowlisted suffix, and
    so every one of them really sent.
    """
    subdomain = inventory["dns"]["subdomain"]
    for broken in ("nsx01", {"0": "nsx01"}, 3):
        inventory["nsx"]["managers"] = broken
        asked = []

        def resolver(name, want_reverse=False, want_canonical=False):
            asked.append(name)
            return None

        result = run_probes(inventory, CONFIG, resolver=resolver,
                            connector=lambda *_: True, resolv_conf_reader=lambda: ())
        assert f"n.{subdomain}" not in asked
        assert not any(f.path.startswith("/nsx/managers/") for f in result.findings)

    # A list whose *elements* are wrong-typed keeps the good ones.
    inventory["nsx"]["managers"] = [42, None, {"a": 1}, "nsx01"]
    asked = []

    def resolver(name, want_reverse=False, want_canonical=False):
        asked.append(name)
        return None

    run_probes(inventory, CONFIG, resolver=resolver, connector=lambda *_: True,
               resolv_conf_reader=lambda: ())
    assert f"nsx01.{subdomain}" in asked
    assert f"42.{subdomain}" not in asked


def test_a_padded_appliance_name_is_composed_from_the_stripped_value(inventory):
    """`hostname: " vc01 "` composed unstripped gives " vc01 .<subdomain>",
    which permits_name happily permits (it strips before matching) and
    which then goes to the resolver with an embedded space in the label.
    The already-qualified fields had the same bug: they were tested with
    .strip() and appended without it.
    """
    subdomain = inventory["dns"]["subdomain"]
    inventory["appliances"]["vcenter"]["hostname"] = " vc01 "
    inventory["nsx"]["vipFqdn"] = "  nsx.vcf.lab.knowledgeondemand.net  "
    # The host path composes a name too, from its own sibling line. It was
    # fixed a round later than the other two because no test padded a host
    # name, so the "no asked name contains a space" assertion below never
    # looked at it.
    inventory["hosts"][0]["name"] = " esx01 "
    asked = []

    def resolver(name, want_reverse=False, want_canonical=False):
        asked.append(name)
        return None

    run_probes(inventory, CONFIG, resolver=resolver, connector=lambda *_: True,
               resolv_conf_reader=lambda: ())
    assert f"vc01.{subdomain}" in asked
    assert "nsx.vcf.lab.knowledgeondemand.net" in asked
    assert f"esx01.{subdomain}" in asked
    assert not any(" " in name for name in asked)


# --- The round-trip check must stay falsifiable -----------------------------
#
# The forward zone and the reverse zone are two separate authorities, and
# this check exists to confirm they agree. Accepting the forward answer's
# whole alias list handed the forward zone a way to name the PTR it wanted
# accepted -- one party certifying its own answer, which is not a check at
# all. Only the canonical name is accepted, and only when that name is
# itself inside the operator's --allowlist-domain.


def canonical_resolver(canonical, address, ptr):
    """A zone answering: name -> (canonical, address), address -> ptr."""
    def resolve(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            return (canonical, address)
        if want_reverse:
            return ptr
        return address
    return resolve


def test_a_forward_answer_cannot_smuggle_extra_names_into_the_accept_set(inventory):
    """The demonstration from review: a forward zone that lists the PTR it
    wants accepted among the queried name's aliases made the round-trip
    check produce zero findings on any input.

    The fix was structural -- the forward mode no longer returns an alias
    list at all -- so the attack can only be expressed here as the *shape*
    of the answer: `(canonical, *aliases, address)`, which is what the
    first implementation consumed. Nothing in an over-long answer may
    reach the accept-set, and the outcome that must never occur is
    silence: whatever this document is, the appliance either round-trips
    or is reported.
    """
    def resolver(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            return (name, "attacker-owned.evil.example", "10.50.10.40")
        if want_reverse:
            return "attacker-owned.evil.example"
        return "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    reported = {f.path for f in result.findings}
    assert "/appliances/vcenter/hostname" in reported
    assert "/appliances/sddcManager/hostname" in reported


def test_a_legitimate_cname_is_still_not_a_reverse_mismatch(inventory):
    """The defect this mode was added for, and the only one it may cure:
    gethostbyaddr() returns the CANONICAL name, so a CNAME'd appliance
    inside the operator's own zone round-trips to a different string.
    """
    calls = []
    zone = canonical_resolver("vcenter-real.vcf.lab.knowledgeondemand.net",
                              "10.50.10.40",
                              "vcenter-real.vcf.lab.knowledgeondemand.net")

    def resolver(name, want_reverse=False, want_canonical=False):
        calls.append((name, want_reverse, want_canonical))
        return zone(name, want_reverse, want_canonical)

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert [f for f in result.findings if f.code == "VCF-PROBE-REVERSE-MISMATCH"
            and f.path.startswith("/appliances")] == []
    # An absence assertion needs a lower bound, or it passes just as
    # happily when the round trip never ran -- which is exactly how this
    # test's predecessor went vacuous. Both legs must really have been
    # walked for the vCenter name.
    vcenter = f"vc01.{inventory['dns']['subdomain']}"
    assert (vcenter, False, True) in calls        # combined forward
    assert ("10.50.10.40", True, False) in calls  # reverse on the answer


def test_a_canonical_name_outside_the_domain_allowlist_certifies_nothing(inventory):
    """A canonical name the operator never allowlisted is not evidence
    about the operator's zone, so it must not suppress the mismatch."""
    result = run_probes(inventory, CONFIG,
                        resolver=canonical_resolver("elsewhere.evil.example",
                                                    "10.50.10.40",
                                                    "elsewhere.evil.example"),
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert any(f.code == "VCF-PROBE-REVERSE-MISMATCH" and f.path.startswith("/appliances")
               for f in result.findings)


def test_the_healthy_appliance_path_issues_exactly_two_lookups_per_name(inventory):
    """One forward -- which carries the canonical name and the address in
    a single answer -- and one reverse. A third query is not just waste:
    it opens a window in which the address that passed permits() and the
    name that certifies its PTR are answers to two different questions,
    which a zone is free to answer inconsistently.
    """
    calls = []
    healthy = all_good(inventory)

    def resolver(name, want_reverse=False, want_canonical=False):
        calls.append(name)
        return healthy(name, want_reverse, want_canonical)

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    assert result.findings == ()
    names = appliance_fqdns(inventory)
    appliance_ips = {f"10.50.10.{40 + i}" for i in range(len(names))}
    appliance_calls = [n for n in calls if n in set(names) or n in appliance_ips]
    assert len(appliance_calls) == 2 * len(names)


# --- Fix round 2: one resolver contract, no shim ----------------------------
#
# The appliance path always needs the third argument, so the "two-parameter
# resolvers keep working" shim could not keep its promise: a 2-param fake
# raised TypeError inside the worker thread, _bounded_resolve's
# `except Exception` swallowed it, and every appliance came back as
# VCF-PROBE-UNKNOWN "no forward DNS answer" at `info` -- non-blocking,
# valid: true, and the round trip never ran. A check that reports success
# while not running is the exact failure this layer exists to prevent, so
# the shim is gone: all three arguments, every call, both paths.


def two_parameter_resolver(name, want_reverse=False):
    """A resolver written against the old contract. It would answer
    everything correctly -- that is the point. The defect was never its
    answers; it was that its arity failure was indistinguishable from a
    zone that does not resolve."""
    if want_reverse:
        return "vc01.vcf.lab.knowledgeondemand.net"
    return "10.50.10.40"


def test_a_resolver_that_cannot_take_three_arguments_is_refused_once(inventory):
    result = run_probes(inventory, CONFIG, resolver=two_parameter_resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    hits = [f for f in result.findings if f.code == "VCF-PROBE-SEAM-UNUSABLE"]
    assert len(hits) == 1                       # once, not once per name
    assert hits[0].path == "/"
    # And it is not dressed up as a DNS result: the operator is told the
    # runner could not use the resolver, not that nine names are missing.
    assert "VCF-PROBE-UNKNOWN" not in result.codes
    assert result.valid is False


def test_the_refused_resolver_is_never_called_at_all(inventory):
    """A single up-front interface check, not a per-name discovery."""
    calls = []

    def legacy(name, want_reverse=False):
        calls.append(name)
        return None

    run_probes(inventory, CONFIG, resolver=legacy, connector=lambda *_: True,
               resolv_conf_reader=lambda: ())
    assert calls == []


def test_an_unreadable_signature_proceeds_rather_than_refusing(inventory):
    """This is an interface check, not a security gate, so it fails OPEN.
    A C callable -- socket.gethostbyname is exactly one, and exactly the
    kind of thing a caller might inject -- has no signature inspect can
    read, and refusing on that basis would break a working resolver.

    `max` stands in for it here: same unreadable builtin signature, but
    it cannot touch the network even if it were somehow called with one
    argument, which keeps this test hermetic.
    """
    result = run_probes(inventory, CONFIG, resolver=max,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert "VCF-PROBE-SEAM-UNUSABLE" not in result.codes


def test_the_interface_check_does_not_gate_anything_else(inventory):
    """permits()/permits_name() remain the only gates that decide whether
    a probe happens. A resolver that passes the interface check is not
    thereby permitted anything."""
    result = run_probes(inventory, ProbeConfig(), resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    assert "VCF-PROBE-SEAM-UNUSABLE" not in result.codes
    assert set(result.codes) == {"VCF-PROBE-TARGET-BLOCKED",
                                 "VCF-PROBE-NAME-BLOCKED",
                                 "VCF-PROBE-NOTHING-PERMITTED"}


# --- Fix round 2: the appliance path says what actually happened ------------

def test_a_malformed_forward_answer_is_not_called_a_missing_answer(inventory):
    """The resolver DID answer; the answer was the wrong shape. This is
    the message an operator sees when a zone tries to smuggle extra names
    into the accept-set, so calling it "no forward DNS answer" misdescribes
    the one case that matters most.
    """
    def resolver(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            return (name, "attacker-owned.evil.example", "10.50.10.40")
        return None

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    messages = [f.message for f in result.findings
                if f.path.startswith("/appliances") and f.code == "VCF-PROBE-UNKNOWN"]
    assert messages
    assert all("no forward DNS answer" not in m for m in messages)
    assert any("pair" in m for m in messages)


def test_a_forward_answer_with_no_address_says_so(inventory):
    """(canonical, None) used to render "Refused to probe None: outside
    the configured allowlist." It failed closed, which was right, but the
    text told the operator nothing true."""
    def resolver(name, want_reverse=False, want_canonical=False):
        if want_canonical:
            return (name, None)
        return None

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    appliance = [f for f in result.findings if f.path.startswith("/appliances")]
    assert appliance
    assert all("no address" in f.message for f in appliance)
    assert not any("Refused to probe None" in f.message for f in result.findings)


# --- Fix round 3: the injected seams -----------------------------------------
#
# resolver, connector and resolv_conf_reader are all caller-supplied
# callables. What they do, what they raise and whether they are callables
# at all is not this module's to assume -- and api.py calls run_probes
# without a wrapper, so anything that escapes here reaches the caller as a
# traceback. Two rules, and both have now been broken once each: nothing
# escapes, and an unusable seam is *reported*, never quietly turned into
# "the document's names do not resolve".


class _SignatureExplodes:
    """A callable whose signature cannot even be asked for.

    Not hypothetical: any object can define __signature__, and a property
    that raises is exactly the shape a wrapper/proxy object produces when
    the thing it wraps is not yet available.
    """

    @property
    def __signature__(self):
        raise RuntimeError("signature unavailable")

    def __call__(self, name, want_reverse=False, want_canonical=False):
        return None


def test_a_resolver_whose_signature_raises_does_not_escape(inventory):
    """inspect.signature can raise anything the object's __signature__
    raises. Catching only (TypeError, ValueError) let a RuntimeError out
    of run_probes, through api.py's unwrapped call, to the caller.
    """
    result = run_probes(inventory, CONFIG, resolver=_SignatureExplodes(),
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert isinstance(result, Result)
    # And it fails OPEN: an unreadable signature is not evidence of a bad
    # resolver, so the callable is used rather than refused.
    assert "VCF-PROBE-SEAM-UNUSABLE" not in result.codes


def test_a_resolver_that_is_not_callable_is_refused_not_silently_tolerated(inventory):
    """The arity check alone let this through: inspect.signature(5) raises
    TypeError, the check failed open, every call then raised TypeError
    inside the worker thread, _bounded_resolve swallowed it, and the run
    reported a document of unresolvable names and stayed valid. That is
    the same silent pass a two-parameter resolver used to produce, reached
    by a different wrong type.
    """
    result = run_probes(inventory, CONFIG, resolver=5,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert "VCF-PROBE-SEAM-UNUSABLE" in result.codes
    assert "VCF-PROBE-UNKNOWN" not in result.codes
    assert result.valid is False


def test_a_connector_that_is_not_callable_is_refused_too(inventory):
    """Same seam, same reasoning. A non-callable connector raised
    TypeError straight out of run_probes, because connect() -- unlike
    resolve() -- is called on the calling thread with nothing catching it.
    """
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=5, resolv_conf_reader=lambda: ())
    assert isinstance(result, Result)
    assert "VCF-PROBE-SEAM-UNUSABLE" in result.codes
    assert result.valid is False


def test_no_finding_ever_echoes_an_injected_callables_exception_text(inventory):
    """redact() is a pattern masker, not a sanitizer, so an exception
    message is never safe to render: it carries filesystem paths and
    whatever else the raiser put in it. The module already says this in a
    comment at the one place it matters; this is the assertion that keeps
    it true.
    """
    secret = "/etc/shadow leaked"

    def boom():
        raise ValueError(secret)

    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True, resolv_conf_reader=boom)
    # Non-vacuous: the reader really was called and really did fail.
    assert "VCF-PROBE-UNKNOWN" in result.codes
    assert any("ValueError" in f.message for f in result.findings)
    for finding in result.findings:
        assert secret not in finding.message
        assert secret not in (finding.fix or "")


# --- Fix round 3: a host name that is not a usable name ----------------------

def test_an_unusable_host_name_is_never_composed_into_a_query(inventory):
    """`_compose_name` promised one definition of composition for both
    paths, but only stripping was shared: the appliance path also rejected
    non-strings and blanks, and the host path did not. So `name: null`
    asked the resolver for "None.vcf.lab.knowledgeondemand.net" and
    `name: ""` asked for ".vcf.lab.knowledgeondemand.net" -- which
    permits_name() permits, because ".suffix".endswith(".suffix") is True.

    Nothing escapes containment, but this is the garbled-value class this
    module has now hardened for dns.nameservers and nsx.managers, and a
    finding naming `None.lab.example.net` is not a message anyone can act
    on.
    """
    subdomain = inventory["dns"]["subdomain"]
    for broken in ("", "   ", None, 42):
        inventory["hosts"] = [{"name": broken, "mgmtIp": "10.50.10.11",
                               "vmnics": ["vmnic0"], "hardware": {}}]
        asked = []

        def resolver(name, want_reverse=False, want_canonical=False):
            asked.append(name)
            return None

        result = run_probes(inventory, CONFIG, resolver=resolver,
                            connector=lambda *_: True, resolv_conf_reader=lambda: ())
        assert f"None.{subdomain}" not in asked
        assert f".{subdomain}" not in asked
        assert f"42.{subdomain}" not in asked
        assert not any(name.startswith(".") for name in asked)
        # And the host is accounted for, not silently dropped.
        assert any(f.path == "/hosts/0" for f in result.findings)


def test_a_usable_host_name_still_resolves(inventory):
    """The guard must not be a blanket refusal."""
    asked = []
    healthy = all_good(inventory)

    def resolver(name, want_reverse=False, want_canonical=False):
        asked.append(name)
        return healthy(name, want_reverse, want_canonical)

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.5",))
    assert result.findings == ()
    assert f"esx01.{inventory['dns']['subdomain']}" in asked


# --- Fix round 3: provenance about answers that exist ------------------------

def test_the_vantage_point_note_is_silent_when_no_lookup_was_made(inventory):
    """"Probe answers came from 1.1.1.1" is a claim about answers. With
    every target blocked there are no answers, so the note describes
    nothing that happened.
    """
    inventory["hosts"] = []
    inventory.pop("appliances", None)
    inventory.pop("nsx", None)
    result = run_probes(inventory, ProbeConfig(), resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes


def test_the_vantage_point_note_still_fires_when_lookups_happened(inventory):
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" in result.codes
