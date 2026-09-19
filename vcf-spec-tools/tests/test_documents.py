import time

import pytest
import yaml
from vcfspec.documents import (MAX_ALIASES, MAX_NODES, _ALIAS_RE, DocumentKind,
                               DocumentTooDeep, DocumentTooLarge,
                               DocumentTooManyAliases, DocumentTooManyNodes,
                               detect_kind, load_document)


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


# --- MAX_ALIASES counted occurrences; it never bounded the expansion -------

def alias_bomb(levels: int = 7, fan_out: int = 8) -> str:
    """The review's reproduction: 320 bytes, 56 aliases (44 under the
    MAX_ALIASES limit of 100), seven levels of eight -- which expands to
    8**7 leaves and 21,913,097 nodes. Aliases compose multiplicatively, so
    counting occurrences in the source text bounds nothing.
    """
    lines = ["a0: &a0 [" + ",".join(["x"] * fan_out) + "]"]
    for level in range(1, levels + 1):
        lines.append(f"a{level}: &a{level} ["
                     + ",".join([f"*a{level - 1}"] * fan_out) + "]")
    return "\n".join(lines) + "\n"


def test_the_alias_bomb_is_under_every_older_limit():
    """Stated explicitly so the next reader does not assume some existing
    guard catches this: it is small, shallow, and under the alias count."""
    text = alias_bomb()
    assert len(text.encode("utf-8")) == 320
    assert len(_ALIAS_RE.findall(text)) == 56 < MAX_ALIASES


def test_alias_expansion_is_refused_and_refused_quickly():
    """A guard that costs what it prevents is not a guard. MAX_DEPTH could
    never help here -- it ran after safe_load had already built the graph,
    and the document is only ten levels deep anyway."""
    start = time.monotonic()
    with pytest.raises(DocumentTooManyNodes):
        load_document(alias_bomb())
    assert time.monotonic() - start < 1.0


def test_one_more_level_of_expansion_is_also_refused_quickly():
    """The limit permitted 100 aliases; one more level is x8 again."""
    start = time.monotonic()
    with pytest.raises(DocumentTooManyNodes):
        load_document(alias_bomb(levels=8))
    assert time.monotonic() - start < 1.0


def test_aliases_themselves_are_still_perfectly_legal():
    """The bound is on expansion, not on aliases: a document that uses one
    to avoid repeating itself must still load."""
    doc = load_document("common: &c {mtu: 9000}\na: *c\nb: *c\n")
    assert doc["a"] == {"mtu": 9000} and doc["b"] == {"mtu": 9000}


def test_the_bundled_example_is_nowhere_near_the_node_budget():
    """Three orders of magnitude of headroom is the claim; check it rather
    than assert it in a comment."""
    from vcfspec.documents import _inspect
    from vcfspec.inventory import EXAMPLE_PATH
    _, nodes = _inspect(load_document(EXAMPLE_PATH.read_text(encoding="utf-8")))
    assert nodes < MAX_NODES / 100


def test_the_bomb_reaches_the_orchestrator_as_a_finding_not_an_exception():
    from vcfspec.api import validate_document
    out = validate_document(alias_bomb())
    assert out["valid"] is False
    assert out["findings"][0]["code"] == "VCF-INPUT-UNREADABLE"
    assert "DocumentTooManyNodes" in out["findings"][0]["message"]


# --- Security review 2026-09-19, finding 5: the libyaml loader -----------
#
# A legal 1.79 MB document cost ~18 s to load, 18.1 s of which cProfile
# put inside yaml.safe_load -- the parser, not the diff -- blocking the
# long-lived asyncio MCP server for the duration. CSafeLoader is the same
# safe subset, roughly 20x faster. These tests pin the two things that
# must not drift: that the loader stays inside the safe subset, and that
# it behaves identically to the pure-Python one.

def test_the_loader_is_libyaml_when_available_and_safe_either_way():
    import yaml

    from vcfspec.documents import _SAFE_LOADER
    assert _SAFE_LOADER in (getattr(yaml, "CSafeLoader", None), yaml.SafeLoader)
    # The point of the guard: never a loader that can construct objects.
    for unsafe in ("Loader", "UnsafeLoader", "FullLoader", "CLoader"):
        assert _SAFE_LOADER is not getattr(yaml, unsafe, None)
    if hasattr(yaml, "CSafeLoader"):
        assert _SAFE_LOADER is yaml.CSafeLoader


def test_python_object_tags_are_still_refused():
    """safe_load semantics, restated against whichever loader is active.
    A !!python tag must never construct anything."""
    with pytest.raises(Exception) as caught:
        load_document("a: !!python/object/apply:os.system ['echo pwned']\n")
    assert "ConstructorError" in type(caught.value).__name__ or \
        isinstance(caught.value, ValueError)


@pytest.mark.parametrize("text,expected", [
    ("a: &x 1\nb: *x\n", {"a": 1, "b": 1}),          # aliases
    ("a: 1\na: 2\n", {"a": 2}),                       # duplicate keys
    ("y: 010\nz: yes\n", {"y": 8, "z": True}),        # implicit resolution
])
def test_the_active_loader_agrees_with_pure_python_safe_load(text, expected):
    """Parity, not just speed: swapping the parser must not change what a
    document means."""
    import yaml
    assert load_document(text) == expected
    assert yaml.safe_load(text) == expected


def test_deep_nesting_is_still_DocumentTooDeep_not_a_raw_parser_error():
    """Both loaders signal their nesting limit as RecursionError (libyaml
    raises "Stack overflow"), so the single handler in load_document
    covers both. If that ever stops being true this reddens rather than
    letting a raw parser exception reach the orchestrator."""
    text = "{a: " * 10_000 + "1" + "}" * 10_000
    with pytest.raises(DocumentTooDeep):
        load_document(text)
