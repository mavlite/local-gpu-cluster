# ESX hardware health (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each BD795i SE VCF host publishes CPU temperatures, CPU package power, per-core
effective clock and NVMe health as an ESX health report, `lab.hwHealth`. VCF Operations charts
the report and sends alerts by email.

**Architecture:**
- A Python 3.11, standard-library-only package reads the sensors through `vsish` and `esxcli`
  (commands injected, so tests need no ESX).
- A `healthd` plugin publishes the readings through ESX's health report service. A boot-time
  sampler is the fallback if the plugin sandbox can't read the hardware.
- It ships as a component in the cluster's vLCM image.
- A Management Pack Builder pack pulls the report into Operations.

**Tech Stack:** Python 3.11 (ESX's interpreter; no third-party modules on the host), pytest on the
workstation, ESX 9.1.1 `healthd`/ConfigStore/vLCM, VCF Operations Management Pack Builder.

**Spec:** `docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md`

**Start condition:** the UniFi network migration plan
(`docs/superpowers/plans/2026-11-05-ubiquiti-lab-network-migration.md`) is complete. Re-check
host IPs, vCenter and Operations reachability before Task 1. The addresses below are from
2026-11-06: `hyp01` `172.16.10.145`, `hyp02` `.143`, `hyp03` `.141`, vCenter `.129`.

## Global Constraints

- Host code runs on ESX's Python 3.11: standard library only, no syntax or stdlib newer than 3.11.
- Supported CPU: AMD family 25 (0x19), model 97 (0x61). Anything else is `supported: false`, with no
  CPU readings and a WARNING. Never guess.
- Only hardware write allowed: the SMN index (root complex config `0x60`), restored to `0` in a
  `finally`. SMN addresses touched: `0x00059800`, `0x00059b08`, `0x00059b0c` only.
- If the SMN index reads non-zero, skip the temperatures for that run and say why.
- A reading that can't be trusted is reported as missing in `errors[]` with a reason, never as
  `0` or a guess.
- No kernel code, no SSH polling (the owner rejected it).
- Secrets: credentials from `~/.vcflab/credentials.env` by key name only (the
  `load(key)` pattern), never printed. Fixtures and logs hold no credentials.
- Report name `lab.hwHealth`. Plugin key and VIB name `lab-hwhealth`. Report `schema: 1`. The
  field names in the spec's "Report contents" are the contract with the Operations pack.
- Thresholds (spec): Tctl and CCD WARNING ≥ 90 °C, ERROR ≥ 95 °C. NVMe temperature WARNING ≥
  WCTEMP (else 70 °C), ERROR ≥ CCTEMP (else 80 °C). Any NVMe critical-warning bit → ERROR. NVMe
  spare WARNING ≤ 20 %, ERROR ≤ 10 %. CPU unsupported or all CPU readings missing → WARNING.
  Nothing alerts on effective clock.
- The whole run fits inside `healthd`'s `max_lifetime` of 30 s: each command has a 10 s
  timeout, and the run has a 20 s budget.
- Live changes to hosts, vCenter, the vLCM image or Operations are made by the owner, or by the
  builder only after the owner says yes in that task. The builder's own checks are read-only.
- Commits: conventional (`feat:`, `test:`, `docs:` and so on), committed by path, **no
  `Co-Authored-By` trailer**.
- Tests: `python3 -m pytest scripts/esx-hwhealth/tests -q` (on the Windows workstation,
  `python -m pytest …`).

## Review Focus

1. A `vsish` or `esxcli` command prints an error ("Get failed: Not supported", empty output) instead of a
   value → that reading goes to `errors[]` with the command's message, and the rest of the report still
   builds. Test: `test_vsish_error_text_is_missing_not_zero` (Task 3).
2. A core halted for the whole window (`ΔMPERF == 0`), or an energy counter that wrapped → `None`/the
   correct delta, never a division error or a negative wattage. Tests: `test_effective_mhz_zero_mperf`,
   `test_energy_delta_wraps` (Task 2).
3. A runner exception partway through an SMN read → the index is still restored to `0` and the
   temperatures are reported missing. Test: `test_smn_restores_index_on_error` (Task 3).
4. One NVMe drive's SMART query fails, or a drive reports no WCTEMP/CCTEMP (0 or absent) → the other drives
   are still reported, and the 70/80 °C defaults apply. Tests: `test_nvme_one_drive_fails`,
   `test_nvme_threshold_defaults` (Tasks 3, 4).
5. A slow command (`esxcli nvme` stalls) → the run ends inside the 20 s budget, and the readings
   it didn't get are missing with reason `"budget"`. Test: `test_collect_respects_budget` (Task 5).

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/esx-hwhealth/hwhealth/__init__.py` | Package marker, `SCHEMA = 1`, `REPORT_NAME = "lab.hwHealth"` |
| `scripts/esx-hwhealth/hwhealth/decode.py` | Pure decode: raw register → °C, J, MHz |
| `scripts/esx-hwhealth/hwhealth/runner.py` | `Runner` type, `CommandError`, real subprocess runner with timeout |
| `scripts/esx-hwhealth/hwhealth/smn.py` | SMN index/data access with the safety rule |
| `scripts/esx-hwhealth/hwhealth/readers.py` | CPU identity, RAPL, APERF/MPERF, NVMe parsers over a runner |
| `scripts/esx-hwhealth/hwhealth/collect.py` | One sampling run → the `unstructured` dict (budget, two-point deltas) |
| `scripts/esx-hwhealth/hwhealth/evaluate.py` | Thresholds → `(status, reasons)` |
| `scripts/esx-hwhealth/lab_hwhealth.py` | `healthd` plugin entrypoint; `--stdout`; `--from-cache` (fallback) |
| `scripts/esx-hwhealth/lab_hwhealth_sampler.py` | Fallback sampler, built only if Task 1 says so |
| `scripts/esx-hwhealth/packaging/build_component.py` | Builds the VIB and offline-bundle component |
| `scripts/esx-hwhealth/tests/…` | pytest; `conftest.py` puts `scripts/esx-hwhealth` on `sys.path` |
| `scripts/esx-hwhealth/tests/fixtures/…` | Recorded real `vsish`/`esxcli` output (Task 1) |
| `docs/esx-hwhealth/spike-findings.md` | Task 1 answers with evidence |
| `docs/esx-hwhealth/runbook.md` | Install, verify, remove, upgrade, Operations pack |
| `docs/esx-hwhealth/ops-pack/` | Management Pack Builder export, dashboard, alert definitions |

---

### Task 1: Nested ESX spike and fixture capture

Answers the spec's open questions before code depends on them. Output is
`docs/esx-hwhealth/spike-findings.md` plus fixtures. Every question gets **yes or no, with the
command and its real output**.

**Files:**
- Create: `docs/esx-hwhealth/spike-findings.md`
- Create: `scripts/esx-hwhealth/tests/fixtures/` (`cpu_list.txt`, `msr_c0010299.txt`, `msr_c001029b.txt`,
  `pcpu0_perf.txt`, `smn_get_0x64.txt`, `vsish_not_supported.txt`, `nvme_device_list.txt`,
  `nvme_smart_<vmhba>.txt`, `nvme_device_get_<vmhba>.txt`, `ntp_health_report.json`)

- [ ] **Step 1: Owner gate: nested ESX VM.** Ask the owner to approve a nested ESX 9.1.1 VM, `esx-hwh-lab`
  (4 vCPU, 16 GB, hardware-assisted virtualisation exposed, local datastore, dev VM yield policy:
  CPU shares Custom 1000, memory shares Low), on a Ryzen VCF host or the Proxmox host. The builder
  deploys it only after a yes. Record the placement in the findings.
- [ ] **Step 2: Capture fixtures from `hyp01` (read-only, SSH enabled by the owner).** Save each command's output
  as-is into the fixture files. `smn_get_0x64.txt` comes from a plain `vsish -e get …/addr/0x64`
  with no index write. Scan every fixture for `pass|secret|token` before saving.
  Expected: 10 files, no matches from the scan.
- [ ] **Step 3: S1 — registration.** On the nested host, create the ConfigStore entry `esx/health/lab-hwhealth`
  (a copy of `ntp_health`'s config with `path` pointing at a 5-line test plugin that sends
  `{"hello": 1}`). Try, in order: `configstorecli config current create`, a default-config JSON shipped
  in a test VIB. Record which works, and whether it survives a reboot.
  Expected: one method works; `esxcli system health report get -r lab.hwHealth` returns `{"hello": 1}`.
- [ ] **Step 4: S2 — sandbox.** From the test plugin (running under `healthd`), run
  `vsish -e get …/pciConfigReg/size/4/addr/0x0`, `vsish -e set …/0x60 0x0`,
  `vsish -e get /hardware/msr/pcpu/0/addr/0xC0010299` and `esxcli nvme device list`, and put each exit
  code and output in the report. Nested hardware is virtual, so this proves **permission**, not
  values.
  Expected: decide **plugin-direct** (all four work) or **sampler fallback** (any are denied), and
  write the decision at the top of the findings.
- [ ] **Step 5: S3 — remote API.** From the workstation, find a working way to read
  `GET /api/esx/observability/reports/lab.hwHealth` on the nested host, and through vCenter if it
  proxies host reports. Record the exact auth flow (session endpoint, headers, content type), and
  whether a custom report shows in the vSphere UI or vCenter API.
  Expected: a working `curl`-equivalent recipe, with credentials taken by key name.
- [ ] **Step 6: S4 — packaging.** Build a test VIB and offline-bundle component holding the test plugin.
  Prove `esxcli software component apply -d <bundle.zip>`, then remove it, on the nested host at
  CommunitySupported acceptance. Record the build tool and format details that work for 9.1.1, and
  whether a reboot is needed.
  Expected: install, report present, remove, report gone.
- [ ] **Step 7: Commit**

```bash
git add docs/esx-hwhealth/spike-findings.md scripts/esx-hwhealth/tests/fixtures
git commit -m "docs(hwhealth): nested ESX spike findings and recorded fixtures"
```

### Task 2: Decode functions

**Files:**
- Create: `scripts/esx-hwhealth/hwhealth/__init__.py`, `scripts/esx-hwhealth/hwhealth/decode.py`
- Test: `scripts/esx-hwhealth/tests/conftest.py`, `scripts/esx-hwhealth/tests/test_decode.py`

**Interfaces:**
- Produces: `tctl_c(raw: int) -> float`; `ccd_c(raw: int) -> float | None`;
  `rapl_energy_unit_j(unit_raw: int) -> float`; `energy_delta_j(before: int, after: int, unit_j: float) -> float`;
  `effective_mhz(nominal_mhz: float, aperf_delta: int, mperf_delta: int) -> float | None`.

- [ ] **Step 1: Write the failing tests** (values are the 2026-11-06 `hyp01` readings)

```python
import pytest
from hwhealth.decode import tctl_c, ccd_c, rapl_energy_unit_j, energy_delta_j, effective_mhz

def test_tctl_range_select():
    assert tctl_c(0x7d3b0000) == pytest.approx(76.125)
    assert tctl_c(0x7d5b0000) == pytest.approx(76.25)

def test_tctl_without_range_select():
    assert tctl_c(600 << 21) == pytest.approx(75.0)

def test_ccd_valid_and_invalid():
    assert ccd_c(0xb8f) == pytest.approx(64.875)
    assert ccd_c(0xbec) == pytest.approx(76.5)
    assert ccd_c(0x38f) is None          # bit 11 clear

def test_rapl_unit():
    assert rapl_energy_unit_j(0xa1000) == 2 ** -16

def test_energy_delta():
    assert energy_delta_j(0xcaacc12d, 0xcb433a5c, 2 ** -16) == pytest.approx(150.4734, rel=1e-4)

def test_energy_delta_wraps():
    assert energy_delta_j(0xFFFFFFF0, 0x10, 1.0) == 32.0

def test_effective_mhz():
    assert effective_mhz(2495.3, 2000, 1000) == pytest.approx(4990.6)

def test_effective_mhz_zero_mperf():
    assert effective_mhz(2495.3, 5, 0) is None
```

- [ ] **Step 2: Run, expect FAIL** — `python3 -m pytest scripts/esx-hwhealth/tests/test_decode.py -q` → `ModuleNotFoundError: hwhealth`.
- [ ] **Step 3: Implement** the five functions with the spec's decode rules (energy counter is 32-bit: `(after - before) % 2**32`). `conftest.py` inserts `scripts/esx-hwhealth` into `sys.path`. `__init__.py` defines `SCHEMA = 1` and `REPORT_NAME = "lab.hwHealth"`.
- [ ] **Step 4: Run, expect PASS** — 8 passed.
- [ ] **Step 5: Commit** — `git add scripts/esx-hwhealth/hwhealth scripts/esx-hwhealth/tests/conftest.py scripts/esx-hwhealth/tests/test_decode.py` then `git commit -m "feat(hwhealth): Zen 4 temperature, RAPL and APERF decode"`.

### Task 3: Runner, SMN access and readers

**Files:**
- Create: `scripts/esx-hwhealth/hwhealth/runner.py`, `smn.py`, `readers.py` (in `hwhealth/`)
- Test: `scripts/esx-hwhealth/tests/test_smn.py`, `test_readers.py`

**Interfaces:**
- Consumes: Task 2 decode functions; Task 1 fixtures.
- Produces:
  - `runner.py`: `Runner = Callable[[list[str], float], str]` (argv, timeout s → stdout);
    `class CommandError(Exception)` carrying `.argv` and `.message`;
    `subprocess_runner(argv, timeout) -> str`, which raises `CommandError` on non-zero exit,
    timeout, or output matching `Get failed|Not supported|Not implemented|mal-formed`.
  - `smn.py`: `ROOT_CFG = "/hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr"`;
    `TCTL = 0x59800`; `CCD = (0x59b08, 0x59b0c)`; `class SmnBusy(Exception)`;
    `read_smn(run: Runner, addrs: Sequence[int]) -> dict[int, int]`.
  - `readers.py`: frozen dataclasses `CpuIdentity(vendor: str, family: int, model: int,
    nominal_mhz: float, pcpus: int)` and `NvmeHealth(adapter: str, model: str, temp_c: int,
    spare_pct: int, used_pct: int, critical_warning: int, media_errors: int,
    wctemp_c: int | None, cctemp_c: int | None)`; functions
    `read_cpu_identity(run) -> CpuIdentity`, `read_rapl(run) -> tuple[int, int]` (unit raw,
    package energy raw), `read_perf(run, pcpu: int) -> tuple[int, int]` (APERF, MPERF),
    `read_nvme(run) -> tuple[list[NvmeHealth], list[dict]]` (drives, error entries).

- [ ] **Step 1: Write the failing tests**, using a `FakeRunner` that maps argv tuples to fixture text or exceptions and records every call in order:
  - `test_smn_reads_and_restores`: index `0x00000000`; reading `[TCTL]` returns `{0x59800: 0x7d3b0000}`. Calls are exactly: get `0x60`, set `0x60 0x59800`, get `0x64`, set `0x60 0x0`.
  - `test_smn_busy_skips_without_writes`: index reads `0x00000004` → raises `SmnBusy`, and no `set` call was made.
  - `test_smn_restores_index_on_error`: the `0x64` get raises `CommandError` → the error propagates, and the last call is set `0x60 0x0`.
  - `test_cpu_identity_from_fixture`: `cpu_list.txt` → `vendor == "AuthenticAMD"`, `family == 25`, `model == 97`, `pcpus ==` the number of `CPU:<n>` blocks in the fixture (count it when you capture it), `nominal_mhz ≈ 2495.3` (from `Core Speed: 2495312195`).
  - `test_read_rapl_from_fixture`: returns `(0xa1000, <value in msr_c001029b.txt>)`.
  - `test_vsish_error_text_is_missing_not_zero`: `subprocess_runner` on `vsish_not_supported.txt` content raises `CommandError`.
  - `test_nvme_from_fixtures`: every drive in `nvme_device_list.txt` parsed. Fields match the fixture values, and `wctemp_c`/`cctemp_c` come from `nvme_device_get_*`.
  - `test_nvme_one_drive_fails`: the SMART query for one adapter raises → the other drives are returned, and one error entry names that adapter.
- [ ] **Step 2: Run, expect FAIL** — `python3 -m pytest scripts/esx-hwhealth/tests/test_smn.py scripts/esx-hwhealth/tests/test_readers.py -q`.
- [ ] **Step 3: Implement.** `read_smn` follows the spec's SMN safety rule exactly (try/finally restore; the busy check comes before any write). The parsers read labels from the fixtures (no regex guessing beyond them). `pcpus` is the `CPU:<n>` block count. A WCTEMP/CCTEMP of `0` is `None`.
- [ ] **Step 4: Run, expect PASS** — 8 passed; the full suite is still green.
- [ ] **Step 5: Commit** — `feat(hwhealth): runner, SMN access with index restore, sensor readers`.

### Task 4: Threshold evaluation

**Files:**
- Create: `scripts/esx-hwhealth/hwhealth/evaluate.py`
- Test: `scripts/esx-hwhealth/tests/test_evaluate.py`

**Interfaces:**
- Consumes: the `unstructured` dict shape from the spec ("Report contents").
- Produces: `OK, WARNING, ERROR = 0, 1, 2`; `evaluate(u: dict) -> tuple[int, list[str]]`.

- [ ] **Step 1: Write the failing tests** (build dicts in the spec's shape):
  - `test_ok`: `tctl_c 76.1`, CCDs `[64.9, 76.5]`, one NVMe drive at 41 °C, spare 100 → `(0, [])`.
  - `test_cpu_warning_at_90` and `test_cpu_error_at_95`: Tctl `90.0` → status 1; any CCD `95.0` → status 2. The reasons name the reading.
  - `test_nvme_uses_wctemp_cctemp`: `wctemp_c 75`, `cctemp_c 85`, temp `76` → 1; temp `85` → 2.
  - `test_nvme_threshold_defaults`: no wctemp/cctemp, temp `70` → 1, `80` → 2.
  - `test_nvme_critical_warning_and_spare`: `critical_warning 1` → 2; spare `20` → 1; spare `10` → 2.
  - `test_unsupported_or_missing_cpu_warns`: `supported false` → 1; supported, but `tctl_c` and `ccd_c` absent → 1.
  - `test_status_is_max`: one WARNING and one ERROR → 2, with both reasons listed.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, with the thresholds as module constants copied from Global Constraints. The input dict is never mutated.
- [ ] **Step 4: Run, expect PASS** — 9 passed.
- [ ] **Step 5: Commit** — `feat(hwhealth): status thresholds for CPU and NVMe`.

### Task 5: Collector and plugin entrypoint

**Files:**
- Create: `scripts/esx-hwhealth/hwhealth/collect.py`, `scripts/esx-hwhealth/lab_hwhealth.py`
- Conditional (only if Task 1 Step 4 chose **sampler fallback**): `scripts/esx-hwhealth/lab_hwhealth_sampler.py`
- Test: `scripts/esx-hwhealth/tests/test_collect.py`, `test_plugin.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `collect(run: Runner, sleep=time.sleep, now=time.monotonic, wall=<utc iso str fn>, budget_s=20.0) -> dict`, which returns the spec's `unstructured` dict.
  - `lab_hwhealth.main(argv: list[str], run: Runner = subprocess_runner, plugin_cls=None) -> int`. Flags: `--stdout`, and `--from-cache PATH` (fallback only). `plugin_cls` defaults to `vmware.healthd.healthPlugin.HealthPlugin`, imported lazily.
  - Fallback only: `CACHE = "/var/run/lab-hwhealth/cache.json"`, `MAX_AGE_S = 180`, and the sampler's `run_once(run, path) -> None`, which writes atomically (temp file plus `os.replace`).

- [ ] **Step 1: Write the failing tests:**
  - `test_collect_full`: fixtures plus a scripted second RAPL/APERF reading 1 s later → `schema 1`. `cpu.supported` true, `tctl_c ≈ 76.125`, `package_w ≈ 150.47` for 150.47 J in 1.0 s, `len(eff_mhz) == pcpus`, NVMe list populated, `errors == []`.
  - `test_collect_unsupported_cpu`: family 25, model 33 → `supported false`, no `tctl_c`/`ccd_c`/`package_w`/`eff_mhz` keys, one error entry, and **no SMN or MSR call made**.
  - `test_collect_smn_busy`: index non-zero → no `tctl_c` or `ccd_c`; error reason `"smn index busy"`; the other readings are present.
  - `test_collect_respects_budget`: the fake `now` advances 21 s on the NVMe call → the result returns, and the readings not yet taken are listed with reason `"budget"`.
  - `test_plugin_stdout`: `main(["--stdout"], run=fake)` prints JSON with `structured.status` from `evaluate`, `structured.tags == ["hardware"]`, and `unstructured` from `collect`. It returns 0.
  - `test_plugin_sends_report`: a fake `plugin_cls` records `sendReport("lab.hwHealth", u, structuredData={"status": s, "magnitude": m})`.
  - Fallback only: `test_from_cache_stale` (an `mtime` older than 180 s → only an error entry `"cache stale"`, status 1) and `test_sampler_atomic_write`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `collect` takes the first RAPL and APERF/MPERF readings, sleeps 1.0 s, takes the second, then reads SMN, then NVMe.
  - It checks the budget before each step.
  - Magnitude follows `mobility_network.py`: LARGE for WARNING or ERROR, else NONE.
  - `--stdout` never imports `vmware.*`.
- [ ] **Step 4: Run, expect PASS**, then run the whole suite: `python3 -m pytest scripts/esx-hwhealth/tests -q` → all passed, 0 skipped.
- [ ] **Step 5: Run on `hyp01` by hand (read-only plus the SMN rule; SSH enabled by the owner).** Copy the package to `/tmp/lab-hwhealth/` (Python base64 transfer; no `scp`), run `python /tmp/lab-hwhealth/lab_hwhealth.py --stdout`, then remove `/tmp/lab-hwhealth`. **Owner gate first:** this is the first SMN write by the new code on a real host.
  Expected: valid JSON with `tctl_c` within ±3 °C of a fresh hand reading; index `0x0` afterwards; no new `vmkwarning` lines.
- [ ] **Step 6: Commit** — `feat(hwhealth): collector and healthd plugin entrypoint`.

### Task 6: Component packaging

**Files:**
- Create: `scripts/esx-hwhealth/packaging/build_component.py`
- Test: `scripts/esx-hwhealth/tests/test_build_component.py`

**Interfaces:**
- Consumes: Task 1 S1/S4 findings (registration method, build format); Task 5 files.
- Produces: `build(out_dir: Path, version: str) -> tuple[Path, Path]`, which returns `(vib_path, bundle_zip_path)`. The VIB is named `lab-hwhealth`, the vendor is `lab`, acceptance is `community`, and the payload installs to the plugin path from S2.

- [ ] **Step 1: Write the failing tests:** the built VIB is an `ar` archive containing `descriptor.xml` (name `lab-hwhealth`, the version passed in, acceptance `community`) and a payload whose file list is exactly the plugin, the package and the registration artefact S1 chose. The bundle zip holds the metadata files S4 proved. Building twice with the same version gives identical payload file lists.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with the format details from `spike-findings.md`. No tool is downloaded at build time unless S4 recorded it with a pinned version.
- [ ] **Step 4: Run, expect PASS**, then prove it on the nested host: apply the bundle, `esxcli system health report get -r lab.hwHealth` shows the report (with `supported false` on virtual hardware), remove it, the report is gone.
- [ ] **Step 5: Commit** — `feat(hwhealth): build the lab-hwhealth VIB and component bundle`.

### Task 7: Real-host rollout through the vLCM image

All live steps are the owner's, or the builder's after the owner says yes at that step.

**Files:**
- Create: `docs/esx-hwhealth/runbook.md` (install, verify, remove, upgrade notes, with real output pasted)

- [ ] **Step 1: Owner go/no-go gate.** Present the evidence:
  - Task 5 Step 5 output;
  - the SMN race described in the spec;
  - the acceptance change (PartnerSupported → CommunitySupported) on `hyp01`-`hyp03`;
  - Secure Boot `Disabled`, re-checked on all three hosts with `secureBoot.py -s`.
  Expected: an explicit yes, recorded in the runbook.
- [ ] **Step 2: Baseline (read-only):** vLCM image compliance, vSAN health, and NSX transport-node state for all three hosts.
- [ ] **Step 3: Owner:** upload the bundle to the depot and add the component to the cluster image. vLCM can stage it on a host in maintenance mode first (`hyp01`, no VMs); remediate that host only.
  Expected: `hyp01` compliant, `lab.hwHealth` present, `vmkernel.log` and `vmkwarning.log` clean for 30 minutes.
- [ ] **Step 4: Owner:** remediate `hyp02`, then `hyp03`, one at a time, in maintenance mode.
  Expected: all compliant; each host's report has `tctl_c` and both CCDs, and status 0 at idle.
- [ ] **Step 5: Verify the remote API recipe** (from S3) against all three hosts from the workstation. Credentials by key name; the output is the report, not secrets.
- [ ] **Step 6: Commit** — `docs(hwhealth): rollout runbook with verification output`.

### Task 8: VCF Operations pack, dashboard and email alerts

**Files:**
- Create: `docs/esx-hwhealth/ops-pack/README.md` (field → metric mapping, the steps the owner clicked); the pack export goes in the same folder.

- [ ] **Step 1: Write the field mapping**, one row per metric, from the spec's report contents: `cpu.tctl_c` → `Hardware Health|CPU|Tctl (°C)`, `cpu.ccd_c[n]` → `…|CCD n (°C)`, `cpu.package_w`, `cpu.eff_mhz_summary.*`, `nvme[*]` keyed by `adapter`, plus `structured.status`. Each row says which ESX Host object it attaches to (matched by host name).
- [ ] **Step 2: Owner, in Management Pack Builder:**
  - Build the pack from the S3 auth recipe and the mapping, with a 5-minute collection interval.
  - Export it to `ops-pack/`.
  Expected: all three hosts show the metrics within 15 minutes.
- [ ] **Step 3: Owner: dashboard and alerts.**
  - Dashboard "Lab hardware health": Tctl and CCD per host over time, package W, NVMe temperature.
  - Alert definitions on the Global Constraints thresholds.
  - The email outbound plugin, using the owner's SMTP relay and recipient; any secret goes in `credentials.env` under a named key.
  Expected: a test notification arrives.
- [ ] **Step 4: Alert proof (owner present):** temporarily lower the WARNING threshold below the current Tctl on one host, confirm the email arrives, restore the threshold.
- [ ] **Step 5: Commit** — `docs(hwhealth): Operations pack export, dashboard and alerts`.

### Task 9: Repo docs and checks

**Files:**
- Modify: `AGENTS.md` (Checks: add `python3 -m pytest scripts/esx-hwhealth/tests -q`)
- Modify: `docs/vcf-deployment-reference.md` (hosts section: `lab-hwhealth` component, report name, runbook link)

- [ ] **Step 1: Edit both files** as above.
- [ ] **Step 2: Run the full suites:** the AGENTS.md test commands plus the new one. Expected: all pass, with the skip count shown.
- [ ] **Step 3: Commit** — `docs: record the lab-hwhealth component and its test command`.
