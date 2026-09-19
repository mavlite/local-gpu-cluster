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

> **CORRECTED 2026-09-19.** No longer true, and deliberately so.
> Treating a missing sidecar as a fresh start was a fail-open hole (see
> OBS-3 in the second follow-up): deleting both sidecars let a different
> bundle be written with `rc=0`. A missing sidecar beside an existing
> schema is now refused, so the first re-vendor of `9.1.1.0` **does**
> require `--accept-new-upstream` once.

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

> **CORRECTED 2026-09-19 (see the second follow-up below).** The 0.36 s
> figure is wrong — it holds only for flat, keyed and deep document
> shapes, and I did not test alias expansion. The true worst case was
> **15.82 s with a 306.8 MB response**. The residual decision below was
> reasoned from the wrong number and is re-rated in the follow-up.

Two things stated honestly rather than glossed:

- `maxLength` counts characters, not bytes. `documents.MAX_BYTES` remains
  the byte-side backstop underneath, so a multibyte-heavy document is
  still bounded.
- **Not done:** wrapping `call_handler` in `asyncio.to_thread`. It is the
  right third layer, but `mcp` is not installed in this environment so
  `build_server()` cannot be exercised, and shipping an unverified change
  to the transport on top of a 50x improvement is the wrong trade.
  Recommended as a follow-up once `mcp` is installed.
  **Re-rated in the second follow-up below, against the corrected
  figure.**

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
   backfillable without a network fetch of the upstream document. Its
   absence is now refused rather than tolerated, so the next re-vendor
   needs `--accept-new-upstream` once to record it.

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

---

# Second follow-up — re-review observations (OBS-1 … OBS-4)

- **Date:** 2026-09-19 (same day, after re-review APPROVED)
- **Tests:** 381 → **392 passed** (+11)

Re-review approved all nine findings and confirmed 20 evasion vectors
against the closed-set gate return one byte-identical envelope with zero
filesystem calls. Four observations came back. Two were documentation
corrections; two were fixes. One of the corrections changed a conclusion,
which is the important part of this round.

## OBS-4 — my worst-case figure was wrong, and I had budgeted against it

**I reported 0.36 s / 10.2 MB as the worst legal `vcf_diff_spec` call.
That figure only holds for flat, keyed and deep document shapes. I never
tested alias expansion.**

Reproduced, and then pushed further than the re-review did:

| Input | Legal? | Cost (pre-fix) |
|---|---|---|
| 254,349-char flat (my original) | yes | 0.36 s, 10.2 MB peak |
| 1,673-char alias chain | yes | 4.78 s, 49.9 MB response |
| 2,873-char alias chain | yes | 6.12 s, 86.6 MB response |
| **10,073-char alias chain** | **yes** | **15.82 s, 306.8 MB response, 323.8 MB peak** |

All are inside every input bound: 30 aliases (limit 100), 196,590 nodes
(limit 200,000), well under `maxLength`. The amplification is **30,461x**
and scales linearly with JSON-pointer length, which is bounded only by
`MAX_DOCUMENT_CHARS`.

**Root cause: `MAX_NODES` bounds how far a document expands; nothing
bounded how much the diff emits about it.** Those are different numbers,
and I had conflated them.

**Fix.** `vcf_diff_spec`'s output is now bounded by two limits, because
neither subsumes the other — many small changes is a different shape from
few changes with enormous pointers, and alias expansion produces the
second:

- `MAX_CHANGES = 10_000`
- `MAX_CHANGE_CHARS = 1_000_000`

On truncation the tool returns `truncated: true`, `valid: false` and a
blocking `VCF-DIFF-TRUNCATED` finding. **Blocking is deliberate**: a
partial diff reported as valid is the worst answer available, because an
agent asking "did anything under `/credentials` change?" would read an
early-stopped walk as "no". The changes found are still returned — they
are correct as far as they go — but the caller is told twice that the
comparison is incomplete.

| | before | after |
|---|---|---|
| 10,073-char alias chain | 15.82 s, 306.8 MB | **0.37 s, 1.0 MB** |
| 254,349-char flat | 0.36 s | **0.13 s**, not truncated |
| real 3-host inventory, 1 field changed | 1 change | **1 change**, not truncated |

**Mutation results:** budget never truncates → **2 failures** (and the
suite takes 41 s instead of 2.6 s, which is itself the finding); bounding
count but not size → **1**; reporting a truncated diff as valid → **1**.

### Re-rating the `asyncio.to_thread` residual

**Re-rated: warranted, and now the top residual.** I deferred it on the
strength of 0.36 s. The real number was 15.82 s — a 44x error, and 15.82 s
of synchronous stall on a long-lived single-process server is not
something to wave through on "it is only 0.36 s".

Two things follow, and the order matters:

1. **The output bound above is the actual fix, and it is done.**
   `to_thread` would not have helped here: moving a 306.8 MB response off
   the event loop still builds a 306.8 MB response and still returns it.
   Bounding the output takes the worst case to 0.37 s — below the figure I
   originally, wrongly, claimed. Had I shipped `to_thread` on the strength
   of the corrected number without bounding the output, I would have
   hidden the symptom and kept the amplification.
2. **`to_thread` remains recommended, now as defence in depth rather than
   as the fix.** With the output bounded, the worst measured call is
   0.37 s; that is a real stall on a shared loop but not a denial of
   service, and it is no longer load-bearing for anything. It still cannot
   be verified here — `mcp` is not installed, so `build_server()` cannot
   be exercised — so it stays deferred, but it is now the first thing to
   do once `mcp` is installed, not an optional nicety.

The honest summary: the deferral was reasoned from a number I had not
tested widely enough, and the correct response was neither "defer harder"
nor "ship `to_thread`" but "bound the output, which neither figure had
prompted anyone to do".

## OBS-3 — the vendor guard failed open on a missing sidecar

Reproduced: delete both sidecars, and a bundle containing a planted
`backdoor` property was written with `rc=0`. `_refuse_on_change` treated
"no digest recorded" as "first run" unconditionally, so absence of
evidence became evidence of absence.

**Fix.** A schema already on disk is what distinguishes an anomaly from a
genuine first vendor. If `sddc-spec.schema.json` exists and either digest
sidecar is absent, the script refuses and says the sidecar should have
been committed alongside the schema. A true first vendor — no schema, no
digests — is still a clean `rc=0`.

Knock-on, documented rather than special-cased: the vendored `9.1.1.0` has
no `sddc-spec.source.sha256`, so its first re-vendor now requires
`--accept-new-upstream` once. Carving out "this particular sidecar may be
absent" would reintroduce exactly the fail-open path the guard closes. The
earlier statement in this report has been corrected in place.

**Mutation:** restoring the fail-open behaviour → **2 failures**. A
parametrized test also removes each sidecar individually, so deleting only
the one that would have caught you is not enough.

## OBS-1 — `schema.py`'s docstring overstated the guarantee

The docstring claimed set membership constrains symlinks and junctions. It
does not. Verified: `mklink /J` (no privilege required) inside
`vcfspec/schemas/` is followed by `_discover()`, appears in
`known_versions()`, and its planted schema+sidecar certified junk as
`valid: True`.

**Corrected to state the real boundary:** the gate constrains the **name**,
not what the name points at. The trust boundary it defends is the
caller-supplied string — an MCP `vcf_version` or a CLI `--version` — and
against that it is complete: no such string can name anything outside
`SCHEMA_DIR`. Integrity of the package directory itself is the installer's
and the filesystem's job. The docstring now says so, including why
hardening against it here would be pointless: anyone who can plant a
junction inside the package can overwrite the schema and its sidecar
directly, which defeats the checksum far more simply.

## OBS-2 — the no-reflection rule is argument-scoped

Correct, and now stated in the README under its own heading.

- **Tool arguments are never echoed** — `vcf_version`, `code`,
  `input_kind`. Each rejection names what the package ships instead.
- **Document bodies are reported, necessarily** — values and key names
  appear in finding messages and JSON pointers, because saying what is
  wrong with your document is the tool's job. The largest instance is the
  new `maxItems: 64` rejection, where jsonschema serialises the whole
  `hosts` array: **10,347 characters at 65 hosts**, measured.
- **Credentials inside that output are masked.** Verified by planting a
  literal credential inside an over-length `hosts` array: it came back
  `***REDACTED***` and the literal appears nowhere in the envelope.

The distinction to hold onto: arguments are not reflected; document
content is reported, and redacted on the way out.

## Corrections applied to the earlier sections of this report

1. The "worst legal `vcf_diff_spec` = 0.36 s / 10.2 MB" claim, annotated
   in place with the corrected 15.82 s / 306.8 MB figure.
2. The `to_thread` residual, cross-referenced to the re-rating above.
3. The claim that `9.1.1.0`'s first re-vendor "records one without needing
   the flag" — no longer true after OBS-3, corrected in place.
4. Residual item 4, same correction.

A wrong number in a security document is worse than no number, because the
next person budgets against it. In this case the next person was me.
