import pytest
from vcfspec.rules import load_catalogue
from vcfspec.rules.network import check_networks


def test_clean_inventory_produces_no_network_findings(inventory):
    assert check_networks(inventory).findings == ()


def test_gateway_outside_subnet_is_an_error(make_inventory):
    doc = make_inventory(**{"networks.management.gateway": "10.99.0.1"})
    assert "VCF-NET-GATEWAY-OUTSIDE-SUBNET" in check_networks(doc).codes


def test_overlapping_subnets_are_an_error(make_inventory):
    doc = make_inventory(**{"networks.vmotion.subnet": "10.50.10.0/24"})
    assert "VCF-NET-SUBNET-OVERLAP" in check_networks(doc).codes


def test_adjacent_subnets_do_not_overlap(make_inventory):
    doc = make_inventory(**{"networks.vmotion.subnet": "10.50.11.0/25",
                            "networks.vmotion.gateway": "10.50.11.1"})
    assert "VCF-NET-SUBNET-OVERLAP" not in check_networks(doc).codes


def test_tep_pool_overlapping_a_network_is_an_error(make_inventory):
    doc = make_inventory(**{"nsx.tepPool.cidr": "10.50.12.0/24",
                            "nsx.tepPool.gateway": "10.50.12.1"})
    assert "VCF-NET-SUBNET-OVERLAP" in check_networks(doc).codes


def test_vsan_and_vmotion_sharing_a_vlan_is_an_error(make_inventory):
    doc = make_inventory(**{"networks.vsan.vlan": 1611})
    assert "VCF-NET-VLAN-REUSED" in check_networks(doc).codes


def test_transport_vlan_reusing_a_traffic_vlan_is_an_error(make_inventory):
    doc = make_inventory(**{"nsx.transportVlanId": 1612})
    assert "VCF-NET-VLAN-REUSED" in check_networks(doc).codes


def test_fabric_mtu_below_1600_is_an_error(make_inventory):
    assert "VCF-NSX-FABRIC-MTU-TOO-LOW" in check_networks(
        make_inventory(**{"nsx.fabricMtu": 1599})).codes


def test_fabric_mtu_of_exactly_1600_is_accepted(make_inventory):
    assert "VCF-NSX-FABRIC-MTU-TOO-LOW" not in check_networks(
        make_inventory(**{"nsx.fabricMtu": 1600})).codes


def test_host_ip_outside_management_subnet_is_an_error(inventory):
    inventory["hosts"][0]["mgmtIp"] = "10.99.0.11"
    assert "VCF-NET-HOST-IP-OUTSIDE-SUBNET" in check_networks(inventory).codes


def test_duplicate_host_ip_is_an_error(inventory):
    inventory["hosts"][1]["mgmtIp"] = inventory["hosts"][0]["mgmtIp"]
    assert "VCF-NET-DUPLICATE-IP" in check_networks(inventory).codes


def test_tep_pool_too_small_for_two_per_host_is_a_warning(make_inventory):
    doc = make_inventory(**{"nsx.tepPool.ranges": [
        {"start": "10.50.13.20", "end": "10.50.13.23"}]})
    assert "VCF-NSX-TEP-POOL-TOO-SMALL" in check_networks(doc).codes


@pytest.mark.parametrize("purpose", ["vmotion", "vsan"])
def test_missing_ip_pool_is_an_error(make_inventory, purpose):
    doc = make_inventory(**{f"networks.{purpose}.pool": None})
    assert "VCF-NET-IP-POOL-MISSING" in check_networks(doc).codes


def test_ip_pool_smaller_than_host_count_is_an_error(make_inventory):
    # 3 hosts in the example; this pool holds only 2 usable addresses.
    doc = make_inventory(**{"networks.vmotion.pool": {
        "start": "10.50.11.20", "end": "10.50.11.21"}})
    assert "VCF-NET-IP-POOL-TOO-SMALL" in check_networks(doc).codes


def test_ip_pool_only_reaches_host_count_by_counting_broadcast_is_an_error(make_inventory):
    # 3 raw addresses (253, 254, 255), but the Installer ignores anything
    # ending in .255 -- so this is really only 2 usable against 3 hosts.
    doc = make_inventory(**{"networks.vmotion.pool": {
        "start": "10.50.11.253", "end": "10.50.11.255"}})
    result = check_networks(doc)
    assert "VCF-NET-IP-POOL-TOO-SMALL" in result.codes
    assert "VCF-NET-IP-POOL-MISSING" not in result.codes


def test_ip_pool_that_only_reaches_host_count_via_network_address_is_an_error(make_inventory):
    # 3 raw addresses (0, 1, 2), but .0 does not count -- 2 usable, 3 needed.
    doc = make_inventory(**{"networks.vsan.pool": {
        "start": "10.50.12.0", "end": "10.50.12.2"}})
    assert "VCF-NET-IP-POOL-TOO-SMALL" in check_networks(doc).codes


def test_management_without_a_pool_is_not_flagged(inventory):
    assert "management" not in inventory["networks"] or \
        "pool" not in inventory["networks"]["management"]
    result = check_networks(inventory)
    assert "VCF-NET-IP-POOL-MISSING" not in result.codes
    assert "VCF-NET-IP-POOL-TOO-SMALL" not in result.codes


def test_updated_example_has_sufficient_ip_pools(inventory):
    result = check_networks(inventory)
    assert "VCF-NET-IP-POOL-MISSING" not in result.codes
    assert "VCF-NET-IP-POOL-TOO-SMALL" not in result.codes


def test_malformed_network_does_not_raise(make_inventory):
    doc = make_inventory(**{"networks.vsan": {"vlan": 1612}})
    check_networks(doc)   # must not raise; schema layer reports the real problem


@pytest.mark.parametrize("code", [
    "VCF-NET-GATEWAY-OUTSIDE-SUBNET", "VCF-NET-SUBNET-OVERLAP", "VCF-NET-VLAN-REUSED",
    "VCF-NSX-FABRIC-MTU-TOO-LOW", "VCF-NET-HOST-IP-OUTSIDE-SUBNET",
    "VCF-NET-DUPLICATE-IP", "VCF-NSX-TEP-POOL-TOO-SMALL",
    "VCF-NET-IP-POOL-MISSING", "VCF-NET-IP-POOL-TOO-SMALL"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()


@pytest.mark.parametrize("mutation", [
    {"networks": ["not", "a", "mapping"]},
    {"networks": "banana"},
    {"nsx": "banana"},
    {"nsx": {"tepPool": "banana"}},
    {"nsx": {"tepPool": {"ranges": "banana"}}},
    {"nsx": {"tepPool": {"ranges": ["banana"]}}},
    {"networks": {"vmotion": {"pool": "banana"}}},
    {"networks": {"vsan": {"pool": {"start": "banana", "end": "banana"}}}},
])
def test_wrong_typed_sections_do_not_raise(inventory, mutation):
    inventory.update(mutation)
    check_networks(inventory)   # must not raise


# --- Finding 13: rules/network.py and rules/platform.py each carried a
# byte-identical private copy of _mapping/_address/_network. Both now
# import the same functions from rules/coerce.py -- `is`, not just
# behavioural equality, so a future edit to one module cannot silently
# re-fork a "local" copy without this catching it.

def test_network_and_platform_share_the_same_coerce_helpers():
    from vcfspec.rules import coerce, network, platform
    assert network._mapping is coerce.as_mapping
    assert network._address is coerce.as_address
    assert network._network is coerce.as_network
    assert platform._mapping is coerce.as_mapping
    assert platform._address is coerce.as_address
    assert platform._network is coerce.as_network
