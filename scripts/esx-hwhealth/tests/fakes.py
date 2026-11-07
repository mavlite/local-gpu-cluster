"""Shared test helper: a FakeRunner that maps argv tuples to text or exceptions."""
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeRunner:
    def __init__(self, responses: dict):
        self.responses = dict(responses)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv, timeout):
        key = tuple(argv)
        self.calls.append((key, timeout))
        if key not in self.responses:
            raise AssertionError(f"unexpected call: {key}")
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [c[0] for c in self.calls]
