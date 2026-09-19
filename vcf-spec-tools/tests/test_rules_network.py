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


def test_malformed_network_does_not_raise(make_inventory):
    doc = make_inventory(**{"networks.vsan": {"vlan": 1612}})
    check_networks(doc)   # must not raise; schema layer reports the real problem


@pytest.mark.parametrize("code", [
    "VCF-NET-GATEWAY-OUTSIDE-SUBNET", "VCF-NET-SUBNET-OVERLAP", "VCF-NET-VLAN-REUSED",
    "VCF-NSX-FABRIC-MTU-TOO-LOW", "VCF-NET-HOST-IP-OUTSIDE-SUBNET",
    "VCF-NET-DUPLICATE-IP", "VCF-NSX-TEP-POOL-TOO-SMALL"])
def test_codes_exist_in_catalogue(code):
    assert code in load_catalogue()


@pytest.mark.parametrize("mutation", [
    {"networks": ["not", "a", "mapping"]},
    {"networks": "banana"},
    {"nsx": "banana"},
    {"nsx": {"tepPool": "banana"}},
    {"nsx": {"tepPool": {"ranges": "banana"}}},
    {"nsx": {"tepPool": {"ranges": ["banana"]}}},
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
