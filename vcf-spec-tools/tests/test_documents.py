import pytest
import yaml
from vcfspec.documents import (DocumentKind, DocumentTooDeep, DocumentTooLarge,
                               DocumentTooManyAliases, detect_kind, load_document)


def test_detects_inventory_by_apiversion_and_kind():
    kind, result = detect_kind({"apiVersion": "vcfspec/v1", "kind": "LabInventory"})
    assert kind is DocumentKind.INVENTORY and result.findings == ()


def test_detects_native_sddcspec_by_required_keys():
    kind, result = detect_kind({"sddcId": "lab", "dnsSpec": {}, "networkSpecs": [],
                                "vcenterSpec": {}})
    assert kind is DocumentKind.SDDC_SPEC and result.valid is True


def test_unrecognised_document_never_guesses():
    kind, result = detect_kind({"something": "else"})
    assert kind is DocumentKind.UNKNOWN
    assert result.codes == ("VCF-INPUT-UNRECOGNISED",)


def test_override_wins_over_sniffing():
    kind, _ = detect_kind({"sddcId": "lab", "dnsSpec": {}, "networkSpecs": [],
                           "vcenterSpec": {}}, override="inventory")
    assert kind is DocumentKind.INVENTORY


def test_unknown_override_is_a_finding_not_an_exception():
    kind, result = detect_kind({"apiVersion": "vcfspec/v1", "kind": "LabInventory"},
                               override="nonsense")
    assert kind is DocumentKind.UNKNOWN
    assert result.codes == ("VCF-INPUT-BAD-KIND",)


def test_override_literally_spelled_unknown_is_also_a_finding():
    """DocumentKind.UNKNOWN is a real enum member ("unknown"), so a bare
    `DocumentKind(override)` construction used to accept override="unknown"
    without raising ValueError, unlike any other bad string -- silently
    routing to the same "kind could not be determined" branch as a
    genuinely unrecognised document, but with no finding attached, since
    that branch normally relies on the caller (detect_kind's own
    fallback path) to attach one. The net effect upstream, before this
    fix, was validate_document(doc, input_kind="unknown") reporting
    `valid: true` with zero findings on a document nothing had validated.
    "unknown" must be rejected exactly like any other invalid override.
    """
    kind, result = detect_kind({"apiVersion": "vcfspec/v1", "kind": "LabInventory"},
                               override="unknown")
    assert kind is DocumentKind.UNKNOWN
    assert result.codes == ("VCF-INPUT-BAD-KIND",)


def test_rejects_oversized_document():
    with pytest.raises(DocumentTooLarge):
        load_document("a: " + "x" * 2_000_001)


def test_rejects_deeply_nested_document():
    deep = "a:\n" + "".join(f"{' ' * (i + 1)}b{i}:\n" for i in range(60)) + " " * 61 + "c: 1"
    with pytest.raises(DocumentTooDeep):
        load_document(deep)


def test_uses_safe_load_so_python_tags_are_rejected():
    with pytest.raises(yaml.YAMLError):
        load_document("!!python/object/apply:os.system ['echo pwned']")


def test_rejects_a_document_that_blows_the_parser_stack():
    bomb = "a: " + "[" * 10000 + "]" * 10000
    with pytest.raises(DocumentTooDeep):
        load_document(bomb)


def test_rejects_alias_bomb_before_parsing():
    doc = "a: &x [1,2]\n" + "".join(f"b{i}: *x\n" for i in range(150))
    with pytest.raises(DocumentTooManyAliases):
        load_document(doc)
