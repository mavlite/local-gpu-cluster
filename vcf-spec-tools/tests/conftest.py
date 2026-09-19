"""Fixtures: every test gets its own copy of the example inventory."""
from __future__ import annotations

import copy

import pytest
from vcfspec.inventory import load_example


@pytest.fixture
def inventory() -> dict:
    return copy.deepcopy(load_example())


@pytest.fixture
def make_inventory():
    """make_inventory(**{"nsx.tepPool.cidr": "10.0.0.0/24"}) -> modified copy."""

    def build(**overrides) -> dict:
        doc = copy.deepcopy(load_example())
        for dotted, value in overrides.items():
            node = doc
            *parents, leaf = dotted.split(".")
            for key in parents:
                node = node[key]
            node[leaf] = value
        return doc

    return build
