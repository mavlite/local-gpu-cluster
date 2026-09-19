# Security re-review — `fix/vcf-spec-tools-security`

- **Date:** 2026-09-19
- **Scope:** fixes only. `585f2d9..a77de3d`, 9 commits, package `vcf-spec-tools/`.
  The underlying feature was not re-reviewed.
- **Inputs:** `2026-09-19-vcf-spec-tools-security-review.md`,
  `2026-09-19-security-fixes-report.md`, `review-585f2d9..a77de3d.diff`
- **Suite:** `cd vcf-spec-tools && python -m pytest -q` → **381 passed in 2.00s**
- **Verdict:** **APPROVED.** All nine findings addressed. Three new
  observations, none blocking; one is a correction to a claim in the fix
  report rather than a defect in the code.

Every reproduction below was executed. Scratch scripts lived in the session
scratchpad. Nothing in the repository was modified; `git status --short`
before and after is `?? .playwright-mcp/` only.

---

## 1. Priority 1 — the HIGH pair (`vcf_version` as a path segment)

### 1.1 The review's traversal reproduction — dead

`scratchpad/p1_traversal.py`, the review's own script verbatim: attacker
`sddc-spec.schema.json` + matching `.sha256` sidecar in a fresh temp
directory, `vcf_version` a relative path to it.

```
version arg: ../../../../../../AppData/Local/Temp/tmpb9zsssi4/evil
result valid: False codes: ['VCF-MCP-BAD-ARGS']
attacker path leaked into envelope?: False
```

Pre-fix this returned `valid: true` with zero findings. It now returns a
rejection envelope containing no fragment of the attacker's path.

That long vector is also over `maxLength: 32`, so it would be refused by
the argument bound alone. To prove the **gate** is what refuses it, I
re-ran with the hostile directory placed at `vcfspec/evil` and
`vcf_version: "../evil"` — 7 characters, well inside the bound
(`p1_gate.py` section A):

```
[A] short traversal '../evil' valid=False ['VCF-MCP-BAD-ARGS']
    message: The tool 'vcf_validate_spec' was called with invalid arguments
             (requested VCF version is not vendored; vendored: 9.1.1.0).
```

The closed-set gate, not the length bound, is doing the work. Library-level
`schema_path()` refuses `../evil`, `C:/Windows/Temp`, `/etc`,
`9.1.1.0\x00` and `../9.1.1.0` with `UnknownSchemaVersion`.

### 1.2 Attempts to defeat the closed set — 20 vectors, one envelope

`p1_gate.py` section B, all within `maxLength: 32`: trailing/leading space,
trailing dot, trailing newline, NUL, zero-width space, trailing slash,
`./` prefix, `9.1.1.0/../9.1.1.0` (normalises back to the real one),
prefix (`9.1.1`) and suffix (`9.1.1.0.1`, `x9.1.1.0`) of a vendored name,
fullwidth-digit `９.1.1.0` (NFKC-folds to `9.1.1.0`), NFKD form,
`9.1.1.0\x00evil`, `../evil`, `..\evil`, `%2e%2e/evil`, `inventory`
(a real directory in `SCHEMA_DIR` that is not a schema dir), and plain
unknown `9.9.9.9` / `9.1.1.1`.

```
[B] envelopes byte-identical to the plain-unknown control: all 20 vectors
[B] distinct envelopes across all vectors: 2   (the 20 rejections + the real 9.1.1.0)
```

No normalisation, folding, trimming or separator handling gets through:
the gate is an exact `in frozenset` on an unmodified string, so nothing is
canonicalised into a match. `inventory` is correctly excluded because
`_discover()` requires the directory to actually contain
`sddc-spec.schema.json`.

### 1.3 Gate runs before any filesystem access — confirmed

`p1_gate.py` section D spies on `builtins.open`, `Path.exists`,
`Path.is_file`, `Path.is_dir`, `Path.iterdir` and `Path.read_text`, warms
the legitimate directory listing, clears the counter, then drives
`schema_path`, `load_schema` and `render.load_defaults` with `../evil`,
`9.9.9.9`, `..\evil` and `inventory`:

```
[D] FS calls during rejected lookups: 0
[D] calls mentioning hostile fragments: []
```

Zero. The oracle has no path to probe.

### 1.4 Sidecar provenance — confirmed

`schema._load_verified()` reads the sidecar from `path.parent`, where
`path = SCHEMA_DIR / resolve_version(version) / SCHEMA_FILENAME`. Since
`resolve_version` returns only a member of the on-disk vendored set, the
parent is always a directory inside the installed package. The sidecar can
no longer be caller-influenced.

### 1.5 Symlink / junction — the one residual, and why it is not a finding

`symlink_to` inside `SCHEMA_DIR` fails on this host without
SeCreateSymbolicLinkPrivilege. A **directory junction**, which needs no
privilege, does succeed (`p1_junction.py`):

```
mklink /J schemas\9.9.9.9 -> <temp>\evil   ->  0
known_versions with junction: ['9.1.1.0', '9.9.9.9']
junction result valid= True []
```

So `_discover()` does follow a junction planted inside `SCHEMA_DIR`. This
is **not an escalation**: it requires write access to the package's own
`vcfspec/schemas/` directory, at which point the attacker can simply
overwrite `9.1.1.0/sddc-spec.schema.json` and its sidecar directly, which
was always possible and is the trust assumption the checksum exists under.
Junction removed; `known_versions()` back to `['9.1.1.0']`.

It does make one sentence in `schema.py`'s docstring overstated — "a regex
alone would still permit `9.1.1.0` to name whatever a symlink, junction or
a future `SCHEMA_DIR` reshuffle put there; membership of a listing of real
vendored directories is what actually constrains it." Membership does not
constrain that case either; what constrains it is that `SCHEMA_DIR` is
package-owned. Recorded as **OBS-1** below.

**Findings 1 and 2: ADDRESSED.**

---

## 2. Priority 2 — echo/reflection sweep

### 2.1 The sweep is real

`p2_sweep.py` enumerates `TOOLS` the same way the test does:

```
sweep-covered string args: 9      tools covered: 4 of 5
  vcf_render_spec.document / .vcf_version / .input_kind
  vcf_validate_spec.document / .vcf_version / .input_kind
  vcf_explain_finding.code
  vcf_diff_spec.left / .right
```

Nine arguments, four tools — exactly as claimed. The fifth tool
(`vcf_spec_schema`) declares no properties, so there is nothing to sweep.
No string argument is omitted. The canary is 27 characters, inside every
`maxLength`, so it genuinely reaches the handler rather than being refused
at the boundary.

**Guard-the-guard, mutation-tested.** Making `_sweep_arguments()` return
immediately:

```
FAILED test_the_sweep_itself_covers_every_tool_and_the_known_offenders
1 failed, 32 passed
```

A vacuous sweep reddens. Test file restored from backup; `git status` clean.

### 2.2 Independent hunt (`p2_hunt.py`)

Canaries sent as nested document values, as document *keys*, in `sddcId`,
under `vcenterSpec`, as a credential, and in malformed YAML, through both
MCP tools, the diff tool, and the CLI with stdout and stderr captured
separately.

- **Nothing new at the MCP layer for tool arguments.** The three fixed
  sites stay fixed.
- **Document content is reflected, by design and unchanged by this branch.**
  A hostname value appears in `VCF-NAME-NOT-LOWERCASE` messages; an
  unexpected key name appears in jsonschema's
  `Additional properties are not allowed ('X' was unexpected)`; a changed
  key appears as a `/X` pointer in `vcf_diff_spec` changes. This is
  inherent — a validator cannot report a finding without naming the field.
  The sweep does not cover it and does not claim to. The branch's stated
  rule ("never reflect caller text into a message an agent reads") is
  therefore achieved for *arguments*, not for *document bodies*. Worth
  stating because the fix report words it unqualified. **OBS-2.**
- **stderr:** the only echo is `cannot read <path>: no such file` — the
  operator's own typed path, INFO-9, deliberately kept. No document content
  reached stderr on any case.
- **CLI stdout** carries the same envelope as MCP, already through
  `redact()`.

### 2.3 Regression mutations

| Mutation | Result |
|---|---|
| re-echo version in `_reclassify_unknown_version` | **7 failed** (incl. 4 uniformity params) |
| re-echo version in `VCF-VERSION-NOT-CONSULTED` | **3 failed** |
| re-echo `code` in `tool_explain_finding` | **2 failed** |

All restored with `git checkout --`.

**Reflection follow-up: ADDRESSED.**

---

## 3. Priority 3 — the four inverted assertions

Each was mutation-tested by restoring the pre-fix behaviour and checking
the inverted assertion is what goes red.

| # | Was | Now | Regression caught? | Weaker? |
|---|---|---|---|---|
| 1 | `"9.9.9.9" in bad_args["message"]` (validate) | `not in` **+ `"9.1.1.0" in`** | yes — M1, `test_unvendored_vcf_version_..._for_validate` | **No — stronger** |
| 2 | same, render | `not in` **+ `"9.1.1.0" in`** | yes — M1, render twin | **No — stronger** |
| 3 | `"0.0.0" in note["message"]` | `not in` **+ `DEFAULT_VERSION in` + `"not consulted" in`** | yes — M2, `test_irrelevant_version_..._note` | **No — stronger** |
| 4 | `"NOPE" in out["summary"]` | `not in` **+ `assert out["summary"]`** | yes — M3, `test_explain_unknown_code_..._not_an_exception` | **Marginally less specific, not weaker in effect** |

**Verdict on the four: none is weaker than what it replaced, and all four
were necessary rather than convenient.** Inversions 1–3 each replaced one
assertion with two or three, and each now pins a positive fact (the
vendored set is named; the note still says a version was ignored) that the
old assertion did not check at all. Every one of the four fails if the
behaviour regresses — verified, not assumed.

Inversion 4 is the only one to note: `assert out["summary"]` is a
truthiness check where the old line checked content. It still catches the
regression, because the sibling assertion `out["code"] ==
"VCF-EXPLAIN-UNKNOWN-CODE"` and the parametrised sweep case
`vcf_explain_finding.code` both fire (M3 → 2 failures). The specificity
lost is real but is covered elsewhere; it does not warrant a change.

The inversions were necessary: each old assertion asserted *exactly the
behaviour the finding identified as the defect*. Keeping them was not an
option, and each carries its reasoning at the call site.

---

## 4. Priority 4 — the remaining findings

**Finding 3 — dependency bounds: ADDRESSED.** `pyproject.toml` reads
`mcp>=1.28.1,<2`, `pyyaml>=6.0.2,<7`, `jsonschema>=4.21,<5`,
`pytest>=8.0,<10`, `setuptools>=68,<84`. `pip install --dry-run
".[mcp,dev]"` resolves the full graph (picks `mcp 1.30.0`, `pytest 9.1.1`)
and `--no-deps` builds the package metadata. Mutation (restoring
`mcp>=1.2` and unbounded `pyyaml`) → **2 failures** in
`test_dependencies.py`.

**Finding 4 — vendor script: ADDRESSED.** `p4_vendor.py` drives
`vendor_schema.main()` against a local source file with `OUT_DIR`
redirected to a temp dir:

| Case | Result |
|---|---|
| first vendor, nothing on disk | writes, rc=0, records both digests |
| identical re-vendor | rc=0, no-op, disk unchanged |
| **upstream changed, extracted bundle byte-identical** | **rc=1, refused**, disk unchanged |
| same, `--accept-new-upstream` | rc=0, writes |
| bundle changed | rc=1, refused, disk unchanged |
| bundle changed, with flag | rc=0, writes |

The refusal prints both digests `old -> new  CHANGED` and happens before
`mkdir`, so a refused run leaves the filesystem untouched — verified by
comparing file contents before and after. I could not make it overwrite
silently in any case where a digest sidecar exists.

One caveat found, **OBS-3**: `_recorded()` returns `None` both for "first
vendor" and for "the schema file is present but its `.sha256` sidecar was
deleted". In the second case the guard fails open and overwrites without
the flag (case [g]: both sidecars removed, a different bundle written,
rc=0). This needs local write access to the package's schemas directory —
the same trust level as editing the script — so it is informational, not a
finding. It is the same intentional "graceful path" that lets the
already-vendored `9.1.1.0`, which has no `sddc-spec.source.sha256`, record
one on its next re-vendor.

**Finding 5 — libyaml loader: ADDRESSED, semantics preserved.**
`documents._SAFE_LOADER is yaml.CSafeLoader` → True. `grep` for
`yaml.load(`, `UnsafeLoader`, `FullLoader`, `yaml.Loader`, `CLoader` over
`vcfspec/` → the only `yaml.load` is `documents._safe_load`, with
`Loader=_SAFE_LOADER`. `!!python/object/apply:os.system`,
`!!python/name:os.system` and the nested form are all refused with
`ConstructorError`. Parity against `yaml.safe_load` on `yes`, `0o17`,
`2020-01-01`, `010`, `1_000` → identical results.

**Finding 6 — `VCF-SCHEMA-INTEGRITY`: ADDRESSED.** `p4_tamper.py` copies
`schemas/` to a temp dir, repoints `SCHEMA_DIR` (so the repo is never
touched), mutates `$defs.SddcSpec.required`:

```
tampered -> ['VCF-SCHEMA-INTEGRITY'] valid= False   severity: ['critical']
msg: Integrity check FAILED for the vendored VCF 9.1.1.0 schema ... not a problem with the request.
render tampered -> ['VCF-LIC-EVALUATION','VCF-RENDER-DEFAULT-APPLIED','VCF-SCHEMA-INTEGRITY']
restored, loads: True
```

Present in `catalogue.yaml` at `severity: critical`, and no longer
translated to `VCF-MCP-BAD-ARGS` on either the validate or the render path.

**Finding 7 — `maxItems: 64`: ADDRESSED.** `v1.schema.json:119` carries
`"minItems": 1, "maxItems": 64`. Against a deep-copied real inventory:
3 hosts valid, 63 valid, **64 valid with zero findings**, 65 → a single
`VCF-INV-SCHEMA`, 200 → rejected. The cap does not reject legitimate work.

**Finding 8 — build artefacts: ADDRESSED.** `git ls-files | grep -c
egg-info` → `0`; `vcf-spec-tools/vcfspec.egg-info/` still on disk with
`PKG-INFO` etc. Root `.gitignore` carries `*.egg-info/`, `build/`, `dist/`,
`.venv/`, `.pytest_cache/`.

**Finding 9 — CLI `OSError` string: ADDRESSED (kept, documented).**
Reproduced: `cannot read ZZCANARY9174ZZ-nofile.yaml: no such file` on
stderr, operator's own path only. No document content on any stderr path
tested.

---

## 5. Priority 5 — what the fixes may have broken

**Legitimate large documents still work.** A 64-host inventory (5.5 KB,
the largest the schema now permits) validates at the MCP surface in
0.009 s. A synthetic 262,140-character document is accepted and diffed.

**Library and CLI are NOT silently constrained.** A 267,208-character
document (over `MAX_DOCUMENT_CHARS`) is accepted by `validate_document()`
and by `vcfspec validate <file>` — both return real `VCF-SCHEMA` findings —
while the same text at the MCP surface returns `VCF-MCP-BAD-ARGS`. The
bound is server-surface-only, exactly as the fix report states. `vcfspec
validate --version 9.1.1.0` still works; only unvendored versions are
refused, which is the intended fix, not a silent constraint.

**Performance claim — NOT reproduced.** The fix report states "the worst
legal `vcf_diff_spec` call is now **0.36 s and 10.2 MB**". I measured five
input shapes at the 256 K ceiling (`p5_perf.py`):

```
flat mapping, all changed     262140 chars -> 0.186s   out 0.88 MB
keyed list, all changed       262143 chars -> 0.275s   out 0.59 MB
max keys, all changed         262141 chars -> 0.332s   out 1.07 MB
deep nested (30 levels)       262137 chars -> 0.093s   out 0.13 MB
alias-heavy (99 aliases)       18078 chars -> 0.359s   out 5.26 MB
```

0.36 s is reproducible for those shapes — but it is not the worst legal
call. Pushing alias expansion up to just under `MAX_NODES` (`p5_worst.py`):

```
keys=1900 aliases=99  in=59797 chars -> 3.865s  out 17.28 MB  peak 83.7 MB
keys=2000 aliases=99  in=62897 chars -> 0.148s  VCF-INPUT-UNREADABLE (node cap fires)
```

A **59.8 KB** input — inside `maxLength`, inside `MAX_ALIASES` (99 ≤ 100),
inside `MAX_NODES` (188,100 < 200,000) — costs **3.87 s** and a **17.3 MB**
response, ~10× the time and ~8× the memory the report claims. `call_handler`
is still invoked synchronously inside the `async def call_tool` (no
`asyncio.to_thread`, deliberately deferred), so that is 3.87 s of blocked
event loop.

This is **not a regression**: pre-fix, `MAX_BYTES = 2_000_000` allowed
strictly worse, and what bounds this case is `MAX_NODES`, which the fixes
did not change. The `maxLength` bound is a genuine improvement. What is
wrong is only the *claim*, and it matters because it is the stated reason
residual item 1 (`asyncio.to_thread`) was judged optional. At 3.9 s it is
not optional-feeling any more. **OBS-4** — recommend correcting the figure
in the fix report and re-rating residual item 1.

---

## 6. New observations (none blocking)

- **OBS-1 (INFO).** A directory junction planted inside
  `vcfspec/schemas/` is followed by `_discover()` and admits an arbitrary
  schema. Requires write access to the package directory, which already
  defeats the checksum by simpler means. `schema.py`'s docstring claim
  that set membership constrains this case is overstated; the actual
  control is that `SCHEMA_DIR` is package-owned. Suggest softening the
  comment rather than changing code.
- **OBS-2 (INFO).** The no-reflection rule holds for tool *arguments*,
  not for *document content*. Values and key names from the submitted
  document still appear in finding messages and pointers — necessarily so.
  The largest single instance is the new `maxItems: 64` rejection, whose
  jsonschema message serialises the entire `hosts` array (11,184 characters
  for 65 hosts). Credentials inside it **are** masked — a planted
  `VMw@re123!Real` came back as 65 × `***REDACTED***`, verified. The
  pattern pre-dates this branch; only its magnitude is new. Suggest the
  fix report qualify the rule as argument-scoped.
- **OBS-3 (INFO).** `vendor_schema._refuse_on_change` fails open when a
  digest sidecar is absent but the schema file is present. Same trust
  level as editing the script. Recorded, not required.
- **OBS-4 (LOW — documentation).** The "worst legal `vcf_diff_spec` =
  0.36 s / 10.2 MB" figure is ~10× optimistic; the real worst legal call
  measured 3.87 s / 17.3 MB / 83.7 MB peak. Correct the figure and
  reconsider the priority of the deferred `asyncio.to_thread`.

---

## 7. Working-tree state

Four temporary mutations were applied and reverted with `git checkout --`
(`vcfspec/mcp_server.py` ×2, `vcfspec/rules/catalogue.yaml` +
`vcfspec/api.py`, `pyproject.toml`); `tests/test_security_version.py` was
mutated and restored from a scratchpad copy. Tamper and vendor tests ran
against temp-directory copies, never the repo. A directory junction was
created inside `vcfspec/schemas/` and removed; `vcfspec/evil/` was created
and removed.

```
$ git status --short
?? .playwright-mcp/
$ git diff --stat
(empty)
```

`.playwright-mcp/` was already untracked before this review. The only
addition is this file.
