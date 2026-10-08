import pytest

from semver import bump, compare, parse


def test_parse_basic_and_prerelease_and_build():
    assert parse("1.2.3") == (1, 2, 3, ())
    assert parse("1.2.3-alpha.1") == (1, 2, 3, ("alpha", 1))
    assert parse("1.2.3-rc.1+build.5") == (1, 2, 3, ("rc", 1))
    assert parse("0.0.0") == (0, 0, 0, ())


@pytest.mark.parametrize("bad", ["1.2", "1.2.3.4", "a.2.3", "01.2.3", "1.02.3",
                                 "1.2.3-", "1.2.3-a..b", "1.2.3-01", "1.2.3-a_b"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_core_precedence():
    assert compare("1.10.0", "1.9.0") == 1
    assert compare("2.0.0", "10.0.0") == -1
    assert compare("1.0.0", "1.0.0") == 0


def test_release_beats_prerelease():
    assert compare("1.0.0", "1.0.0-rc.1") == 1
    assert compare("1.0.0-rc.1", "1.0.0") == -1


def test_semver_spec_ordering_chain():
    chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
             "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
    for lo, hi in zip(chain, chain[1:]):
        assert compare(lo, hi) == -1, (lo, hi)
        assert compare(hi, lo) == 1, (hi, lo)


def test_build_metadata_ignored():
    assert compare("1.0.0+a", "1.0.0+b") == 0
    assert compare("1.0.0-rc.1+x", "1.0.0-rc.1") == 0


def test_bump():
    assert bump("1.2.3", "major") == "2.0.0"
    assert bump("1.2.3", "minor") == "1.3.0"
    assert bump("1.2.3", "patch") == "1.2.4"
    assert bump("1.2.3-rc.1+b", "patch") == "1.2.4"
    with pytest.raises(ValueError):
        bump("1.2.3", "build")
