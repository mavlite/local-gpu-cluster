"""Every declared dependency carries both bounds.

Security review 2026-09-19, finding 3. The floor is a security floor:
`mcp>=1.2` admitted five advisories (PYSEC-2026-1616/1617/1618/3482/3483,
all fixed by 1.28.1). The ceiling is an availability floor: this homelab
lost mcp-sdg on 2026-08-21 when an unbounded `mcp>=1.2` resolved to 2.0.0.

This is a policy test, not a resolve test -- it reads pyproject.toml and
asserts the constraints as written, so it holds on a machine that is
offline and on one whose lockfile happens to have picked something clean.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _declared() -> list[tuple[str, str]]:
    """(where it is declared, requirement string) for every dependency."""
    config = _config()
    out = [("dependencies", req) for req in config["project"]["dependencies"]]
    for extra, reqs in config["project"].get("optional-dependencies", {}).items():
        out += [(f"optional-dependencies.{extra}", req) for req in reqs]
    out += [("build-system.requires", req)
            for req in config["build-system"]["requires"]]
    return out


def test_the_scan_itself_sees_every_dependency_group():
    """Guards the guard: if this ever reads an empty list, the parse broke
    and every assertion below would pass vacuously."""
    names = {req.split(">")[0].split("<")[0].split("=")[0]
             for _, req in _declared()}
    assert {"pyyaml", "jsonschema", "mcp", "pytest", "setuptools"} <= names


@pytest.mark.parametrize("where,requirement", _declared())
def test_every_dependency_has_an_upper_bound(where, requirement):
    assert "<" in requirement, (
        f"{where}: {requirement!r} has no upper bound. An unbounded "
        "constraint is how `mcp>=1.2` resolved to 2.0.0 and broke a "
        "service in this homelab on 2026-08-21.")


@pytest.mark.parametrize("where,requirement", _declared())
def test_every_dependency_has_a_lower_bound(where, requirement):
    assert ">=" in requirement, f"{where}: {requirement!r} has no lower bound."


def test_the_mcp_floor_excludes_every_known_advisory():
    """PYSEC-2026-3483 is the last of the five, fixed in 1.28.1."""
    mcp = _config()["project"]["optional-dependencies"]["mcp"]
    assert len(mcp) == 1
    floor = re.search(r">=\s*([0-9.]+)", mcp[0]).group(1)
    assert tuple(int(p) for p in floor.split(".")) >= (1, 28, 1), (
        f"mcp floor {floor} admits at least one of PYSEC-2026-1616, -1617, "
        "-1618, -3482, -3483")
