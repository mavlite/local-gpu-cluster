# VCF DNS pre-flight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the three stdlib-only DNS pre-flight checks rev 4 specifies — a VSP-scoped `.local` warning, a resolver vantage-point note, and round-trip probing of the appliance names — plus the `--fail-on` exit-code flag.

**Architecture:** Two rule additions in `vcfspec/rules/platform.py` (static, pure), two probe additions in `vcfspec/validate/probes.py` (behind the existing injected resolver seam), one new CLI flag. No new dependency: every check uses `socket` and `pathlib` from the standard library.

**Tech Stack:** Python 3.12+, pytest, PyYAML (`safe_load` only). No dnspython. No new third-party package.

**Spec:** `docs/superpowers/specs/2026-09-20-vcf-dns-preflight-design.md` (rev 4)

## Global Constraints

Copied verbatim from the spec and the project's standing rules:

- **No new dependency.** Every addition uses the Python standard library. dnspython is explicitly deferred.
- **Existing codes keep their catalogue severities.** In particular `VCF-PROBE-NO-REVERSE-DNS` stays `error`. Re-severitying an existing code flips `Result.valid` for every existing user and is out of scope.
- **Probe containment is unchanged and fails closed.** `ProbeConfig.permits()` gates every address before any connect; `permits_name()` gates every name before any forward lookup; an empty allowlist permits nothing.
- **No address that the operator did not declare is ever connected to.**
- **The MCP server performs no network I/O** and must not gain a probe path.
- **Rules run on schema-invalid documents.** Every lookup is defensive: a wrong-typed section is skipped, never raised on. Never infer a field's shape by inspecting its value (e.g. "does it contain a dot") — dispatch on the field name.
- **No network or filesystem access at import time**, and never as a default-argument value (both evaluate once, at module load).
- **Commits carry no `Co-Authored-By` trailer.**
- **Mutation testing:** revert a mutation with a matched `Edit`, never `git checkout --`, `git restore` or `git stash`.
- Every new code needs a `catalogue.yaml` entry with `severity`, `source`, `source_url`, `summary` and `fix`, or `tests/test_catalogue_coverage.py` fails.

---

### Task 1: `VCF-NAME-VSP-LOCAL-SUFFIX` — the `.local` rule, scoped to VSP

**Files:**
- Modify: `vcf-spec-tools/vcfspec/rules/catalogue.yaml` (add one entry)
- Modify: `vcf-spec-tools/vcfspec/rules/platform.py:32-66`
- Test: `vcf-spec-tools/tests/test_rules_platform.py`

**Interfaces:**
- Consumes: `finding_for(code, pointer, **kwargs)` from `vcfspec/rules/__init__.py`; `_mapping`, `_sequence` already in `platform.py`.
- Produces: `_named_values(inventory)` gains a `/dns/subdomain` entry. `_naming_rules` gains a `value.lower() == domain` guard.

**Background the implementer needs.** VMware's split-domain post says core components (vCenter, NSX, SDDC Manager, VCF Operations) **still allow** `.local`; only VCF Identity Broker, VCF Automation and vSphere Supervisor lost it, and those three run on the VSP platform. So this rule fires on `dns.subdomain` and the two VSP FQDNs **only**. Flagging vCenter, NSX, SDDC Manager or `hosts[].name` would reject a supported design — that is the defect this rule is a correction of, not a nicety.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rules_platform.py
def test_local_suffix_on_vsp_platform_fqdn_is_a_warning(make_inventory):
    doc = make_inventory(**{"appliances.vsp.platformFqdn": "vcf-vsp.lab.local"})
    result = check_platform(doc)
    hits = [f for f in result.findings if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]
    assert [f.path for f in hits] == ["/appliances/vsp/platformFqdn"]
    assert hits[0].severity is Severity.WARNING


def test_local_subdomain_is_reported_once_at_its_source(make_inventory):
    doc = make_inventory(**{"dns.subdomain": "vcf.lab.local"})
    doc["appliances"]["vsp"]["platformFqdn"] = "vcf-vsp.vcf.lab.local"
    doc["appliances"]["vsp"]["instanceFqdn"] = "vcf-vsp-i.vcf.lab.local"
    hits = [f for f in check_platform(doc).findings
            if f.code == "VCF-NAME-VSP-LOCAL-SUFFIX"]
    # One finding, at the one-line fix -- not three naming the symptom.
    assert [f.path for f in hits] == ["/dns/subdomain"]


def test_local_is_not_flagged_on_the_components_that_still_allow_it(make_inventory):
    doc = make_inventory(**{"dns.subdomain": "vcf.lab.example.net"})
    doc["appliances"]["vcenter"]["hostname"] = "vcenter.corp.local"
    doc["appliances"]["sddcManager"]["hostname"] = "sddc.corp.local"
    doc["nsx"]["vipFqdn"] = "nsx.corp.local"
    doc["hosts"][0]["name"] = "esx01.corp.local"
    codes = check_platform(doc).codes
    assert "VCF-NAME-VSP-LOCAL-SUFFIX" not in codes


def test_sso_domain_is_exempt(make_inventory):
    # vsphere.local is an identity namespace, not a DNS domain, and is the
    # correct value. A rule that flagged it would be telling the operator to
    # break a working deployment.
    doc = make_inventory(**{"appliances.vcenter.ssoDomain": "vsphere.local"})
    assert "VCF-NAME-VSP-LOCAL-SUFFIX" not in check_platform(doc).codes


def test_subdomain_itself_is_not_reported_as_the_wrong_domain(make_inventory):
    # Adding /dns/subdomain to _named_values() puts the subdomain through
    # the WRONG-DOMAIN check, where `value.endswith("." + domain)` is false
    # for value == domain. Guard it, or every document gains a finding.
    doc = make_inventory(**{"dns.subdomain": "vcf.lab.example.net"})
    wrong = [f for f in check_platform(doc).findings
             if f.code == "VCF-NAME-WRONG-DOMAIN"]
    assert [f.path for f in wrong if f.path == "/dns/subdomain"] == []


def test_uppercase_subdomain_is_still_caught(make_inventory):
    doc = make_inventory(**{"dns.subdomain": "VCF.lab.example.net"})
    lower = [f for f in check_platform(doc).findings
             if f.code == "VCF-NAME-NOT-LOWERCASE" and f.path == "/dns/subdomain"]
    assert len(lower) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_rules_platform.py -k "local or subdomain" -v`
Expected: FAIL — `VCF-NAME-VSP-LOCAL-SUFFIX` is not a known code, and the two `subdomain` tests fail because `/dns/subdomain` is not in `_named_values()` yet.

- [ ] **Step 3: Add the catalogue entry**

```yaml
VCF-NAME-VSP-LOCAL-SUFFIX:
  severity: warning
  source: docs
  source_url: https://blogs.vmware.com/cloud-foundation/2026/04/17/bridging-the-local-gap-a-split-domain-design-for-vmware-cloud-foundation-deployment/
  summary: "'{value}' at {where} uses the .local suffix, which the VCF Management Services platform does not support."
  fix: "Use a routable suffix for the VSP names. This is a warning, not an error: VCF Operations, vCenter, NSX and SDDC Manager still allow .local during the transition window -- only VCF Identity Broker, VCF Automation and vSphere Supervisor dropped it, and those three run on the VSP platform. Correcting dns.subdomain fixes the composed names in one edit."
```

- [ ] **Step 4: Add `/dns/subdomain` to `_named_values()` and guard the domain check**

In `_named_values`, add `("/dns/subdomain", _mapping(inventory.get("dns")).get("subdomain"))` to the head of the existing tuple sequence, so a subdomain finding sorts before the names composed from it.

In `_naming_rules`, the wrong-domain condition becomes:

```python
        if ("." in value and domain and value.lower() != domain
                and not value.lower().endswith(f".{domain}")):
```

A value equal to the declared domain is in the declared domain; without the guard, adding `/dns/subdomain` to `_named_values()` makes every document emit `VCF-NAME-WRONG-DOMAIN` against its own subdomain.

- [ ] **Step 5: Add the rule**

Add to `platform.py`, and call it from `check_platform` after `_naming_rules`:

```python
_LOCAL_SUFFIX = ".local"

# Only these carry the restriction. VCF Operations, vCenter, NSX and SDDC
# Manager still allow .local during the transition window, so a rule that
# flagged them would reject a supported design -- which is exactly what an
# earlier draft of this rule did.
_VSP_NAME_POINTERS = (
    "/dns/subdomain",
    "/appliances/vsp/platformFqdn",
    "/appliances/vsp/instanceFqdn",
)


def _local_suffix_rules(inventory: dict) -> list[Finding]:
    """Flag .local on the VSP names, reporting the subdomain when it is the
    source rather than each name composed from it."""
    named = dict(_named_values(inventory))
    subdomain = str(named.get("/dns/subdomain", "")).lower().rstrip(".")
    if subdomain.endswith(_LOCAL_SUFFIX):
        return [finding_for("VCF-NAME-VSP-LOCAL-SUFFIX", "/dns/subdomain",
                            value=named["/dns/subdomain"], where="/dns/subdomain")]
    out = []
    for pointer in _VSP_NAME_POINTERS:
        value = named.get(pointer)
        if isinstance(value, str) and value.lower().rstrip(".").endswith(_LOCAL_SUFFIX):
            out.append(finding_for("VCF-NAME-VSP-LOCAL-SUFFIX", pointer,
                                   value=value, where=pointer))
    return out
```

`appliances.vcenter.ssoDomain` is exempt by construction: it is not in `_named_values()` and must not be added there.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_rules_platform.py tests/test_catalogue_coverage.py -v`
Expected: PASS.

- [ ] **Step 7: Mutate to prove the tests bite**

Temporarily widen `_VSP_NAME_POINTERS` to include `/appliances/vcenter/hostname`; confirm `test_local_is_not_flagged_on_the_components_that_still_allow_it` fails. Revert with a matched `Edit` — never `git checkout`.

- [ ] **Step 8: Run the whole suite and commit**

```bash
python -m pytest -q
git add vcfspec/rules/catalogue.yaml vcfspec/rules/platform.py tests/test_rules_platform.py
git commit -m "feat(rules): warn on .local for the VSP names only"
```

---

### Task 2: `VCF-PROBE-RESOLVER-MISMATCH` — the vantage-point note

**Files:**
- Modify: `vcf-spec-tools/vcfspec/rules/catalogue.yaml`
- Modify: `vcf-spec-tools/vcfspec/validate/probes.py:146-152`
- Test: `vcf-spec-tools/tests/test_probes.py`

**Interfaces:**
- Consumes: `run_probes(inventory, config, resolver=None, connector=None)`.
- Produces: `run_probes` gains a keyword-only `resolv_conf_reader=None` parameter — a zero-argument callable returning `tuple[str, ...]` of the runner's configured resolver addresses. Later tasks add parameters to the same signature; keep them keyword-only and defaulted.

**Why this exists.** On 2026-09-21 the same name resolved correctly from the lab runner and returned nothing from the Proxmox host, whose resolver is `1.1.1.1` — which would also have published the lab's internal names to a third party. Rev 3's answer was to query the declared nameservers directly, which needs dnspython and drags in the whole containment problem. Rev 4 reports the mismatch instead.

**Severity is `info`, and the fix text must be honest.** A corporate resolver that *forwards* the lab zone trips this while resolving perfectly; `systemd-resolved` shows only `127.0.0.53` and can never match; Windows has no `resolv.conf` at all. This is a note about where the answers came from, not a defect.

- [ ] **Step 1: Write the failing tests**

```python
def test_resolver_mismatch_is_info_and_fires_once(inventory):
    inventory["dns"]["nameservers"] = ["10.50.10.5"]
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    hits = [f for f in result.findings if f.code == "VCF-PROBE-RESOLVER-MISMATCH"]
    assert len(hits) == 1                      # once per run, not once per host
    assert hits[0].severity is Severity.INFO
    assert hits[0].path == "/dns/nameservers"


def test_no_mismatch_when_a_declared_nameserver_is_in_use(inventory):
    inventory["dns"]["nameservers"] = ["10.50.10.5", "10.50.10.6"]
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("10.50.10.6",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes


def test_unreadable_resolv_conf_is_unknown_not_mismatch(inventory):
    def boom():
        raise OSError("no such file")
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True, resolv_conf_reader=boom)
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes
    assert "VCF-PROBE-UNKNOWN" in result.codes


def test_no_declared_nameservers_means_nothing_to_compare(inventory):
    inventory["dns"].pop("nameservers", None)
    result = run_probes(inventory, CONFIG, resolver=all_good(inventory),
                        connector=lambda *_: True,
                        resolv_conf_reader=lambda: ("1.1.1.1",))
    assert "VCF-PROBE-RESOLVER-MISMATCH" not in result.codes


def test_the_reader_is_not_built_at_import_or_as_a_default(monkeypatch):
    # The real reader touches the filesystem. Importing this module, and
    # calling run_probes with probes that never need it, must not.
    import builtins
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: (opened.append(a[0]), real_open(*a, **k))[1])
    import importlib
    import vcfspec.validate.probes as probes_module
    importlib.reload(probes_module)
    assert not any("resolv.conf" in str(p) for p in opened)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_probes.py -k resolver -v`
Expected: FAIL — `run_probes()` got an unexpected keyword argument `resolv_conf_reader`.

- [ ] **Step 3: Add the catalogue entry**

```yaml
VCF-PROBE-RESOLVER-MISMATCH:
  severity: info
  source: docs
  source_url: ""
  summary: "Probe answers came from {runner}, which is none of the declared nameservers ({declared})."
  fix: "This is not necessarily wrong -- a forwarder can resolve the lab zone correctly, systemd-resolved reports only 127.0.0.53, and Windows has no resolv.conf at all. It tells you the answers above were not obtained from the DNS the deployment will use. Re-run from the management network to be certain."
```

- [ ] **Step 4: Implement the reader and the check**

```python
_RESOLV_CONF = "/etc/resolv.conf"


def _default_resolv_conf_reader() -> tuple[str, ...]:
    """The runner's configured resolver addresses, in file order.

    Raises OSError when the file is absent or unreadable (Windows, a
    minimal container) -- the caller turns that into VCF-PROBE-UNKNOWN.
    """
    found = []
    with open(_RESOLV_CONF, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                found.append(parts[1])
    return tuple(found)


def _resolver_vantage_point(inventory: dict, reader) -> list[Finding]:
    declared = [str(x) for x in (_mapping(inventory.get("dns")).get("nameservers") or [])
                if isinstance(x, str)]
    if not declared:
        return []          # nothing declared, nothing to compare against
    try:
        runner = reader()
    except OSError as exc:
        return [finding_for("VCF-PROBE-UNKNOWN", "/dns/nameservers",
                            target=_RESOLV_CONF,
                            reason=f"resolver configuration unreadable ({type(exc).__name__})")]
    if not runner or set(runner) & set(declared):
        return []
    return [finding_for("VCF-PROBE-RESOLVER-MISMATCH", "/dns/nameservers",
                        runner=", ".join(runner), declared=", ".join(declared))]
```

Note `type(exc).__name__`, never `str(exc)`: this package does not echo exception text, because `redact()` is a pattern masker and not a sanitizer, and an OSError message carries a filesystem path.

Import `as_mapping as _mapping` from `..rules.coerce` at the top of `probes.py`.

In `run_probes`, take the new parameter and build the reader lazily — never at import, never as a default-argument value:

```python
def run_probes(inventory: dict, config: ProbeConfig, resolver=None,
               connector=None, *, resolv_conf_reader=None) -> Result:
    resolve = resolver or _default_resolver
    connect = connector or _default_connector
    read_resolvers = resolv_conf_reader or _default_resolv_conf_reader
```

Emit the vantage-point findings once, after the host loop and before the `NOTHING-PERMITTED` check.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_probes.py tests/test_catalogue_coverage.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vcfspec/rules/catalogue.yaml vcfspec/validate/probes.py tests/test_probes.py
git commit -m "feat(probes): report the resolver vantage point instead of querying it"
```

---

### Task 3: Probe the appliance names

**Files:**
- Modify: `vcf-spec-tools/vcfspec/validate/probes.py`
- Test: `vcf-spec-tools/tests/test_probes.py`

**Interfaces:**
- Consumes: `ProbeConfig.permits`, `ProbeConfig.permits_name`, `_bounded_resolve`, `_dns_name`, and the `resolv_conf_reader` seam from Task 2.
- Produces: `_default_resolver(name, want_reverse=False)` gains a third mode. Change its signature to `_default_resolver(name, want_reverse=False, want_aliases=False)`; `want_aliases=True` returns `tuple[str, ...]` — the canonical name followed by its aliases, from `socket.gethostbyname_ex(name)`. Test fakes in `tests/test_probes.py` take `(name, want_reverse)` today; the loop must call the resolver in a way that existing two-argument fakes still work (pass `want_aliases` only when it is needed, or give the parameter a default and let fakes ignore it — update `resolver_for`/`all_good` accordingly).

**Four things make appliances different from hosts. All four are defects if missed.**

**(a) Gating.** A host is gated by `config.permits(mgmtIp)` before any lookup, using an address the operator declared. An appliance has **no declared address**, so that gate cannot fire and the resolved address is chosen by whoever controls the zone. So: `permits_name(fqdn)` gates the forward lookup exactly as it does for hosts, and then `config.permits(resolved)` gates the reverse lookup. A resolved address outside the allowlist yields `VCF-PROBE-TARGET-BLOCKED` and no reverse lookup.

**(b) Appliances are never connected to.** No `connect()` call for any appliance name, ever. Pre-Installer they do not exist, so a 443 probe is guaranteed noise — and it would be a connection to an address we did not choose.

**(c) Composition is by field name, never by sniffing for a dot.** Rules run on schema-invalid documents, so a value's shape proves nothing.

| Already an FQDN — use as-is | Short name — append `.{subdomain}` |
|---|---|
| `/nsx/vipFqdn` | `/appliances/vcenter/hostname` |
| `/appliances/vsp/platformFqdn` | `/appliances/sddcManager/hostname` |
| `/appliances/vsp/instanceFqdn` | `/nsx/managers/{i}` |

Composing unconditionally, as the host loop does, would query `nsx.vcf.lab.example.net.vcf.lab.example.net`.

**(d) CNAMEs.** `gethostbyname` follows a CNAME silently and `gethostbyaddr()[0]` returns the **canonical** name, so a CNAME'd appliance alias produces `VCF-PROBE-REVERSE-MISMATCH` at `error` on a perfectly valid lab. Appliance names are the ones most likely to be aliases. Accept the reverse answer when it matches the queried name **or any alias** of it, from `gethostbyname_ex`.

**PTR exemption:** `/nsx/vipFqdn` and `/appliances/vsp/platformFqdn` are both VIP-like — the VSP platform's addresses come from `vsp.poolStart..poolEnd` — so a missing PTR for either is not reported.

**Counters:** appliances must not touch `candidates`/`permitted`, so `VCF-PROBE-NOTHING-PERMITTED`'s `/hosts` message stays true.

- [ ] **Step 1: Write the failing tests**

```python
APPLIANCE_CASES = [
    ("/appliances/vcenter/hostname", "vcenter.vcf.lab.knowledgeondemand.net"),
    ("/appliances/sddcManager/hostname", "sddc.vcf.lab.knowledgeondemand.net"),
    ("/nsx/vipFqdn", "nsx.vcf.lab.knowledgeondemand.net"),
]


def test_short_names_are_composed_and_fqdns_are_not(inventory):
    asked = []

    def resolver(name, want_reverse=False, want_aliases=False):
        asked.append(name)
        return None

    run_probes(inventory, CONFIG, resolver=resolver, connector=lambda *_: True,
               resolv_conf_reader=lambda: ())
    # The NSX VIP is already an FQDN. Composing it again is the bug.
    assert "nsx.vcf.lab.knowledgeondemand.net" in asked
    assert not any(n.count("vcf.lab.knowledgeondemand.net") > 1 for n in asked)


def test_an_appliance_is_never_connected_to(inventory):
    contacted = []
    run_probes(inventory, CONFIG, resolver=all_good_with_appliances(inventory),
               connector=lambda host, port, timeout: (contacted.append(host), True)[1],
               resolv_conf_reader=lambda: ())
    host_ips = {h["mgmtIp"] for h in inventory["hosts"]}
    assert set(contacted) <= host_ips


def test_an_appliance_resolving_outside_the_allowlist_is_blocked(inventory):
    # The zone -- not the operator -- chose this address. It must not be
    # reverse-resolved and must not be connected to.
    reversed_names, contacted = [], []

    def resolver(name, want_reverse=False, want_aliases=False):
        if want_reverse:
            reversed_names.append(name)
            return None
        return "203.0.113.9"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda h, p, t: (contacted.append(h), True)[1],
                        resolv_conf_reader=lambda: ())
    assert "203.0.113.9" not in reversed_names
    assert "203.0.113.9" not in contacted
    assert "VCF-PROBE-TARGET-BLOCKED" in result.codes


def test_a_cname_alias_is_not_a_reverse_mismatch(inventory):
    # gethostbyaddr returns the CANONICAL name. Without alias handling this
    # flags a valid lab at `error` and fails the whole document.
    def resolver(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return ("vcenter-real.vcf.lab.knowledgeondemand.net", name)
        if want_reverse:
            return "vcenter-real.vcf.lab.knowledgeondemand.net"
        return "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    mism = [f for f in result.findings if f.code == "VCF-PROBE-REVERSE-MISMATCH"
            and f.path.startswith("/appliances")]
    assert mism == []


def test_a_genuine_reverse_mismatch_on_an_appliance_is_still_reported(inventory):
    def resolver(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return (name,)
        if want_reverse:
            return "someone-else.vcf.lab.knowledgeondemand.net"
        return "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    assert any(f.code == "VCF-PROBE-REVERSE-MISMATCH" and f.path.startswith("/appliances")
               for f in result.findings)


def test_vip_like_names_are_exempt_from_requiring_a_ptr(inventory):
    def resolver(name, want_reverse=False, want_aliases=False):
        if want_aliases:
            return (name,)
        return None if want_reverse else "10.50.10.40"

    result = run_probes(inventory, CONFIG, resolver=resolver,
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    no_ptr = {f.path for f in result.findings if f.code == "VCF-PROBE-NO-REVERSE-DNS"}
    assert "/nsx/vipFqdn" not in no_ptr
    assert "/appliances/vsp/platformFqdn" not in no_ptr
    assert "/appliances/vcenter/hostname" in no_ptr


def test_appliances_do_not_count_toward_nothing_permitted(inventory):
    # Every host blocked, appliances resolvable: the /hosts message must
    # still fire and still say "none of the N hosts".
    blocked = ProbeConfig(allowlist=("203.0.113.0/24",),
                          domain_allowlist=("vcf.lab.knowledgeondemand.net",))
    result = run_probes(inventory, blocked, resolver=all_good(inventory),
                        connector=lambda *_: True, resolv_conf_reader=lambda: ())
    hit = [f for f in result.findings if f.code == "VCF-PROBE-NOTHING-PERMITTED"]
    assert len(hit) == 1 and hit[0].path == "/hosts"
    assert str(len(inventory["hosts"])) in hit[0].message
```

Write `all_good_with_appliances(inventory)` next to the existing `all_good` helper: it answers forward, reverse and alias queries for every host **and** every appliance name, so a clean environment still produces zero findings. Add a `test_clean_environment_with_appliances_produces_no_findings` asserting exactly that.

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_probes.py -k "appliance or cname or vip or composed" -v`
Expected: FAIL — appliance names are not probed at all yet.

- [ ] **Step 3: Extend the default resolver**

```python
def _default_resolver(name: str, want_reverse: bool = False,
                      want_aliases: bool = False):
    try:
        if want_aliases:
            canonical, aliases, _ = socket.gethostbyname_ex(name)
            return (canonical, *aliases)
        return socket.gethostbyaddr(name)[0] if want_reverse else socket.gethostbyname(name)
    except OSError:
        return None
```

`_bounded_resolve` must pass the third argument through. Give it a `want_aliases=False` parameter and call `resolve(name, want_reverse, want_aliases)` only when `want_aliases` is true, so existing two-parameter fakes keep working:

```python
def _bounded_resolve(resolve, name, want_reverse, timeout_s, want_aliases=False):
    ...
    def worker():
        try:
            box[0] = (resolve(name, want_reverse, want_aliases) if want_aliases
                      else resolve(name, want_reverse))
        except Exception:
            box[0] = None
```

- [ ] **Step 4: Build the appliance target list**

```python
# Appliance names, by field. Never inferred from a value's shape: rules
# run on schema-invalid documents, so "it has a dot in it" proves nothing
# about whether a field holds a short name or an FQDN.
def _appliance_targets(inventory: dict, subdomain: str) -> list[tuple[str, str, bool]]:
    """(json pointer, fqdn, ptr_required) for each appliance name declared.

    ptr_required is False for the VIP-like names: the NSX VIP and the VSP
    platform name front pools, so a missing PTR is not a defect there.
    """
    appliances = _mapping(inventory.get("appliances"))
    nsx = _mapping(inventory.get("nsx"))
    vsp = _mapping(appliances.get("vsp"))
    out: list[tuple[str, str, bool]] = []

    def compose(short: object) -> str | None:
        if not isinstance(short, str) or not short.strip():
            return None
        return f"{short}.{subdomain}" if subdomain else short

    for pointer, value, ptr_required in (
        ("/appliances/vcenter/hostname",
         compose(_mapping(appliances.get("vcenter")).get("hostname")), True),
        ("/appliances/sddcManager/hostname",
         compose(_mapping(appliances.get("sddcManager")).get("hostname")), True),
    ):
        if value:
            out.append((pointer, value, ptr_required))
    for index, manager in enumerate(nsx.get("managers") or []):
        composed = compose(manager)
        if composed:
            out.append((f"/nsx/managers/{index}", composed, True))
    # Already fully qualified -- appending the subdomain again is the bug.
    for pointer, value, ptr_required in (
        ("/nsx/vipFqdn", nsx.get("vipFqdn"), False),
        ("/appliances/vsp/platformFqdn", vsp.get("platformFqdn"), False),
        ("/appliances/vsp/instanceFqdn", vsp.get("instanceFqdn"), True),
    ):
        if isinstance(value, str) and value.strip():
            out.append((pointer, value, ptr_required))
    return out
```

- [ ] **Step 5: Probe them**

Add after the host loop, before the vantage-point findings. It does **not** touch `candidates` or `permitted`:

```python
    for pointer, fqdn, ptr_required in _appliance_targets(inventory, subdomain):
        if not config.permits_name(fqdn):
            findings.append(finding_for("VCF-PROBE-NAME-BLOCKED", pointer, name=fqdn))
            continue
        resolved = _bounded_resolve(resolve, fqdn, False, config.timeout_s)
        if resolved is None:
            findings.append(finding_for("VCF-PROBE-UNKNOWN", pointer, target=fqdn,
                                        reason="no forward DNS answer"))
            continue
        # The zone chose this address, not the operator. Gate it before the
        # reverse lookup -- and never connect to an appliance at all.
        if not config.permits(resolved):
            findings.append(finding_for("VCF-PROBE-TARGET-BLOCKED", pointer,
                                        target=resolved))
            continue
        reverse = _bounded_resolve(resolve, resolved, True, config.timeout_s)
        if reverse is None:
            if ptr_required:
                findings.append(finding_for("VCF-PROBE-NO-REVERSE-DNS", pointer,
                                            ip=resolved, fqdn=fqdn))
            continue
        names = {_dns_name(fqdn)}
        aliases = _bounded_resolve(resolve, fqdn, False, config.timeout_s,
                                   want_aliases=True)
        if isinstance(aliases, (tuple, list)):
            names |= {_dns_name(a) for a in aliases}
        if _dns_name(reverse) not in names:
            findings.append(finding_for("VCF-PROBE-REVERSE-MISMATCH", pointer,
                                        ip=resolved, resolved=reverse, fqdn=fqdn))
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_probes.py -v`
Expected: PASS, including the pre-existing host tests — the host loop is unchanged.

- [ ] **Step 7: Mutate to prove the containment tests bite**

Three mutations, each reverted with a matched `Edit`:
1. Drop the `config.permits(resolved)` guard → `test_an_appliance_resolving_outside_the_allowlist_is_blocked` must fail.
2. Compose unconditionally (`f"{value}.{subdomain}"` for `/nsx/vipFqdn`) → `test_short_names_are_composed_and_fqdns_are_not` must fail.
3. Drop the alias set → `test_a_cname_alias_is_not_a_reverse_mismatch` must fail.

If any mutation leaves the suite green, the test is not testing what it claims.

- [ ] **Step 8: Run the whole suite and commit**

```bash
python -m pytest -q
git add vcfspec/validate/probes.py tests/test_probes.py
git commit -m "feat(probes): round-trip the appliance names, gated on the resolved address"
```

---

### Task 4: `--fail-on` — exit code only

**Files:**
- Modify: `vcf-spec-tools/vcfspec/cli.py`
- Test: `vcf-spec-tools/tests/test_cli.py`

**Interfaces:**
- Consumes: `Severity`, `BLOCKING` from `vcfspec/findings.py`; the `result` dict `main()` already builds.
- Produces: nothing other modules consume. `Result.valid` is untouched and the MCP surface gains nothing.

**Default is `error`.** That is today's behaviour, and `--fail-on` applies to all findings, so defaulting to `warning` would change the exit contract for every existing user of a tool whose probing is opt-in. An earlier draft had this backwards.

- [ ] **Step 1: Write the failing tests**

```python
def test_fail_on_defaults_to_error(tmp_path, capsys):
    # A document whose worst finding is a warning still exits 0 by default.
    path = _write(tmp_path, _inventory_with_a_warning())
    assert main(["validate", str(path)]) == 0


def test_fail_on_warning_makes_a_warning_exit_nonzero(tmp_path, capsys):
    path = _write(tmp_path, _inventory_with_a_warning())
    assert main(["validate", str(path), "--fail-on", "warning"]) == 1


def test_fail_on_does_not_change_the_reported_validity(tmp_path, capsys):
    path = _write(tmp_path, _inventory_with_a_warning())
    main(["validate", str(path), "--fail-on", "warning"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True      # exit code moved; the verdict did not


def test_fail_on_is_rejected_for_render(tmp_path):
    path = _write(tmp_path, _valid_inventory())
    with pytest.raises(SystemExit) as exc:
        main(["render", str(path), "--fail-on", "warning"])
    assert exc.value.code == 2
```

`_inventory_with_a_warning` builds a document that validates but carries at least one `warning` — the example inventory plus `VCF-LIC-EVALUATION` is `info`, so use a real warning such as a `VCF-NAME-WRONG-DOMAIN` trigger. Assert the chosen document's findings actually contain a `warning` and no `error`, so the test cannot pass vacuously.

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_cli.py -k fail_on -v`
Expected: FAIL — unrecognised argument `--fail-on`.

- [ ] **Step 3: Add the flag to the `validate` subparser only**

```python
    validate.add_argument(
        "--fail-on", choices=("warning", "error"), default="error",
        help="Lowest severity that makes the process exit non-zero. "
             "Default 'error', which is the historical behaviour: the "
             "exit code follows Result.valid. This changes the exit code "
             "only -- the reported `valid` field keeps its meaning, so a "
             "document that is valid still reports valid: true.")
```

Adding it to `validate` and not to `render` is what makes `render --fail-on` a usage error (argparse exits 2) — the fourth test relies on that and needs no extra code.

- [ ] **Step 4: Apply it at the exit**

Replace the final `return 0 if result["valid"] else 1` with:

```python
    if args.command == "validate" and args.fail_on == "warning":
        severities = {f.get("severity") for f in result.get("findings", ())}
        if severities & {"warning", "error", "critical"}:
            return 1
    return 0 if result["valid"] else 1
```

Read the severity out of the serialised finding dicts the CLI already holds; do not reach back into a `Result` object the CLI no longer has.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Update the module docstring**

The exit-code contract at the top of `cli.py` is documentation an operator reads. Add a line under the `1` entry recording that `--fail-on warning` widens what counts, and that it never changes the `valid` field.

- [ ] **Step 7: Run the whole suite and commit**

```bash
python -m pytest -q
git add vcfspec/cli.py tests/test_cli.py
git commit -m "feat(cli): --fail-on selects the exit-code threshold, default error"
```

---

### Task 5: Documentation and the example inventory

**Files:**
- Modify: `vcf-spec-tools/README.md`
- Test: `vcf-spec-tools/tests/test_docs.py`

**Interfaces:**
- Consumes: the four codes added above.
- Produces: nothing.

`tests/test_docs.py` already asserts the README and the catalogue agree; read it first and satisfy whatever contract it enforces rather than inventing a new section shape.

- [ ] **Step 1: Read the existing docs test**

Run: `python -m pytest tests/test_docs.py -v` and read the file. It is the spec for this task.

- [ ] **Step 2: Document the new codes and the flag**

Cover: the three new codes; that `--fail-on` changes the exit code only; and — this is the part an operator most needs — that appliance names are probed without ever being connected to, and that a resolved address outside the allowlist is refused rather than followed.

- [ ] **Step 3: Run the suite and commit**

```bash
python -m pytest -q
git add README.md
git commit -m "docs: record the DNS pre-flight checks and --fail-on"
```
