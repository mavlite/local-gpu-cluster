import pytest
from vcfspec.findings import BLOCKING, Severity
from vcfspec.rules import load_catalogue
from vcfspec.rules.platform import check_platform


def test_reference_lab_has_no_blocking_findings(inventory):
    result = check_platform(inventory)
    blocking = [f.code for f in result.findings if f.severity in BLOCKING]
    assert blocking == [], blocking


def test_reference_lab_reports_evaluation_licensing(inventory):
    assert "VCF-LIC-EVALUATION" in check_platform(inventory).codes


def test_memory_tiering_clears_the_n1_shortfall(inventory):
    assert "VCF-CAP-N1-SHORTFALL" not in check_platform(inventory).codes


def test_without_tiering_the_n1_shortfall_is_reported(inventory):
    for host in inventory["hosts"]:
        host["hardware"]["memoryTieringGb"] = 0
    result = check_platform(inventory)
    assert "VCF-CAP-N1-SHORTFALL" in result.codes
    assert result.valid is True          # a warning, not an error


def test_insufficient_total_ram_is_an_error(inventory):
    for host in inventory["hosts"]:
        host["hardware"]["ramGb"] = 32
        host["hardware"]["memoryTieringGb"] = 0
    assert "VCF-CAP-RAM-SHORTFALL" in check_platform(inventory).codes


def test_insufficient_storage_after_raid_overhead_is_an_error(inventory):
    for host in inventory["hosts"]:
        host["hardware"]["vsanDeviceTb"] = 0.5
    assert "VCF-CAP-STORAGE-SHORTFALL" in check_platform(inventory).codes


def test_missing_host_hardware_is_reported_not_skipped(inventory):
    inventory["hosts"][0]["hardware"] = {}
    assert "VCF-CAP-UNKNOWN-HARDWARE" in check_platform(inventory).codes


# --- Finding 13: VCF-CAP-UNKNOWN-HARDWARE's fix text used to say "Add
# hardware.cores and hardware.ramGb for every host", promising a vCPU
# capacity check that has never existed -- _capacity_rules only ever reads
# ramGb. Corrected the text rather than adding the check, because vCPU is
# routinely oversubscribed in a vSphere cluster (unlike RAM/storage, which
# cannot be), and a raw core-count check would also fail the bundled
# example (48 physical cores across 3 hosts vs. a 76-vCPU mandatory stack),
# which is a real, deployable lab.

def test_a_host_with_cores_but_no_ram_is_still_unknown_hardware(inventory):
    """hardware.cores alone is not enough to satisfy the capacity check --
    proves the rule genuinely never substitutes cores for ramGb."""
    inventory["hosts"][0]["hardware"] = {"cores": 64}
    assert "VCF-CAP-UNKNOWN-HARDWARE" in check_platform(inventory).codes


def test_cap_unknown_hardware_fix_text_does_not_promise_a_cores_check():
    fix = load_catalogue()["VCF-CAP-UNKNOWN-HARDWARE"].fix
    assert "ramGb" in fix
    assert fix != "Add hardware.cores and hardware.ramGb for every host."
    assert "not" in fix.lower() and "cores" in fix.lower()


def test_capacity_rules_source_never_reads_hardware_cores():
    """Mutation-style guard on the fix text's own honesty: if a future
    change starts reading hardware.cores for capacity, this must be
    revisited alongside the fix text and this test, not silently drift
    out of sync with what the catalogue promises again."""
    import inspect

    from vcfspec.rules import platform
    assert '"cores"' not in inspect.getsource(platform._capacity_rules)
    assert "'cores'" not in inspect.getsource(platform._capacity_rules)


def test_reference_lab_declares_the_workaround_its_tiering_needs(inventory):
    """The example lab is consumer Ryzen hardware and already relies on
    memoryTieringGb for N-1 (see test_memory_tiering_clears_the_n1_shortfall).
    It must also declare the workaround that makes tiering actually work, or
    the capacity plan is counting RAM that will not be there.
    """
    assert "VCF-CAP-TIERING-NEEDS-WORKAROUND" not in check_platform(inventory).codes


def test_tiering_without_the_consumer_amd_workaround_is_a_finding(inventory):
    """This is the rule the whole feature exists for: memoryTieringGb > 0
    declared, but 'consumer-amd' not requested -- tiering is claimed on
    hardware that (per the spec) needs a workaround to make it function, and
    the workaround was never asked for.
    """
    inventory["provisioning"]["hostWorkarounds"] = []
    result = check_platform(inventory)
    assert "VCF-CAP-TIERING-NEEDS-WORKAROUND" in result.codes
    assert result.valid is False   # error severity: this is not a warning
    # Reported per host, not once for the whole document.
    tiering_findings = [f for f in result.findings
                        if f.code == "VCF-CAP-TIERING-NEEDS-WORKAROUND"]
    assert len(tiering_findings) == 3
    assert tiering_findings[0].path == "/hosts/0/hardware/memoryTieringGb"


def test_no_provisioning_workarounds_field_at_all_is_still_a_finding(inventory):
    """hostWorkarounds is optional; its absence must read the same as an
    empty list, not silently skip the check.
    """
    del inventory["provisioning"]["hostWorkarounds"]
    assert "VCF-CAP-TIERING-NEEDS-WORKAROUND" in check_platform(inventory).codes


def test_zero_tiering_needs_no_workaround(inventory):
    """A host with no declared tiering has nothing for the workaround to
    protect, so its absence is not a finding.
    """
    inventory["provisioning"]["hostWorkarounds"] = []
    for host in inventory["hosts"]:
        host["hardware"]["memoryTieringGb"] = 0
    assert "VCF-CAP-TIERING-NEEDS-WORKAROUND" not in check_platform(inventory).codes


def test_missing_vsan_capacity_is_a_shortfall_not_a_silent_pass(inventory):
    for host in inventory["hosts"]:
        host["hardware"].pop("vsanDeviceTb", None)
    assert "VCF-CAP-STORAGE-SHORTFALL" in check_platform(inventory).codes


def test_storage_is_not_checked_when_principal_storage_is_not_vsan(inventory):
    inventory["storage"]["type"] = "NFS"
    for host in inventory["hosts"]:
        host["hardware"].pop("vsanDeviceTb", None)
    assert "VCF-CAP-STORAGE-SHORTFALL" not in check_platform(inventory).codes


def test_uppercase_host_name_is_an_error(inventory):
    inventory["hosts"][0]["name"] = "ESX01"
    assert "VCF-NAME-NOT-LOWERCASE" in check_platform(inventory).codes


def test_uppercase_fqdn_field_is_an_error(make_inventory):
    doc = make_inventory(**{"nsx.vipFqdn": "NSX.vcf.lab.knowledgeondemand.net"})
    assert "VCF-NAME-NOT-LOWERCASE" in check_platform(doc).codes


def test_fqdn_outside_the_dns_subdomain_is_a_warning(make_inventory):
    doc = make_inventory(**{"nsx.vipFqdn": "nsx.other.local"})
    assert "VCF-NAME-WRONG-DOMAIN" in check_platform(doc).codes


def test_local_suffix_on_vsp_platform_fqdn_is_a_warning(make_inventory):
    doc = make_inventory(**{"appliances.vsp.platformFqdn": "vcf-vsp.lab.local"})
    result = check_platform(doc)
    hits = [f for f in result.findings if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]
    assert [f.path for f in hits] == ["/appliances/vsp/platformFqdn"]
    assert hits[0].severity is Severity.WARNING


def test_local_subdomain_is_reported_once_at_its_source(make_inventory):
    doc = make_inventory(**{"dns.subdomain": "vcf.lab.local"})
    doc["appliances"]["vsp"]["platformFqdn"] = "vcf-vsp.vcf.lab.local"
    doc["appliances"]["vsp"]["instanceFqdn"] = "vcf-vsp-i.vcf.lab.local"
    hits = [f for f in check_platform(doc).findings
            if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]
    # One finding, at the one-line fix -- not three naming the symptom.
    assert [f.path for f in hits] == ["/dns/subdomain"]


def test_local_is_not_flagged_on_the_components_that_still_allow_it(make_inventory):
    doc = make_inventory(**{"dns.subdomain": "vcf.lab.example.net"})
    doc["appliances"]["vcenter"]["hostname"] = "vcenter.corp.local"
    doc["appliances"]["sddcManager"]["hostname"] = "sddc.corp.local"
    doc["nsx"]["vipFqdn"] = "nsx.corp.local"
    doc["hosts"][0]["name"] = "esx01.corp.local"
    codes = check_platform(doc).codes
    assert "VCF-NAME-VSP-LOCAL-SUFFIX" not in codes


def test_sso_domain_is_exempt(make_inventory):
    # vsphere.local is an identity namespace, not a DNS domain, and is the
    # correct value. A rule that flagged it would be telling the operator to
    # break a working deployment.
    doc = make_inventory(**{"appliances.vcenter.ssoDomain": "vsphere.local"})
    assert "VCF-NAME-VSP-LOCAL-SUFFIX" not in check_platform(doc).codes


def test_subdomain_itself_is_not_reported_as_the_wrong_domain(make_inventory):
    # Adding /dns/subdomain to _named_values() puts the subdomain through
    # the WRONG-DOMAIN check, where `value.endswith("." + domain)` is false
    # for value == domain. Guard it, or every document gains a finding.
    doc = make_inventory(**{"dns.subdomain": "vcf.lab.example.net"})
    wrong = [f for f in check_platform(doc).findings
             if f.code == "VCF-NAME-WRONG-DOMAIN"]
    assert [f.path for f in wrong if f.path == "/dns/subdomain"] == []


def test_uppercase_subdomain_is_a_lowercase_violation(make_inventory):
    """Not "still": before /dns/subdomain joined _named_values() nothing
    checked the subdomain's case at all, so this is new behaviour, not a
    preserved one. It is the branch's one deliberate change to
    Result.valid -- a document with an uppercase dns.subdomain passed on
    main and fails here -- and it is correct: VCF rejects uppercase FQDNs,
    and the subdomain composes into every name the deployment publishes.
    """
    doc = make_inventory(**{"dns.subdomain": "VCF.lab.example.net"})
    lower = [f for f in check_platform(doc).findings
             if f.code == "VCF-NAME-NOT-LOWERCASE" and f.path == "/dns/subdomain"]
    assert len(lower) == 1


def test_a_local_name_not_composed_from_the_subdomain_is_reported_on_its_own(make_inventory):
    # legacy-vsp.otherco.local is .local for its own reason -- it is not
    # built on dns.subdomain, so correcting dns.subdomain would not fix it.
    # It must keep its own finding, separate from the subdomain's.
    doc = make_inventory(**{"dns.subdomain": "corp.local"})
    doc["appliances"]["vsp"]["platformFqdn"] = "legacy-vsp.otherco.local"
    doc["appliances"]["vsp"]["instanceFqdn"] = "inst.corp.local"
    hits = [f for f in check_platform(doc).findings
            if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]
    assert sorted(f.path for f in hits) == sorted(
        ["/dns/subdomain", "/appliances/vsp/platformFqdn"])


def test_suppression_comparison_is_case_and_trailing_dot_robust(make_inventory):
    doc = make_inventory(**{"dns.subdomain": "CORP.local"})
    doc["appliances"]["vsp"]["platformFqdn"] = "vsp.corp.local."
    hits = [f for f in check_platform(doc).findings
            if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]
    assert [f.path for f in hits] == ["/dns/subdomain"]


def test_vsp_pool_smaller_than_twelve_is_an_error(make_inventory):
    doc = make_inventory(**{"appliances.vsp.poolEnd": "10.50.10.107"})
    assert "VCF-VSP-POOL-TOO-SMALL" in check_platform(doc).codes


def test_vsp_pool_of_exactly_twelve_is_accepted(make_inventory):
    doc = make_inventory(**{"appliances.vsp.poolEnd": "10.50.10.111"})
    assert "VCF-VSP-POOL-TOO-SMALL" not in check_platform(doc).codes


def test_vsp_pool_must_not_cross_its_subnet(make_inventory):
    doc = make_inventory(**{"appliances.vsp.poolStart": "10.50.10.250",
                            "appliances.vsp.poolEnd": "10.50.11.10"})
    assert "VCF-VSP-POOL-CROSSES-SUBNET" in check_platform(doc).codes


def test_vsp_internal_cidr_colliding_with_a_network_is_an_error(make_inventory):
    doc = make_inventory(**{"appliances.vsp.internalCidr": "240.0.0.0/15",
                            "networks.vsan.subnet": "240.0.5.0/24",
                            "networks.vsan.gateway": "240.0.5.1"})
    assert "VCF-VSP-INTERNAL-CIDR-COLLISION" in check_platform(doc).codes


@pytest.mark.parametrize("code", [
    "VCF-NAME-NOT-LOWERCASE", "VCF-NAME-WRONG-DOMAIN", "VCF-VSP-POOL-TOO-SMALL",
    "VCF-VSP-POOL-CROSSES-SUBNET", "VCF-VSP-INTERNAL-CIDR-COLLISION",
    "VCF-CAP-RAM-SHORTFALL", "VCF-CAP-N1-SHORTFALL", "VCF-CAP-STORAGE-SHORTFALL",
    "VCF-CAP-UNKNOWN-HARDWARE", "VCF-CAP-TIERING-NEEDS-WORKAROUND",
    "VCF-LIC-EVALUATION"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()


def test_a_bare_local_subdomain_is_flagged_like_a_dotted_one(make_inventory):
    """`.endswith(".local")` misses a subdomain that IS "local": a
    single-label zone is unusual but legal to write, and it is the same
    unsupported mDNS namespace the dotted form is rejected for.
    """
    doc = make_inventory(**{"dns.subdomain": "local"})
    hits = [f for f in check_platform(doc).findings
            if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX" and f.path == "/dns/subdomain"]
    assert len(hits) == 1


def test_a_name_merely_ending_in_the_letters_local_is_not_flagged(make_inventory):
    """Whole labels, not characters -- the same distinction permits_name()
    makes. "nonlocal" and "mylocal.example.net" are ordinary names."""
    doc = make_inventory(**{"dns.subdomain": "nonlocal"})
    assert not [f for f in check_platform(doc).findings
                if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]


def test_existing_sddc_manager_requires_its_extra_credentials(make_inventory):
    """A real 9.1.1 Installer refuses the whole spec before running any check
    when useExistingDeployment is set without localUserPassword, so this has
    to be caught here rather than discovered by a round trip."""
    doc = make_inventory(**{"appliances.sddcManager.useExistingDeployment": True})
    hits = [f for f in check_platform(doc).findings
            if f.code == "VCF-SDDCM-EXISTING-NEEDS-CREDENTIALS"]
    assert {f.message.split("credentials.")[1].split(" ")[0] for f in hits} == \
        {"sddcManagerLocalUser", "sddcManagerSsh"}
    assert all(f.severity is Severity.ERROR for f in hits)


def test_the_credentials_rule_is_silent_when_they_are_declared(make_inventory):
    doc = make_inventory(**{"appliances.sddcManager.useExistingDeployment": True})
    doc["credentials"]["sddcManagerLocalUser"] = "${sddcm_local}"
    doc["credentials"]["sddcManagerSsh"] = "${sddcm_ssh}"
    assert "VCF-SDDCM-EXISTING-NEEDS-CREDENTIALS" not in check_platform(doc).codes


def test_the_credentials_rule_does_not_fire_for_a_fresh_deployment(inventory):
    """The default inventory deploys a new SDDC Manager and must stay clean --
    the extra credentials are required only when importing an existing one."""
    assert "VCF-SDDCM-EXISTING-NEEDS-CREDENTIALS" not in check_platform(inventory).codes


def test_a_wrong_typed_use_existing_flag_is_not_treated_as_true(make_inventory):
    """Only the boolean true means import. A truthy string arriving from a
    schema-invalid document must not silently demand credentials."""
    for value in ("yes", 1, "true", {}, []):
        doc = make_inventory(**{"appliances.sddcManager.useExistingDeployment": value})
        assert "VCF-SDDCM-EXISTING-NEEDS-CREDENTIALS" not in check_platform(doc).codes
