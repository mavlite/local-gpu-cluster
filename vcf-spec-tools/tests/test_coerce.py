"""Direct unit coverage for the shared coercion helpers in rules/coerce.py.

as_sequence() is the one under most scrutiny here: the `or []` idiom it
replaces only rescues a *falsy* wrong value, so a truthy non-iterable (an
int, a bool, a dict) or a string (iterable, but never what "a sequence of
entries" means for this package) sails straight through into enumerate()
or len() and raises. See _address_strings() in vcfspec/validate/probes.py
for the sibling reasoning on the string case.
"""
from __future__ import annotations

from vcfspec.rules.coerce import as_sequence


def test_list_passes_through():
    assert as_sequence(["a", "b"]) == ["a", "b"]


def test_tuple_becomes_a_list_of_its_items():
    assert as_sequence(("a", "b")) == ["a", "b"]


def test_int_becomes_empty():
    assert as_sequence(5) == []


def test_bool_becomes_empty():
    """True/False are ints in Python and would otherwise be treated as a
    truthy scalar that `or []` cannot rescue -- exactly the `hosts: true`
    crash this helper exists to prevent."""
    assert as_sequence(True) == []
    assert as_sequence(False) == []


def test_string_becomes_empty_not_iterated_into_characters():
    """A string is iterable, but iterating one yields its characters -- the
    same garbling class already fixed for dns.nameservers, nsx.managers and
    the resolver-config reader. A string must be treated as absent."""
    assert as_sequence("esx01") == []


def test_dict_becomes_empty():
    assert as_sequence({"a": 1}) == []


def test_none_becomes_empty():
    assert as_sequence(None) == []


def test_float_becomes_empty():
    assert as_sequence(3.5) == []


def test_generator_becomes_empty():
    """Unlike probes._address_strings (which accepts any iterable because it
    handles an injected caller SEAM), as_sequence() only ever handles
    DOCUMENT data -- and a document only ever spells a sequence as a YAML
    list or tuple. A generator is not a shape yaml.safe_load ever produces
    here, so it is treated the same as any other wrong type."""
    assert as_sequence(x for x in range(3)) == []
