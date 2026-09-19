import dataclasses

import pytest
from vcfspec.findings import Finding, Result, Severity


def f(code="X", severity=Severity.ERROR, path="/a"):
    return Finding(code=code, severity=severity, path=path, message="m",
                   fix="do x", source="docs", source_url="https://example.invalid/d")


def test_valid_is_true_when_only_warnings_and_info():
    assert Result((f(severity=Severity.WARNING), f(severity=Severity.INFO))).valid is True


def test_valid_is_false_on_error_or_critical():
    assert Result((f(severity=Severity.ERROR),)).valid is False
    assert Result((f(severity=Severity.CRITICAL),)).valid is False


def test_merge_returns_new_result_and_does_not_mutate():
    a, b = Result((f(code="A"),)), Result((f(code="B"),))
    assert a.merge(b).codes == ("A", "B")
    assert a.codes == ("A",)


def test_finding_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        f().code = "changed"


def test_severity_serialises_as_a_plain_string():
    assert f().severity == "error"
    assert dataclasses.asdict(f())["severity"] == "error"


def test_by_severity_returns_only_that_severitys_findings():
    result = Result((f(code="A", severity=Severity.ERROR),
                     f(code="B", severity=Severity.WARNING),
                     f(code="C", severity=Severity.ERROR)))
    errors = result.by_severity(Severity.ERROR)
    assert [x.code for x in errors] == ["A", "C"]
    assert all(x.severity is Severity.ERROR for x in errors)


def test_by_severity_is_empty_when_nothing_matches():
    result = Result((f(severity=Severity.WARNING),))
    assert result.by_severity(Severity.CRITICAL) == ()
