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


def resolver_for(answers, aliases=None):
    """A fake resolver over a dict of {(name, want_reverse): answer}.

    The third mode answers the alias query. Its default is `(name,)` --
    the name is its own canonical name with no aliases -- which is what a
    zone without CNAMEs returns, so a test that does not care about
    aliases does not have to say so.
    """
    aliases = aliases or {}

    def resolve(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return aliases.get(name, (name,))
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
    """A healthy lab: forward, reverse and alias answers for every host
    **and** every appliance name.

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

    def resolver(name, want_reverse=False):
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
    # Nothing belonging to the blocked host -- neither its name nor its
    # address -- reached the resolver. The appliance names are a separate
    # path with its own gate (permits_name, then permits() on the answer)
    # and its own tests below; they are the only thing that may appear.
    assert set(resolved) <= appliance_names
    assert contacted == []


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

    def slow_resolver(name, want_reverse=False):
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
                                  "VCF-PROBE-RESOLVER-MISMATCH"])
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

    def resolver(name, want_reverse=False):
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

    def resolver(name, want_reverse=False, want_aliases=False):
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

    def resolver(name, want_reverse=False, want_aliases=False):
        asked.append(name)
        return healthy(name, want_reverse, want_aliases)

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
    assert set(contacted) <= host_ips


def test_an_appliance_resolving_outside_the_allowlist_is_blocked(inventory):
    # The zone -- not the operator -- chose this address. It must not be
    # reverse-resolved and must not be connected to.
    reversed_names, contacted = [], []

    def resolver(name, want_reverse=False, want_aliases=False):
        if want_reverse:
            reversed_names.append(name)
            return None
        return "203.0.113.9"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda h, p, t: (contacted.append(h), True)[1],
                        resolv_conf_reader=lambda: ())
    assert "203.0.113.9" not in reversed_names
    assert "203.0.113.9" not in contacted
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes


def test_a_cname_alias_is_not_a_reverse_mismatch(inventory):
    # gethostbyaddr returns the CANONICAL name. Without alias handling this
    # flags a valid lab at `error` and fails the whole document.
    def resolver(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return ("vcenter-real.vcf.lab.knowledgeondemand.net", name)
        if want_reverse:
            return "vcenter-real.vcf.lab.knowledgeondemand.net"
        return "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    mism = [f for f in result.findings if f.code == "VCF-PROBE-REVERSE-MISMATCH"
            and f.path.startswith("/appliances")]
    assert mism == []


def test_a_genuine_reverse_mismatch_on_an_appliance_is_still_reported(inventory):
    def resolver(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return (name,)
        if want_reverse:
            return "someone-else.vcf.lab.knowledgeondemand.net"
        return "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert any(f.code == "VCF-PROBE-REVERSE-MISMATCH" and f.path.startswith("/appliances")
               for f in result.findings)


def test_vip_like_names_are_exempt_from_requiring_a_ptr(inventory):
    def resolver(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return (name,)
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
