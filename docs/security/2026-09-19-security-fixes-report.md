# Security fixes — `vcf-spec-tools`

- **Date:** 2026-09-19
- **Reviewed document:** `docs/security/2026-09-19-vcf-spec-tools-security-review.md`
- **Branch:** `fix/vcf-spec-tools-security`, cut from `main` at `585f2d9`
- **Baseline:** 312 passed → **369 passed in 2.00s** (+57 tests, none removed)
- **Status:** all nine findings addressed. Two carry a deliberate decision
  to change nothing in code; both are stated below rather than left silent.

Every finding was reproduced before being fixed, and every guard added was
mutation-tested — the protected behaviour was broken and the suite
confirmed red. This package has a documented history of guards that were
correct and protected nothing; the mutation results are recorded per
finding so that claim is checkable rather than asserted.

## Commits

| SHA | Finding | Summary |
|---|---|---|
| `a0a2841` | 1, 2, 6 | Resolve `vcf_version` against a closed set before any path is built |
| `6f30008` | 3 | Raise the `mcp` floor above five advisories; bound every dependency |
| `47c4ec2` | 4 | Refuse to silently re-vendor a changed schema |
| `5f3b2f6` | 5 | Parse with libyaml; bound the MCP surface |
| `167ebc5` | 7 | Cap inventory hosts at 64 |
| `6082e3e` | 8 | Untrack build artefacts and ignore them |
| `1005aa8` | 9 + docs | Document the version gate, the integrity code and the vendor flag |

---

## Findings 1 and 2 (HIGH) — `vcf_version` as a path segment

**Reproduced first.** The review's own script, run verbatim against
pre-fix code:

```
HIGH-1 version arg: ../../../../../../AppData/Local/Temp/tmpjyn58sst/evil
HIGH-1 result: valid=True findings=[]           <- junk spec certified
HIGH-2 EXISTS  ['VCF-LIC-EVALUATION', 'VCF-RENDER-FAILED']
HIGH-2 ABSENT  ['VCF-LIC-EVALUATION', 'VCF-MCP-BAD-ARGS']
absolute: C:\Windows\Temp\sddc-spec.schema.json
nul byte: '...\schemas\9.1.1.0\x00\sddc-spec.schema.json'
```

**Fix — the root cause, not the symptoms.** `schema.resolve_version()` is
now the single gate. It membership-tests the requested version against
the set of versions *actually vendored*, discovered by listing
`SCHEMA_DIR` (a directory only counts when it really holds
`sddc-spec.schema.json`, which is also what keeps the sibling
`schemas/inventory/` tree out of the set). It raises **before any path is
constructed**, so there is no path whose existence could be probed.

Applied at both sites the review identified: `schema.schema_path()` and
`render._defaults_text()`. Both `lru_cache`s are now keyed on a *resolved*
version, so caller text is never a cache key either.

The format regex is applied to the directory listing as an additional
filter. It is **not** the gate, and a test (`"9.1.1.1"` — perfectly
well-formed, not vendored, still refused) pins that distinction.

**Uniformity.** The requested version is no longer quoted back, in either
`VCF-SCHEMA-VERSION-UNKNOWN` or the MCP bad-args translation; the vendored
set is named instead. Verified post-fix:

```
HIGH-1: CLOSED ['VCF-MCP-BAD-ARGS']
HIGH-2: all five envelopes identical: True
```

The five vectors — traversal, absolute path, NUL byte, URL-encoded
traversal, plain unknown-but-well-formed — produce a byte-identical
envelope. A second test asserts the *same* rejected version answers
identically whether or not the implied path exists, which is the oracle's
actual defining property.

Beyond the envelope, a spy on `pathlib` asserts that no fragment of a
hostile version reaches `read_text`, `exists`, `is_file`, `is_dir` or
`iterdir` at all — so a future refactor that refuses *after* opening the
attacker's file reddens rather than passing.

**Two existing assertions were deliberately inverted.**
`test_mcp_server.py` asserted `"9.9.9.9" in bad_args["message"]`; it now
asserts the opposite. Reflecting an arbitrary caller string into an
envelope an AI agent reads is a surface worth not having, and a message
that varies with the input cannot be uniform. The reasoning is recorded at
both call sites.

**Mutation results:** `resolve_version` as a pass-through → **12 failures**;
regex-only with no set membership → **3**; re-echoing the version → **1**.

## Finding 3 (MEDIUM) — dependency bounds

`mcp>=1.28.1,<2` (was `>=1.2,<2`), clearing PYSEC-2026-1616/1617/1618/
3482/3483. `pyyaml>=6.0.2,<7`, `jsonschema>=4.21,<5`, `pytest>=8.0,<10`,
`setuptools>=68,<84`.

**Verified with the constraints as written:** `pip install --dry-run`
resolves the full set (picks `mcp 1.30.0`); an isolated `pip wheel` builds
the package against `setuptools>=68,<84`; the suite passes.

`tests/test_dependencies.py` reads `pyproject.toml` and asserts the
policy, so it holds offline and does not depend on what a resolve happens
to pick today. **Mutation:** restoring the four unbounded constraints and
the old `mcp` floor → **5 failures**.

## Finding 4 (MEDIUM) — vendor script provenance

**Decision.** Two digests are recorded and both are checked before any
write: `sddc-spec.source.sha256` (the fetched upstream document — the
provenance record, which never existed before) and
`sddc-spec.schema.json.sha256` (the extracted bundle). If either would
change, the script prints both digests `old -> new` and exits 1. The check
runs before the `mkdir`, so a refused run leaves the filesystem exactly as
it found it.

A changed upstream whose extracted bundle is byte-identical is **still**
refused. The extraction is lossy — it drops `example`, `discriminator`,
`xml` and `externalDocs` — so "the part we vendor is unchanged" is a
weaker claim than "upstream is unchanged", and only a person can decide
the difference does not matter.

**What an operator must now do to legitimately update a vendored schema:**

1. Run `python scripts/vendor_schema.py <version> [source]` as before. It
   prints both digests as `old -> new` and exits non-zero if either
   changed, writing nothing.
2. Review the change — diff the upstream OpenAPI document, not the
   1,685-line generated JSON.
3. Re-run with `--accept-new-upstream` and quote both printed digests in
   the commit message, so the bump is reviewable from the commit rather
   than from the JSON diff.

An unchanged re-vendor stays a no-op and needs no flag. The
already-vendored `9.1.1.0` has no source digest recorded (it predates
this); the first re-vendor records one without needing the flag, which is
the graceful path rather than a one-off migration.

**Mutation:** making the guard always allow → **3 failures**; checking only
the bundle digest and dropping the upstream half → **1**.

## Finding 5 (MEDIUM) — parse cost

**Before / after**, measured on the same 1.80 MB document, two-document
diff, this machine:

| | wall clock | peak memory |
|---|---|---|
| before (`SafeLoader`, pure Python) | **17.88 s** | 101.8 MB |
| after (`CSafeLoader`, libyaml) | **2.63 s** | 73.8 MB |

`load_document` now uses `yaml.CSafeLoader` when the install has it,
falling back to `yaml.SafeLoader` when it does not. `yaml.safe_load(text)`
*is* `yaml.load(text, SafeLoader)`, so selecting between the two safe
loaders keeps `safe_load` semantics exactly. Parity verified on aliases,
duplicate keys, implicit type resolution (dates, octals, `yes`) and
`!!python` tag rejection, and pinned by tests.

**`MAX_BYTES` decision.** Left at 2 MB for the library and the CLI — an
operator reading their own file under their own account spends only their
own patience. **Lowered for the MCP server surface only**, where the caller
is an agent and the process is shared: the MCP `document`/`left`/`right`
arguments carry `maxLength: 262144` (256 K characters), enforced by the
existing input-schema validator *before any handler body runs*.
`vcf_version` gets 32 and `code` 128 for the same reason. A real 3-host
inventory is ~1.5 KB, so that is ~170x headroom.

Combined effect at the server surface: the worst legal `vcf_diff_spec`
call is now **0.36 s and 10.2 MB**, down from ~18 s and ~102 MB.

Two things stated honestly rather than glossed:

- `maxLength` counts characters, not bytes. `documents.MAX_BYTES` remains
  the byte-side backstop underneath, so a multibyte-heavy document is
  still bounded.
- **Not done:** wrapping `call_handler` in `asyncio.to_thread`. It is the
  right third layer, but `mcp` is not installed in this environment so
  `build_server()` cannot be exercised, and shipping an unverified change
  to the transport on top of a 50x improvement is the wrong trade.
  Recommended as a follow-up once `mcp` is installed.

I also drafted a `MemoryError` handler on the theory that libyaml reports
its nesting limit that way, then **removed it after measuring**: this build
raises `RecursionError` ("Stack overflow"), the same type the pure-Python
loader raises, verified at 10k and 100k levels of nesting. The existing
single handler covers both. An untestable guard for a condition that does
not occur is exactly the pattern this codebase has been bitten by.

ReDoS was measured as negative across all five regexes in the review and
was not revisited.

## Finding 6 (LOW) — tampered schema misreported

`SchemaIntegrityError` no longer shares an `except` clause with
`FileNotFoundError` at any of the three sites in `api.py`. It gets
`VCF-SCHEMA-INTEGRITY`, severity **critical**, whose message tells the
operator the installation's integrity check failed and whose fix says *do
not retry* and treat the installation as compromised until proven
otherwise. `_reclassify_unknown_version` deliberately does not translate
it, so it cannot become `VCF-MCP-BAD-ARGS` again.

The code is in `catalogue.yaml`, and the existing coverage test
(`test_every_code_the_package_can_emit_resolves_in_the_catalogue`) does
force it, as expected.

**Mutation:** folding the integrity error back into the unknown-version
finding → **3 failures**.

## Finding 7 (LOW) — unbounded probe wall-clock

**Decision: `maxItems: 64`.** 64 is VMware's own supported maximum for a
vSAN cluster, and this inventory describes a single VCF management domain,
so no lab this tool targets can approach it — the documented one is 3
hosts. It caps the worst case at 64 × 3 × 2.0 s ≈ **6.4 minutes**, down
from ~33 hours. Still slow, but bounded, operator-initiated and
Ctrl-C-able.

A tighter bound would risk rejecting legitimate work, and a guard that
blocks real work gets deleted rather than fixed — so a test also asserts a
full 64-host inventory still validates cleanly.

**Not done:** the global `deadline` in `ProbeConfig` the review also
suggests. `maxItems` bounds the input, which is the part an attacker
controls; a deadline bounds the run, which the operator already controls
with Ctrl-C. Recorded as optional, not required.

**Mutation:** removing `maxItems` → **2 failures**.

## Finding 8 (INFO) — build artefacts

`git rm -r --cached vcf-spec-tools/vcfspec.egg-info` — removed from
tracking, still on disk, so nobody's working install breaks. `.gitignore`
gained `*.egg-info/`, `build/`, `dist/`, `.venv/` and `.pytest_cache/`
(which self-ignored via its own generated `.gitignore` — that works, but
incidentally, so it is now stated).

## Finding 9 (INFO) — CLI `OSError` string

**Decision: keep it, and record why.** No code change beyond a comment.

`cli.py`'s `cannot read {path}: {exc}` prints an errno message plus the
path the operator themselves typed. The file was never successfully
opened, so no document content and no credential can reach it — the review
reached the same conclusion independently. Trimming it to a class name
would make "permission denied", "is a special file" and "name too long"
indistinguishable, costing a real operator a real diagnostic to buy
nothing.

A CLI telling an operator which of *their* paths failed, and why, is
legitimate. The reasoning now lives at the call site, with an explicit
contrast against `main()`'s last-resort handler — which prints only
`type(exc).__name__` precisely because spec content *can* be in play
there — so the difference between the two is visibly deliberate and does
not get "fixed" into inconsistency later.

---

## Constraints honoured

- Credential values remain `${reference}` strings only; no real secret is
  emitted anywhere. Untouched.
- Redaction stays at the single `api.py` boundary. Untouched.
- `yaml.safe_load` semantics only — the loader swap is between the two
  *safe* loaders, and a test asserts the active loader is never
  `Loader`, `UnsafeLoader`, `FullLoader` or `CLoader`.
- No input mutation.
- All files under 800 lines (largest touched: `tests/test_api.py` at 667,
  `README.md` at 512, `vcfspec/mcp_server.py` at 494).
- No `Co-Authored-By` trailer on any commit; verified across all seven.

## Residual items

Neither blocks the branch; both are recorded so they are not lost.

1. **`asyncio.to_thread` around `call_handler`** — finding 5's third
   layer, deferred because `mcp` is not installed here and the change
   cannot be verified. Worth doing once it is.
2. **`pip-audit` in the `dev` extra plus a CI step** — the review's own
   advice that "a floor with no audit gate drifts again". The floors are
   now correct and `tests/test_dependencies.py` stops them silently
   regressing, but nothing yet re-checks them against *new* advisories.
3. **A `deadline` in `ProbeConfig`** — optional, per finding 7 above.
4. **`sddc-spec.source.sha256` for the vendored `9.1.1.0`** — not
   backfillable without a network fetch of the upstream document. The
   script handles its absence gracefully and records it on the next
   re-vendor.

## Disagreements with the review

One, and it is a matter of emphasis rather than of fact.

The review's suggested fix for findings 1/2 leads with a format regex and
offers the closed-set membership test as "better still". I inverted that:
the membership test is the gate and the regex is a filter on the directory
listing. A regex that accepts `9.1.1.0` still accepts whatever a symlink,
a junction or a future reshuffle of `SCHEMA_DIR` happens to put at that
name, and it cannot answer the question that actually matters — "is this a
schema this package vendored?" — because the only source of truth for that
is the listing. The review's own `is_relative_to` variant is likewise
weaker than it looks: by the time it runs, the caller's path has already
been built and resolved. Stated here because a future reader comparing the
two documents will otherwise see the regex in the review, find it demoted
in the code, and reasonably wonder which was intended.

---

# Follow-up — caller-text reflection, applied consistently

- **Date:** 2026-09-19 (same day, after coordinator verification)
- **Commit:** see below
- **Tests:** 369 → **381 passed** (+12)

The coordinator independently verified the HIGH fix (all five hostile
vectors on the version-consulted path return one byte-identical envelope,
no echo — oracle closed) and then found that my own stated principle had
not been applied everywhere. They were right, and the gap is instructive:
I removed the reflection from the two *rejection* messages, which is where
the oracle lived, and stopped there — treating it as an oracle fix rather
than as the general rule I had written down.

## What was still reflecting

Three sites, found by sweeping every tool argument rather than by
inspecting the two I had already touched:

| Site | Bound | Reflected? |
|---|---|---|
| `VCF-VERSION-NOT-CONSULTED` (inventory path) | 32 chars | **yes** — `vcf_version '../../../../../../Windows' was supplied but not consulted: ...` |
| `vcf_explain_finding`, `code` | 128 chars | **yes** — `No rule with code 'IGNORE-PREVIOUS-INSTRUCTIONS-XYZ'.` |
| `input_kind`, MCP boundary | closed enum | no — refused by the enum, message is a class name only |
| `input_kind`, library `detect_kind` | unbounded | **yes** — `Unknown input_kind 'NOT-A-KIND-LIB'.` |

**Severity: LOW, as the coordinator scoped it.** None is an oracle — no
filesystem access occurs on any of these paths and each answers
identically regardless of what is on disk — and all are length-bounded at
the MCP surface. The concern is only that the caller is an AI agent which
may be validating a document from an untrusted source, and handing it back
caller-chosen characters inside a message it reads is a surface with no
upside.

## Fix

All three now state what happened and name what the package actually
ships, without the echo:

- `VCF-VERSION-NOT-CONSULTED` → *"A non-default vcf_version was supplied
  but not consulted: this document validated as a lab inventory, which has
  one fixed schema regardless of VCF release. Vendored: 9.1.1.0."* Still
  `info`, still does not reject the call — fix A's promise is intact.
- `vcf_explain_finding` → the catalogue's own `VCF-EXPLAIN-UNKNOWN-CODE`
  summary, *"No rule with that code."*, which was already echo-free; only
  the hand-built f-string override was reflecting.
- `detect_kind` → *"Unknown input_kind. Valid values are 'inventory' and
  'sddc_spec'."*

The `input_kind` case is unreachable from any caller-facing surface today
(the MCP enum and argparse `choices=` both refuse it first), and it was
fixed anyway: `validate_document()` is a public library entry point, and
the rule should not depend on which front door happens to be in front of
it.

## Two more assertions inverted

`test_api.py` required `"0.0.0" in note["message"]` and
`test_mcp_server.py` required `"NOPE" in out["summary"]`. Both now assert
the opposite, with the reason recorded at the call site — the same
treatment the first two inversions got.

## The regression test is a sweep, not three cases

`test_no_finding_message_ever_reflects_caller_supplied_text` enumerates
every string argument of every tool from `TOOLS` itself, sends a canary
through each, and asserts the canary never appears in the returned
envelope. Nine arguments across four tools are covered today, and a *new*
tool argument that echoes is caught by a test nobody has to remember to
write — which is the failure mode that produced this follow-up in the
first place. A companion test guards the sweep against silently covering
nothing.

**Mutation results:** restoring the version echo → **3 failures**;
restoring the `code` echo → **2**; restoring the `input_kind` echo → **1**.

## Answer on `code` and `input_kind`

Both were checked, as asked. `code` **did** echo and is fixed.
`input_kind` does **not** echo at the MCP boundary — the closed enum
rejects it in `call_handler` before any handler body runs, and the
resulting message carries only `ValidationError` — but it **did** echo one
layer down in `detect_kind`, reachable through the library API, and that
is fixed too.
