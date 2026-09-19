import pytest
from vcfspec.findings import BLOCKING
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
    doc = make_inventory(**{"nsx.vipFqdn": "NSX.lab.local"})
    assert "VCF-NAME-NOT-LOWERCASE" in check_platform(doc).codes


def test_fqdn_outside_the_dns_subdomain_is_a_warning(make_inventory):
    doc = make_inventory(**{"nsx.vipFqdn": "nsx.other.local"})
    assert "VCF-NAME-WRONG-DOMAIN" in check_platform(doc).codes


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
    "VCF-CAP-UNKNOWN-HARDWARE", "VCF-LIC-EVALUATION"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()
