from semver import bump, compare, parse


def test_parse_a_plain_version():
    assert parse("1.2.3") == (1, 2, 3, ())


def test_compare_core_versions():
    assert compare("1.2.3", "1.2.4") == -1
    assert compare("2.0.0", "2.0.0") == 0


def test_bump_minor():
    assert bump("1.2.3", "minor") == "1.3.0"
