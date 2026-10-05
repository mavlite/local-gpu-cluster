"""Reference solution for T2 (never copied into a worker's workspace)."""
import re

_IDENT = re.compile(r"^[0-9A-Za-z-]+$")


def _num(s):
    if not s.isdigit():
        raise ValueError(f"not numeric: {s!r}")
    if len(s) > 1 and s[0] == "0":
        raise ValueError(f"leading zero: {s!r}")
    return int(s)


def parse(text):
    core, _, _build = text.partition("+")
    core, dash, pre = core.partition("-")
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError("need MAJOR.MINOR.PATCH")
    major, minor, patch = (_num(p) for p in parts)
    prerelease = ()
    if dash:
        idents = pre.split(".")
        out = []
        for ident in idents:
            if not ident or not _IDENT.match(ident):
                raise ValueError(f"bad prerelease identifier: {ident!r}")
            out.append(_num(ident) if ident.isdigit() else ident)
        prerelease = tuple(out)
    return major, minor, patch, prerelease


def _cmp(x, y):
    return (x > y) - (x < y)


def _cmp_ident(a, b):
    if isinstance(a, int) and isinstance(b, int):
        return _cmp(a, b)
    if isinstance(a, int):
        return -1
    if isinstance(b, int):
        return 1
    return _cmp(a, b)


def compare(a, b):
    pa, pb = parse(a), parse(b)
    c = _cmp(pa[:3], pb[:3])
    if c:
        return c
    ra, rb = pa[3], pb[3]
    if not ra or not rb:
        return _cmp(len(rb), len(ra)) if (ra or rb) else 0   # no prerelease ranks higher
    for x, y in zip(ra, rb):
        c = _cmp_ident(x, y)
        if c:
            return c
    return _cmp(len(ra), len(rb))


def bump(text, part):
    major, minor, patch, _ = parse(text)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown part: {part!r}")
