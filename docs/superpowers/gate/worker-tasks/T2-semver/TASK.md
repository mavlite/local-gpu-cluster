# T2 — Semantic versions

Implement three functions in `semver.py`. Every rule below is tested; nothing else is.

## `parse(text)`
Returns `(major, minor, patch, prerelease)`.
- `major`, `minor` and `patch` are ints.
- `prerelease` is a tuple of identifiers. Purely numeric identifiers become `int`; all others stay
  `str`. It is empty if the version has no prerelease.

Format: `MAJOR.MINOR.PATCH`, then an optional `-PRERELEASE`, then an optional `+BUILD`.
- **Build metadata** is accepted and discarded.
- **Prerelease identifiers** are separated by `.`. Each is a non-empty run of `[0-9A-Za-z-]`.
- Raise `ValueError` for:
  - fewer or more than three numeric core parts;
  - a non-numeric core part;
  - an empty identifier;
  - a character outside `[0-9A-Za-z-.]` in the prerelease;
  - a **leading zero** in any numeric part. This applies to core parts *and* numeric prerelease
    identifiers, e.g. `01.2.3` and `1.2.3-01`. A lone `0` is fine.

## `compare(a, b)`
Takes two version strings. Returns `-1`, `0` or `1`, using SemVer 2.0.0 precedence:
1. Compare major, minor and patch numerically.
2. A version **without** a prerelease ranks **higher** than the same core with one.
3. Prerelease identifiers are compared left to right:
   - numeric identifiers compare numerically;
   - alphanumeric identifiers compare in ASCII order;
   - numeric identifiers rank lower than alphanumeric ones.
4. If all shared identifiers are equal, the version with **fewer** identifiers ranks lower.
5. Build metadata never affects precedence: `1.0.0+a` equals `1.0.0+b`.

## `bump(text, part)`
`part` is one of `"major"`, `"minor"`, `"patch"`; any other value raises `ValueError`.
- `major` returns `M+1.0.0`.
- `minor` returns `M.m+1.0`.
- `patch` returns `M.m.p+1`.
- The result **never** has a prerelease or build, even if the input had one. For example,
  `bump("1.2.3-rc.1", "patch") == "1.2.4"`.
