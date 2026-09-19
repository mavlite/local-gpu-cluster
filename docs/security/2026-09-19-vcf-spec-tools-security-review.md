# Security review — `vcf-spec-tools` and `.claude/skills/vcf-spec-authoring`

- **Date:** 2026-09-19
- **Scope:** merge `585f2d9`; feature range `faa25ed..585f2d9` (57 files, +12,521 lines)
- **Reviewer:** security-reviewer agent (Opus 5)
- **Baseline:** `cd vcf-spec-tools && python -m pytest -q` → **312 passed in 3.67s**
- **Verdict:** **APPROVE WITH FIXES.** No CRITICAL. Two HIGH (both in the same
  root cause: `vcf_version` is concatenated into a filesystem path with no
  validation). The previously-settled adversarial findings all hold.

---

## Method

Everything below was verified by running code, not by reading it. Scratch
scripts lived in the session scratchpad; nothing was written into the repo
except this file. Exact commands are quoted per finding.

Environment: Python 3.14.3 (Windows), repo at
`C:\Users\willi\Documents\GitHub\local-gpu-cluster`.

---

## 1. Confirmation of previously-settled findings

Re-verified by reproduction (`scratchpad/regress.py`, `regress2.py`,
`tamper.py`). All hold:

| Settled issue | Re-test result |
|---|---|
| Credential masking in diff at any depth | `vcf_diff_spec` on nested `credentials/nest/deep/pw` → both sides `***REDACTED***`; no plaintext in the JSON envelope. |
| DNS exfiltration via attacker-chosen hostname | `subdomain: attacker.example.com` with a permitted IP → resolver saw only `10.50.10.11/.12/.13` (reverse lookups on allowlisted IPs, by design); forward name lookups refused with `VCF-PROBE-NAME-BLOCKED`. Zero queries to the attacker domain. |
| Probe fail-closed on empty allowlist | `ProbeConfig()` → `VCF-PROBE-TARGET-BLOCKED` per host **and** `VCF-PROBE-NOTHING-PERMITTED`, `valid: false`. |
| YAML alias-expansion DoS | 213-byte 8^6 alias bomb → `VCF-INPUT-UNREADABLE` in **0.050s**. |
| jsonschema echoing a literal password | Example inventory with `esxRoot: VMw@re123!Real` → the literal is **absent** from the whole envelope; reported structurally as `VCF-CRED-NOT-A-REFERENCE`. |
| Rendered specs going unvalidated | `_verify_rendered()` runs (`layers_run` ends `…render, verify`) and withholds `spec` on failure. |
| `valid: true` with no layer run | `not_an_inventory: 1` → `valid: false`, `VCF-INPUT-UNRECOGNISED`, all three layers in `layers_skipped`. |
| Vendored-schema checksum enforced at runtime | See finding 6 — enforced, but see finding 1 for how it is bypassed. |

---

## Findings

### HIGH-1 — `vcf_version` escapes the schemas directory, defeating the vendored-schema checksum

**What.** `vcf_version` is an MCP-caller-controlled string that is
concatenated straight into a path. The SHA-256 sidecar is read from the
*same caller-chosen directory*, so the integrity control only binds a file
to its own sibling — it does not bind the loader to the vendored schema.

**Where.**
- `vcf-spec-tools/vcfspec/schema.py:21-22` — `return SCHEMA_DIR / version / "sddc-spec.schema.json"`
- `vcf-spec-tools/vcfspec/schema.py:28-30` — sidecar read from `path.parent`
- `vcf-spec-tools/vcfspec/render.py:54-56` — `(DEFAULTS_DIR / f"{version}.yaml").read_text(...)`
- Reached from `vcf-spec-tools/vcfspec/mcp_server.py:62` and `:69`, and from `vcfspec/cli.py` `--version`.

**Reproduction** (`scratchpad/escape.py`):

```python
tmp = pathlib.Path(tempfile.mkdtemp())/"evil"; tmp.mkdir()
text = json.dumps({"$schema": ".../2020-12/schema", "type": "object"}, indent=2)+"\n"
(tmp/"sddc-spec.schema.json").write_text(text, newline="\n")
(tmp/"sddc-spec.schema.json.sha256").write_text(
    hashlib.sha256(text.encode()).hexdigest()+"\n", newline="\n")
v = os.path.relpath(tmp, S.SCHEMA_DIR).replace("\\", "/")
call_handler("vcf_validate_spec",
             {"document": "sddcId: x\ngarbage: 1\n",
              "input_kind": "sddc_spec", "vcf_version": v})
```

Output:

```
version arg: ../../../../../../AppData/Local/Temp/tmpolhn5l0x/evil
result: True []
=> attacker-placed schema OUTSIDE the package was loaded and its own sidecar accepted
```

A junk `sddc_spec` was certified `valid: true` with **zero findings**. This is
precisely the outcome the package exists to prevent. Also confirmed that
`SCHEMA_DIR / "C:/Windows/Temp"` resolves to `C:\Windows\Temp\…` (absolute
paths win), and that `\0` in the version is silently coerced to a space by
`pathlib`.

**Caveat, stated honestly.** Full exploitation needs a file the process can
read at an attacker-known path — i.e. filesystem write. In the stated threat
model (an AI agent drives the MCP server and usually also has a Write tool),
that chain is realistic, and a prompt-injected agent that gets the tool to
*certify* a bad spec is materially worse than one that merely lies about it.

**Fix.** Validate the version against a closed pattern before it ever touches
a path, and refuse anything that leaves the package directory:

```python
_VERSION_RE = re.compile(r"\A\d+(?:\.\d+){0,3}\Z")

def schema_path(version: str = DEFAULT_VERSION) -> Path:
    if not _VERSION_RE.match(version):
        raise FileNotFoundError(f"not a VCF version: {version!r}")
    path = (SCHEMA_DIR / version / "sddc-spec.schema.json").resolve()
    if not path.is_relative_to(SCHEMA_DIR.resolve()):
        raise FileNotFoundError(f"not a VCF version: {version!r}")
    return path
```

Better still: derive the allowed set from `sorted(p.name for p in
SCHEMA_DIR.iterdir() if p.is_dir())` and membership-test it, so the path is
never built from caller text at all. Apply the identical guard in
`render._defaults_text`.

---

### HIGH-2 — `vcf_version` is a filesystem existence oracle for an MCP caller

**What.** The same unvalidated string distinguishes "file exists" from "file
absent" through two different finding codes, giving an agent a probe for any
path ending in `.yaml` (via `vcf_render_spec`) or any directory containing
`sddc-spec.schema.json` (via `vcf_validate_spec`). No file write required.

**Where.** `vcfspec/api.py:275-286` (`except (FileNotFoundError,
SchemaIntegrityError)` → `VCF-SCHEMA-VERSION-UNKNOWN`) vs `api.py:290-294`
(any other exception → `VCF-RENDER-FAILED`), translated at
`mcp_server.py:107-113`.

**Reproduction** (`scratchpad/oracle.py`), same document, two versions
pointing at a present and an absent `.yaml`:

```
EXISTS  version=../../../../../../AppData/Local/Temp/tmp…  codes: ['VCF-LIC-EVALUATION', 'VCF-RENDER-FAILED']
ABSENT  version=../../../../../../AppData/Local/Temp/tmp…  codes: ['VCF-LIC-EVALUATION', 'VCF-MCP-BAD-ARGS']
```

Two distinct, reliably distinguishable answers. Content disclosure is a
further step away: `render.default()` (`render.py:66-73`) puts
`entry['value']!r` into a finding message and `entry['source']` into
`source_url`, so a `.yaml` file shaped like a defaults table would have its
values echoed back to the caller verbatim.

**Fix.** The same input validation as HIGH-1 closes this completely. Do not
try to fix it by collapsing the two error codes — the distinction is useful
for a legitimate caller; the traversal is the bug.

---

### MEDIUM-3 — `mcp>=1.2` floor admits five known-vulnerable releases

**What.** `pyproject.toml:8` has the upper bound the 2026-08 `mcp` 2.0.0
outage taught (`<2` — good), but the **floor** is `1.2`, and every release
below 1.28.1 carries at least one advisory.

**Where.** `vcf-spec-tools/pyproject.toml:8` — `mcp = ["mcp>=1.2,<2"]`

**Reproduction.**

```
$ python -m pip_audit -r <(echo 'mcp==1.9.0') --progress-spinner off
Name Version ID              Fix Versions
mcp  1.9.0   PYSEC-2026-1616 1.9.4
mcp  1.9.0   PYSEC-2026-1618 1.10.0
mcp  1.9.0   PYSEC-2026-1617 1.23.0
mcp  1.9.0   PYSEC-2026-3482 1.27.2
mcp  1.9.0   PYSEC-2026-3483 1.28.1
```

A fresh resolve today picks `mcp 1.30.0`, which audits clean — so this is
latent, not live. It becomes live on any constrained resolve, stale wheel
cache, or offline/mirror install.

**Fix.** `mcp = ["mcp>=1.28.1,<2"]`. Add `pip-audit` to the `dev` extra and a
CI step; a floor with no audit gate drifts again.

---

### MEDIUM-4 — `vendor_schema.py` can silently re-vendor a hostile schema with a valid checksum

**What.** The script fetches upstream over HTTPS, converts, writes
`sddc-spec.schema.json`, then computes the SHA-256 **of the bytes it just
wrote** and writes that as the sidecar. The checksum therefore attests
integrity-at-rest only. There is no signature, no pinned upstream digest and
no "refuse if the existing checksum differs" guard, so a MITM'd, typosquatted
or simply wrong `source` argument produces a fully self-consistent vendored
schema. The only control is a human reading the resulting git diff — of a
1,685-line machine-generated JSON file.

**Where.** `vcf-spec-tools/scripts/vendor_schema.py:85` (fetch),
`:117-121` (write schema, then derive and write sidecar from the same text).

**Reproduction.** Read the code path; confirmed by construction in
HIGH-1, where a self-consistent schema+sidecar pair was accepted by the
loader without complaint.

**Fix.** Record and verify an upstream digest: keep a
`sddc-spec.source.sha256` of the *fetched OpenAPI document*, and have the
script refuse when it differs unless `--accept-new-upstream` is passed.
Refuse to overwrite an existing vendored schema whose checksum currently
verifies unless the same flag is given. Print both digests so the bump is
reviewable from the commit message rather than the JSON diff.

---

### MEDIUM-5 — a legal 1.8 MB document costs ~19 s of event-loop-blocking CPU

**What.** `MAX_BYTES = 2_000_000` bounds size, not cost. PyYAML's pure-Python
loader parses at roughly 5 MB/min here, so a single in-bounds
`vcf_diff_spec` call (two documents) blocks for ~19 s. The MCP server is
long-lived, single-process and `asyncio`-driven, so one call stalls every
other. The diff algorithm itself is fine — profiling shows the time is in the
parser, not the diff.

**Where.** `vcfspec/documents.py:11` (`MAX_BYTES`), `:76` (`yaml.safe_load`);
reached from `mcp_server.py:168`.

**Reproduction** (`scratchpad/worst.py`, `prof.py`):

```
doc bytes: 1792677
diff: 20.95s changed=22000 out_bytes=2254732 peak_mem=112.7MB

cProfile: 18.878s total; 18.396s cumulative in documents.load_document,
          18.141s of that in yaml.safe_load
```

ReDoS was tested and is **not** a factor: `redact()` on 200 KB of
`"'"*200000`, `"'" + "a"*199999`, `"password="+"a"*199999`, `"password='"*20000`
and a repeated `'…' is too short` string all completed in **0.001–0.014 s**.
`_QUOTED_SECRET_RE`, `_INLINE_SECRET_RE`, `CREDENTIAL_KEY_RE`, `REFERENCE_RE`
and `_ALIAS_RE` are all linear-time constructions with no nested quantifiers.
The `lru_cache`s on `_load_verified` (maxsize=8) and `_defaults_text`
(maxsize=4) are bounded, so arbitrary `vcf_version` values cannot grow memory.

**Fix.** Three independent, cheap improvements:
1. Use libyaml when present — `yaml.CSafeLoader` **is available** in this
   environment (`hasattr(yaml, 'CSafeLoader') == True`) and is roughly 20×
   faster. Fall back to `SafeLoader` when it is not.
2. Lower `MAX_BYTES` for the MCP path. A real 3-host inventory is ~1.5 KB;
   256 KB is a generous ceiling and cuts the worst case to well under a second.
3. Run `call_handler` under `asyncio.to_thread()` in `mcp_server.call_tool`
   so a slow document cannot stall the protocol loop.

---

### LOW-6 — a tampered vendored schema is reported to the caller as a bad argument

**What.** The runtime checksum check **is** enforced (good — see below), but
`api.py` catches `SchemaIntegrityError` in the same `except` clause as
`FileNotFoundError`, so tampering of the shipped schema surfaces to an MCP
caller as `VCF-MCP-BAD-ARGS: no vendored schema for VCF version '9.1.1.0'` —
a retryable usage error. A supply-chain compromise is presented as a typo,
and nothing logs or alerts.

**Where.** `vcfspec/api.py:178-181` and `:275-286`; translated at
`mcp_server.py:105-113`.

**Reproduction** (`scratchpad/tamper.py`): mutate `$defs.SddcSpec.required`,
clear the cache, call the tool.

```
runtime refuses tampered schema: schema for 9.1.1.0 failed integrity check: expected 4bb952789dcd02a3..., got 972243e00cc66
via MCP: ['VCF-MCP-BAD-ARGS'] valid= False
restored, verifies: True
```

Positive half of this result: the checksum **is** enforced on every
`load_schema()`, at runtime, not just in a test. `SchemaIntegrityError` is
raised from `schema.py:31-34` and no code path bypasses it.

**Fix.** Give integrity failure its own catalogue code
(`VCF-SCHEMA-INTEGRITY`, severity `critical`, not retryable) and keep it out
of the `BAD-ARGS` translation in `_reclassify_unknown_version`.

---

### LOW-7 — probe wall-clock is unbounded in aggregate

**What.** Each probe target is bounded (`timeout_s`, default 2.0, enforced by
a `thread.join(timeout)`), but there is no cap on the **number** of targets
and no global budget. `hosts` in `vcfspec/schemas/inventory/v1.schema.json:118-119`
declares `minItems: 1` and **no `maxItems`**, so host count is limited only
by `MAX_BYTES`/`MAX_NODES` — roughly 20,000 hosts in a legal document. At
3 × `timeout_s` per host (forward DNS, reverse DNS, TCP 443) that is ~33 h of
serial wall-clock.

**Where.** `vcfspec/validate/probes.py:146` (the unbounded `for index, host in
enumerate(inventory.get("hosts") or [])`), `:164/171/174`.

**Reproduction** (`scratchpad/regress2.py`), 3 hosts, 1.0 s timeout, every
resolver and connector hanging:

```
6 3 hosts, 1s timeout, all hang: 9.0s wall (no global cap)
```

Exactly `hosts × 3 × timeout_s`, linear, with no ceiling.

**Severity rationale.** LOW, not MEDIUM: probes are CLI-only (the MCP server
deliberately exposes no `probe_config` — `mcp_server.py:3-10` documents why,
including the daemon-thread leak), they require an operator-supplied
`--allowlist` that actually covers the targets, and an operator can Ctrl-C.

**Fix.** Add `"maxItems": 64` to `hosts` in the inventory schema (a VCF
management domain will never approach it) and a `deadline` in `ProbeConfig`
checked at the top of the host loop.

---

### INFO-8 — build artefacts committed; `.gitignore` does not cover them

`vcf-spec-tools/vcfspec.egg-info/` (`PKG-INFO`, `SOURCES.txt`,
`dependency_links.txt`, …) is tracked (`git ls-files` confirms), and the root
`.gitignore` covers only `__pycache__/`, `*.pyc`, two operator notes and
`config.env`. Verified the egg-info contains **no** local absolute paths or
usernames (`grep -inE "C:\\\\Users|/home/|willi"` → no matches), so there is
no disclosure here — it is hygiene only. **Fix:** `git rm -r --cached
vcf-spec-tools/vcfspec.egg-info` and add `*.egg-info/`, `build/`, `dist/`,
`.venv/`, `.pytest_cache/` to `.gitignore`. (`.pytest_cache/` currently
self-ignores via its own generated `.gitignore`, which works but is
incidental.)

---

### INFO-9 — CLI stderr echoes an `OSError` string

`vcfspec/cli.py:127` — `print(f"cannot read {path}: {exc}", file=sys.stderr)`.
The exception is an `OSError` from `Path.read_text`, so the text is an errno
message plus the path the operator themselves typed. No document content and
no credential can reach it — `cli.py:181-186` is careful to print only
`type(exc).__name__` on the one path where spec content could be in play.
Recorded for completeness, not for action.

---

## 2. Secrets in source and git history

**Scanned:** the whole `faa25ed..585f2d9` diff, not just the tree.

High-entropy / key-material patterns (`BEGIN … PRIVATE KEY`, `AKIA…`,
`gh[pousr]_…`, `sk-…`, `xox[baprs]-`, JWT `eyJ…`): **zero matches.**

Credential-shaped assignments: every hit is a deliberate test literal or a
documented placeholder. Located and confirmed:

| Literal | Files |
|---|---|
| `VMw@re123!Real` | `tests/test_credentials.py`, `tests/test_redact.py` |
| `hunter2pass` / `hunter4` | `tests/test_redact.py` |
| `Vcf!Placeholder1` | `vcfspec/validate/schema_layer.py` (the substitution placeholder, by design), `README.md`, the plan doc |
| `TotallyLeakedSecretXYZ999`, `zzz-unrecognised-shape-…` | negative-coverage fixtures |

None is a credential for any real system: the VCF lab described in
`docs/` has not been built, the tools by contract never accept a literal
credential (`render.InsecureCredentialError`, `VCF-CRED-NOT-A-REFERENCE`),
and `Vcf!Placeholder1` is substituted *into* a document before schema
validation precisely so a real secret never reaches jsonschema.

**Lab identifiability.** `vcfspec/examples/lab-3-host.yaml` uses `lab.local`
and `10.50.10.0/24` — invented, not the real lab. The vendored schema carries
only `x-source` (the public `vmware/vcf-api-specs` raw URL) and
`x-vcf-version`. One real detail does enter the diff:
`docs/superpowers/plans/2026-09-18-vcf-spec-tools.md:~3602` names the home
uplink `192.168.6.0/24`. It is RFC 1918, already present throughout
`docs/archive/`, and discloses nothing routable — flagged for awareness, no
action required. No internal hostnames, no real IPs, no personal identifiers
anywhere in the range.

**`.gitignore`** correctly covers `config.env` / `scripts/config.env` (the
live-credential files) and the two operator notes. Gap is build artefacts
only — see INFO-8.

---

## 3. Path traversal — full MCP input inventory

Every tool input, and what it becomes:

| Tool | Input | Becomes a path? |
|---|---|---|
| `vcf_spec_schema` | *(none — `_schema()` with no properties)* | No. `EXAMPLE_PATH` is a module constant (`mcp_server.py:58`). Not keyed by version. |
| `vcf_validate_spec` | `document` | **No** — YAML/JSON *text*, `load_document(text)` (`documents.py:70`). Confirmed. |
| | `input_kind` | No. Closed enum `{inventory, sddc_spec}` (`mcp_server.py:41`). |
| | `vcf_version` | **YES — see HIGH-1/HIGH-2.** |
| `vcf_render_spec` | `document`, `input_kind` | No, as above. |
| | `vcf_version` | **YES — see HIGH-1/HIGH-2.** |
| `vcf_explain_finding` | `code` | No. Dict key lookup into the parsed catalogue (`mcp_server.py:134`). |
| `vcf_diff_spec` | `left`, `right` | **No** — text, `load_document` per side (`mcp_server.py:168`). |

Traversal vectors tested against `vcf_version`: `../` (works), `..\` (works on
Windows), absolute `C:/…` and `/etc` (work — `pathlib` lets an absolute
component win), URL-encoded `%2f` (does **not** decode — no double-decoding
bug), embedded `\0` (coerced to a space, no crash, no bypass). Symlinks were
not separately tested because plain `../` already escapes, making symlinks
redundant as a vector.

`scripts/vendor_schema.py` writes only to
`OUT_DIR / version / …` where `OUT_DIR` is `__file__`-derived
(`vendor_schema.py:24`). Its `version` comes from `sys.argv` and is
cross-checked against `doc["info"]["version"]` (`:90-92`), so a maintainer
cannot fat-finger it into a wrong directory — but it is a maintainer-run
script, not an agent-reachable surface, so the traversal concern is N/A there.

---

## 4. MCP transport exposure — no finding

`main()` (`mcp_server.py:452-463`) uses `mcp.server.stdio.stdio_server()` and
nothing else. There is **no** HTTP/SSE server, no `uvicorn`, no bind address,
no port anywhere in the package (`grep -rnE "0\.0\.0\.0|--port|uvicorn|sse"`
over `vcfspec/` → no matches). `build_server()` returns a bare
`mcp.server.Server` with the two decorated handlers and performs no binding,
so an embedder would have to attach a transport deliberately.

`README.md:374-381` instructs exactly `python -m vcfspec.mcp_server` and
describes it as "a stdio MCP server". `.claude/skills/vcf-spec-authoring/SKILL.md`
adds no transport instruction. Nothing in the docs would lead an operator to
expose this beyond the local process. Authentication is therefore inherited
from process ownership, which is correct for stdio.

---

## 5. Logging and error output — no finding

`grep -rn "print(\|logging\|logger\|traceback\|sys.stderr\|sys.stdout"` over
`vcfspec/` returns hits in exactly one module, `cli.py`, all of them
intentional:

- `cli.py:119/122/127` — file-read usage errors (see INFO-9)
- `cli.py:145-157` — `--probe`/`--allowlist` usage errors, no document content
- `cli.py:184-190` — the last-resort internal-error path, which prints
  `type(exc).__name__` only, never `str(exc)`
- `cli.py:189-193` — the result envelope itself, which has already passed
  through `redact()`

There is **no** `logging` configuration, no logger, no log file, and no
handler in the package. Nothing writes document contents or credential values
anywhere. No traceback can reach a caller: `call_handler`
(`mcp_server.py:399-427`) wraps dispatch in two `try/except` blocks that emit
only a class name, and `api.py` makes the same promise one layer down.
Verified live — every error envelope produced during this review carried a
class name or a structured code, never exception text.

---

## 6. Dangerous APIs — none present

`grep -rnE "subprocess|os\.system|eval\(|exec\(|pickle|marshal|__import__|shell=True|yaml\.load\(|unsafe_load|urlopen|requests\."`
over `vcfspec/` and `scripts/` returns exactly one hit:
`scripts/vendor_schema.py:85`, `urllib.request.urlopen` against the pinned
HTTPS `vmware/vcf-api-specs` URL, in a maintainer-run script. No shell
execution, no deserialisation of anything but YAML via `safe_load`, no
`yaml.load` with an unsafe loader anywhere.

Both `jsonschema` validators (`schema_layer.py:63`, `inventory.py:36`) are
constructed as bare `Draft202012Validator(schema)` with **no** `format_checker`
and **no** custom registry/resolver. The vendored bundle's only `$ref`s are
internal `#/$defs/…` (rewritten from OpenAPI at `vendor_schema.py:60-63`), so
there is no remote-ref fetch and no SSRF surface. User documents are *data*,
never schemas, so a `$ref` in an operator document is inert.

---

## Dependency inventory

Declared (`pyproject.toml:5-11`):

| Package | Constraint | Upper bound? | Resolved / installed |
|---|---|---|---|
| `pyyaml` | `>=6.0` | **No** | **6.0.3** |
| `jsonschema` | `>=4.21` | **No** | **4.26.0** |
| `mcp` *(extra)* | `>=1.2,<2` | Yes | **1.30.0** (resolved; not installed locally) |
| `pytest` *(dev)* | `>=8.0` | **No** | 8.x |
| `setuptools` *(build)* | `>=68` | **No** | — |

Transitive, from the resolve of the full `[mcp]` extra: `httpx 0.28.1`,
`httpx-sse 0.4.3`, `pydantic 2.13.5`, `pydantic_core 2.46.5`,
`pydantic-settings 2.15.0`, `anyio 4.15.1`, `attrs 26.1.0`,
`jsonschema-specifications 2025.9.1`, `sse-starlette 3.4.11`, `starlette 1.6.0`.

`python -m pip_audit -r <declared deps>` → **"No known vulnerabilities found."**
Audit at the constraint floors is finding MEDIUM-3.

Per the project standard that every dependency carries an upper bound — the
rule written after `mcp` 2.0.0 broke `mcp-sdg` in this homelab — the three
unbounded constraints above should become `pyyaml>=6.0.2,<7`,
`jsonschema>=4.21,<5`, `pytest>=8.0,<10`. `pyyaml` and `jsonschema` are less
likely than `mcp` to ship a breaking major, but the policy exists because
"unlikely" was the reasoning last time too.

---

## Checklist areas explicitly N/A for this stack

Stated so the review is honest about its own scope rather than padded:

- **SQL injection** — N/A. No database, no DB driver, no query string anywhere.
- **XSS / CSP / output escaping** — N/A. No HTML, no templating, no browser
  surface. Output is JSON to stdout or an MCP `TextContent` block.
- **CSRF** — N/A. No HTTP server, no forms, no cookies, no sessions.
- **XXE** — N/A. No XML parser is imported. The one XML-adjacent key in the
  vendor script (`xml`) is an OpenAPI annotation that is *stripped*
  (`vendor_schema.py:57`).
- **Insecure deserialisation** — N/A beyond YAML, which is `safe_load` only
  and additionally bounded. No `pickle`, `marshal` or `shelve`.
- **Authn/authz, session management, rate limiting** — N/A at the code level.
  stdio MCP has exactly one client (its parent process) and inherits its
  privileges; there is no network listener to authenticate or rate-limit. The
  CLI is a local process under the operator's own account.
- **Password hashing / crypto** — N/A. The package handles no secret by
  design: credentials must be `${reference}` strings and `render()` raises
  `InsecureCredentialError` rather than emit a literal. SHA-256 appears once,
  as a file checksum, which is the correct use.
- **HTTPS enforcement / TLS config** — N/A at runtime. The only outbound
  request in the repo is the maintainer-run vendor fetch, which is `https://`.
- **File upload handling** — N/A. No upload path; the CLI reads an
  operator-named local file, the MCP server accepts text only.
- **Payments / PII** — N/A. No payment code, no personal data. The documents
  are network and hardware topology.
- **Security event logging / alerting** — N/A as a code requirement (see the
  "no logging" note in §5, which is the deliberate design), but LOW-6 is the
  one place where a security-relevant event — schema integrity failure —
  deserves to be distinguishable rather than silent.

---

## Working-tree state

`tamper.py` briefly rewrote `vcfspec/schemas/9.1.1.0/sddc-spec.schema.json`
to prove the checksum is enforced. Python's default newline translation on
Windows rewrote the file's line endings on restore; this was caught and
reverted with `git checkout --`, and schema integrity re-verified afterwards
(`load_schema()` → OK). `pip-audit` was installed into the user site-packages
to run the dependency audit; that is an environment change, not a repo change.

Final state:

```
$ git status --short
?? .playwright-mcp/
```

`.playwright-mcp/` was already untracked at the start of the session and was
not created by this review. **No file in the repository was modified.** The
only addition is this report.
