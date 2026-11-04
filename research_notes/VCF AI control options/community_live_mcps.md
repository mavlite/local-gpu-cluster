# Community live-infrastructure MCP servers for VCF 9.x / vSphere / NSX / SDDC Manager (as of 2026-10-22)

Method note: every repo below was shallow-cloned on 2026-10-22 and inspected directly (README, pyproject/package.json, source greps, `git log`). Stars, licences and dates come from the GitHub API (`gh api repos/<r>`) the same day. "Commits" is `git rev-list --count HEAD` on the default branch. Discovery: `gh search repos` for "vcf mcp", "vsphere mcp", "vcenter mcp", "nsx mcp", "esxi mcp", "vmware mcp", "sddc manager mcp", "pyvmomi mcp", "govc mcp" and "vcf operations mcp", plus the official MCP registry API, PyPI JSON API and Glama. Bioinformatics "VCF" (Variant Call Format) repos were excluded: NahButch/vcf_mcp, jda/vcf_mcp_server, and registry entry io.github.sadri-dridi/vcf-ok (vCard).

## Q1. Inventory and credibility: licence, stars, commits, activity, maintainer, AI-dump signs

### Takeaway
Only five projects target VCF beyond plain vCenter: NiranEC77/vcf-mcp, giulianoberteo/vcf-mcp, sentania-labs/vcf-mcp, the vmware-skills family, and in a weaker sense veg-salad/vcenter_mcp. Three of those five are personal projects by Broadcom employees. None is an official Broadcom product. Every project has fewer than 100 stars. The VCF-specific ones have 0–3 stars, and two of them (NiranEC77, giulianoberteo) have no licence file. About a third of the vSphere-only servers are one-day or one-commit dumps.

### Cited Findings

**VCF-scoped (multi-appliance) projects**
- **NiranEC77/vcf-mcp**: 1 star, 1 fork, **no licence** (GitHub API `license: null`, and no LICENSE file in the tree). Created 2026-08-10, last push 2026-10-14. 11 commits on main between 2026-08-10 and 2026-10-14, by "NiranEC77" and "Number One". Version 0.3.1. Not published on PyPI; install is `uvx --from git+https://github.com/...` — [repo](https://github.com/NiranEC77/vcf-mcp), [pyproject](https://github.com/NiranEC77/vcf-mcp/blob/main/pyproject.toml)
  - Maintainer profile: Niran Evenchen, blog niran.ai, no company listed — [GitHub user](https://github.com/NiranEC77). A web search for the name plus the project found nothing.
  - Recent commits: 2026-10-12 "Add vcf_vm_disk_latency", 2026-10-12 "vcf_list_alerts: active alerts only", 2026-09-20 "HTTP mode: OAuth resource server that accepts gateway job scopes" — [commits](https://github.com/NiranEC77/vcf-mcp/commits/main)
- **giulianoberteo/vcf-mcp**: 3 stars, 0 forks, **no licence**. Created 2026-07-02, last push 2026-09-29. 24 commits, all by Giuliano Bertello: a burst on 2026-07-02/03, then 2026-09-17 and 2026-09-29. pyproject says version 0.4.4 while ChangeLog.md says 0.4.7. No tests — [repo](https://github.com/giulianoberteo/vcf-mcp), [ChangeLog](https://github.com/giulianoberteo/vcf-mcp/blob/main/ChangeLog.md)
  - Maintainer: profile reads "Principal TAM @ Broadcom // Staff Customer Success Architect @ VMware", company @Broadcom, blog.bertello.org. This confirms the earlier "Broadcom TAM" claim — [GitHub user](https://github.com/giulianoberteo)
  - The author calls it a prototype for personal lab use: the 0.4.5 changelog says the project "is currently a prototype for personal lab use — not yet hardened for production credentials" — [ChangeLog](https://github.com/giulianoberteo/vcf-mcp/blob/main/ChangeLog.md)
- **sentania-labs/vcf-mcp**: 0 stars, **no licence**. Created 2026-08-24 (history starts 2026-07-20). 40 commits on main (120 across all branches). The last human commit on main is 2026-09-15; later commits are dependabot only (pyjwt on 2026-10-02, mcp 1.30.0 on 2026-10-19). There are 19 feature branches (`fm/...`), 25 test files and 27 ADRs in docs/decisions — [repo](https://github.com/sentania-labs/vcf-mcp), [decisions](https://github.com/sentania-labs/vcf-mcp/tree/main/docs/decisions)
  - Maintainer: Scott R Bowe ("sentania"), "Cloud Management SA @ Broadcom", blog cloudsandunicorns.com. The org bio says it exists "to use a shared runner architecture for my lab" — [user](https://github.com/sentania), [org](https://github.com/sentania-labs)
  - README: "This is an unofficial personal-lab project. It is not a Broadcom or VMware offering." — [README](https://github.com/sentania-labs/vcf-mcp/blob/main/README.md)
  - Agent-built: ADR 007 records "Workers dispatched: claude-worker, codex-worker, agy-worker" and an "Orchestrator run" id. Commits are prefixed "no-mistakes(review)" — [ADR 007](https://github.com/sentania-labs/vcf-mcp/blob/main/docs/decisions/007-mutation-gate-generalization.md)
- **vmware-skills family** (VMware-AIops, -Monitor, -Storage, -NSX, -NSX-Security, -Aria, -AVI, -VKS, -Harden, -Pilot, -Debug, -Log-Insight, -VDI, -PrivateAI, -Policy):
  - VMware-AIops: 79 stars, MIT, 362 commits (2026-02-25 to 2026-10-19), 80 test files — [repo](https://github.com/vmware-skills/VMware-AIops)
  - VMware-NSX: 5 stars, MIT, 170 commits (2026-03-26 to 2026-10-18), 35 test files — [repo](https://github.com/vmware-skills/VMware-NSX)
  - PyPI packages vmware-aiops 2.4.2, vmware-monitor 2.4.2 and vmware-nsx-mgmt 1.15.1 (all uploaded 2026-10-18/19) list Wei Zhou <wei-wz.zhou@broadcom.com> as author — [PyPI vmware-aiops](https://pypi.org/project/vmware-aiops/), [PyPI vmware-nsx-mgmt](https://pypi.org/project/vmware-nsx-mgmt/)
  - The family is published to the official MCP registry as `io.github.vmware-skills/vmware-aiops` (versions 1.8.11 to 2.4.2) and `io.github.vmware-skills/vmware-aria`, among others — [MCP registry API](https://registry.modelcontextprotocol.io/v0/servers?search=vmware)
  - The READMEs point to developer.broadcom.com for "official VMware developer tools", so the family does not present itself as official — [AIops README](https://github.com/vmware-skills/VMware-AIops/blob/main/README.md)
  - This family is already deployed read-only in this lab; see docs/vmware-skills-operator-runbook.md in this repo.
- **veg-salad/vcenter_mcp** (PyPI `vcenter-mcp` 1.0.5): MIT, 0 stars. 23 commits, all made 2026-04-10 to 2026-04-12, and nothing since. The author lists HCLTech, not Broadcom. Despite the "Broadcom VCF MCP Server" description, it only covers the vCenter REST API — [repo](https://github.com/veg-salad/vcenter_mcp), [PyPI](https://pypi.org/project/vcenter-mcp/)

**vSphere / vCenter / ESXi-only projects**
- **2501-ai/vmware-mcp**: 3 stars, **no licence**. 10 commits: nine on 2026-02-17 to 2026-02-24, then one on 2026-10-09 ("Push the image to 2501's registry, signed with the 2501 key"). The company is 2501.ai ("Autopilot AI Research Lab") — [repo](https://github.com/2501-ai/vmware-mcp)
- **bright8192/esxi-mcp-server**: the most-starred vSphere MCP. 64 stars, 28 forks, MIT, 4 commits, last commit 2025-07-01 (abandoned), 6 open issues — [repo](https://github.com/bright8192/esxi-mcp-server)
  - The PyPI package `esxi-mcp-server` 0.2.0 (uploaded 2026-03-27, author "Bright8192") lists its homepage as github.com/Haegiwa405/esxi-mcp-server. That repository now returns 404 — [PyPI](https://pypi.org/project/esxi-mcp-server/)
  - intarweb/esxi-mcp-server (2 commits on 2026-10-19) is a fork described as "esxi-mcp-server 0.2.0, runs on mcp 2" — [repo](https://github.com/intarweb/esxi-mcp-server)
- **giuliolibrando/vmware-vsphere-mcp-server**: 24 stars, **no licence**. 8 commits from 2025-10-27 to 2026-07-22, 7 open issues — [repo](https://github.com/giuliolibrando/vmware-vsphere-mcp-server)
- **InfraMCP/vsphere-mcp-server** (PyPI `vsphere-mcp-server` 0.2.0, 2026-10-05): 2 stars, MIT, 39 commits including dependabot. The PyPI author is Rory McMahon (vocus.com.au). There was a gap from 2025-10-06 to 2026-10-05 — [repo](https://github.com/InfraMCP/vsphere-mcp-server), [PyPI](https://pypi.org/project/vsphere-mcp-server/)
- **TheEvalon/vmware-vcenter-mcp** ("by iOblako"): 1 star, MIT, 5 commits all on 2026-05-04, 4 open issues. Has CI and vitest tests — [repo](https://github.com/TheEvalon/vmware-vcenter-mcp)
- **Warezloder/vSphere-MCP-Pro**: 6 stars, MIT, 9 commits from 2025-12-05 to 2025-12-23, no tests. Listed on Glama — [repo](https://github.com/Warezloder/vSphere-MCP-Pro), [Glama](https://glama.ai/mcp/servers/@Warezloder/vSphere-MCP-Pro)
- **Nginx1/esxi-mcp**: 1 star, MIT, 2 commits both on 2026-10-04. Claims 121 tests in CI, and has 13 test files — [repo](https://github.com/Nginx1/esxi-mcp), [testing.md](https://github.com/Nginx1/esxi-mcp/blob/main/docs/testing.md)
- **michaelrice/vcenter-mcp**: 0 stars, Apache-2.0, 23 commits from 2026-04-30 to 2026-06-26, 3 test files. The maintainer's profile lists company "@rackn" — [repo](https://github.com/michaelrice/vcenter-mcp), [user](https://github.com/michaelrice)
- **ISH2YU/VMware-MCP**: 0 stars, MIT, 5 commits, last push 2026-08-31. Listed on Glama — [repo](https://github.com/ISH2YU/VMware-MCP), [Glama](https://glama.ai/mcp/servers/e2k11e5lpc)
- **uldyssian-sh/vmware-vcenter-mcp**: 7 stars, MIT. 38 commits, **all on 2026-01-05** between roughly 20:00 and 21:22 +0100. Commit messages include "comprehensive security fixes and enhancements", "advanced threat detection and container security scanning" and "implement enterprise secrets management". The README claims "Multi-Tenancy", "LDAP, SAML, OAuth2, and multi-factor authentication", "HA" and "8 Core Architecture Modules" — [repo](https://github.com/uldyssian-sh/vmware-vcenter-mcp), [README](https://github.com/uldyssian-sh/vmware-vcenter-mcp/blob/main/README.md)
- **uldyssian-sh/vmware-esxi-mcp**: 18 stars, MIT. 30 commits, all on 2026-01-05 — [repo](https://github.com/uldyssian-sh/vmware-esxi-mcp)
- **adrianlizman/vmware-mcp-server**: 4 stars, MIT, **1 commit** on 2025-07-23. Claims "production-ready features", "Certificate-based authentication, RBAC integration" — [repo](https://github.com/adrianlizman/vmware-mcp-server)
- Other single-day or tiny repos (0–3 stars):
  - arjungowdal4601/vcenter_mcp: 2 commits; Glama marks it "Inactive" — [Glama](https://glama.ai/mcp/servers/g7qpn61ltx)
  - Presidio-Federal/vsphere-mcp: 1 commit, 2025-12-02 — [repo](https://github.com/Presidio-Federal/vsphere-mcp)
  - nholuongut/VMWare-MCP-server-for-LLMs: 3 commits. Its description copies 2501-ai's wording verbatim — [repo](https://github.com/nholuongut/VMWare-MCP-server-for-LLMs)
  - T-R-I-T-E-J/vmware-mcp: 21 commits, 2026-08-08 to 2026-08-14 — [repo](https://github.com/T-R-I-T-E-J/vmware-mcp)
  - futuretea/vsphere-mcp-server: 5 commits — [repo](https://github.com/futuretea/vsphere-mcp-server)
- **Out of scope (not vSphere/VCF live operations):**
  - havu0/vmware-mcp (7 stars, MIT) controls **VMware Fusion/Workstation via `vmrun`**, not vCenter — [README](https://github.com/havu0/vmware-mcp/blob/main/README.md)
  - ZacharyZcR/vmware-mcp (70 stars) also targets Workstation Pro — [repo](https://github.com/ZacharyZcR/vmware-mcp)
  - mahaker1219/vcf-mcp (MIT, 2 commits on 2026-07-29) is a documentation/Terraform RAG server per its keywords, not a live-ops server — [repo](https://github.com/mahaker1219/vcf-mcp)
  - prophesional/nsx-mcp-server covers Avi (NSX ALB) only — [repo](https://github.com/prophesional/nsx-mcp-server)

### Inferences
- "Community" VCF MCP in October 2026 mostly means personal side-projects by Broadcom staff: Bertello (TAM), Bowe (SA), and Wei Zhou (vmware-skills, Broadcom email on PyPI). NiranEC77's affiliation is unverified. None of them carries vendor support.
- The **missing licences** on NiranEC77, giulianoberteo, sentania-labs, 2501-ai and giuliolibrando mean that copying or modifying the code is legally all-rights-reserved. Running it privately is low-risk; forking or vendoring it is not cleanly permitted.
- Strong AI-generated-dump signals:
  - uldyssian-sh (both repos): 30–38 commits inside about 80 minutes, plus enterprise feature claims that a one-evening repo cannot have earned.
  - adrianlizman: a single commit claiming "production-ready".
  - nholuongut: a copied description.
- Openly agent-built but engineered with care: sentania-labs (multi-agent orchestrator ADRs) and NiranEC77 (co-committer "Number One", AI-flavoured devlog.md).
- The PyPI `esxi-mcp-server` package points to a deleted repo. Treat it as a supply-chain risk and do not install it from PyPI.

### Gaps
- NiranEC77's employer and identity could not be confirmed. No blog or LinkedIn post about the project surfaced in web search.
- No Reddit or LinkedIn launch posts were found for any VCF-scoped project. Only two search variants were tried.
- Smithery and mcp.so listings were not checked directly; the search surfaced only Glama and LobeHub.
- Download counts for the PyPI packages were not collected.

## Q2. What each exposes: APIs, tool design, VCF 9.1 compatibility evidence

### Takeaway
There are two design schools:
- **Generic spec-driven** (NiranEC77, giulianoberteo): a handful of tools — search, describe and call — over vendored OpenAPI specs, so every API operation is reachable.
- **Typed one-tool-per-operation** (sentania, vmware-skills, and all the vSphere servers).

Only NiranEC77 claims every auth scheme was verified against a live VCF 9.1 estate, and it is the only one covering SDDC Manager, NSX Policy, VCF Installer, VCF Operations, Avi and vSAN DP together. sentania explicitly says it has *not* been proven against real appliances. vmware-skills claims full vSphere/NSX 9.1 support via pyVmomi SOAP and the NSX Policy API, and this lab has confirmed it read-only against VCF 9.1.1 vCenter.

### Cited Findings
- **NiranEC77/vcf-mcp**:
  - Coverage: seven appliances (SDDC Manager, VCF Installer, vCenter, NSX, Avi, VCF Operations, vSAN Data Protection) and "8,931 API operations" — [README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - Per-target operation counts: sddc 500, installer 57, vcenter 1,367, nsx 5,182, avi 1,233, ops 527, vsan-dp 65 — [README Targets](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - Generic tools: `vcf_targets`, `vcf_search_api`, `vcf_describe_api`, `vcf_validate` ("dry-runs the spec, changes nothing"), `vcf_call`, `vcf_task`, `vcf_inventory` and `vcf_audit`.
  - Domain tools (added 2026-09-20 and 2026-10-12): VM list/get/power-state/start/stop/reset/suspend, networks/segments/gateways, datastores, alerts, ops snapshot, and VM disk latency. 23 tools in total per the README table — [README Tools](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - Specs: vendored from vmware/vcf-api-specs at commit 3949fc3 (2026-05-13), version 9.1.0.0. The Avi specs were pulled from a live controller — [SPECS-PROVENANCE / README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - VCF 9.1 evidence: "Every scheme above was verified against a live 9.1 estate." The README also lists specific 9.1 behaviours, such as `POST /v1/system/prechecks` being replaced by `/v1/system/health-summary`, deprecated `/v1/edge-clusters`, and task status `"Successful"` — [README VCF 9.1 behaviour](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - Avi credentials are fetched at auth time from SDDC Manager `GET /v1/credentials` (resourceType NSX_ALB) — [README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
- **giulianoberteo/vcf-mcp**:
  - Four tools: `list_specs`, `search_endpoints`, `get_endpoint` and `call_api` — [README](https://github.com/giulianoberteo/vcf-mcp/blob/main/README.md)
  - Specs: fleet (VCF Operations Fleet Management, 106 ops), vcf-ops (370 ops) and sddc (375 ops), for 851 total — [README](https://github.com/giulianoberteo/vcf-mcp/blob/main/README.md)
  - The 2026-09-29 commit "Add vcenter and vi-json YAML specs" adds vCenter REST and VI/JSON. The README still says "three VCF API specs" — [commits](https://github.com/giulianoberteo/vcf-mcp/commits/main), [config/settings.py](https://github.com/giulianoberteo/vcf-mcp/blob/main/config/settings.py)
  - No NSX coverage.
  - The changelog says changes were "verified live against lab VCF instance", but no VCF version is stated. Screenshots show live VCF Ops alerts, SDDC network pools, credential-expiry audits and an SDDC backup task — [ChangeLog](https://github.com/giulianoberteo/vcf-mcp/blob/main/ChangeLog.md)
- **sentania-labs/vcf-mcp**:
  - Separate Streamable HTTP endpoints per product: `/ops/mcp`, `/vcenter/mcp`, `/nsx/mcp`, `/sddc-manager/mcp`, `/ops-networks/mcp`, `/fleet-lcm/mcp`, `/sddc-lcm/mcp`, `/log-management/mcp`, `/vsan-dp/mcp`, plus `/vcf/mcp` for management info.
  - "All nine built-in product packs publish exactly 19 typed tools" — [README](https://github.com/sentania-labs/vcf-mcp/blob/main/README.md)
  - Pack inspection (src/vcf_mcp/packs/*.json): every built-in tool is a GET, or a POST that is a search or query (log searches, Ops ranged stats and alert search). Examples: `list_sddc_domains`, `list_nsx_segments`, `list_nsx_security_policies`, `list_vcenter_vms` — [packs](https://github.com/sentania-labs/vcf-mcp/tree/main/src/vcf_mcp/packs)
  - Not live-verified. The README says: "The fixture proof cannot establish ... compatibility with the real product versions and endpoint base paths in the lab ... successful TLS handshakes against the lab CA chain ... the exact response shapes" — [README Prototype boundary](https://github.com/sentania-labs/vcf-mcp/blob/main/README.md)
- **vmware-skills**:
  - VMware-AIops: "61 tools" for VM lifecycle, deployment, guest ops and clusters — [README](https://github.com/vmware-skills/VMware-AIops/blob/main/README.md)
  - VMware-NSX: "33 MCP tools (20 read-only + 13 write)" for segments, gateways, NAT, routing and IPAM — [README](https://github.com/vmware-skills/VMware-NSX/blob/main/README.md)
  - The family table lists vmware-monitor 27 tools, storage 11, vks 20, nsx-security 21, aria 28, avi 28 and harden 6 — [VMware-NSX README](https://github.com/vmware-skills/VMware-NSX/blob/main/README.md)
  - Compatibility tables: "VCF 9.1 / vSphere 9.1 ✅ Full ... pyVmomi `<10.0` ... connects via SOAP; new REST-only features ... not yet wrapped", and "NSX 9.1 Full. Policy API supported" — [AIops README](https://github.com/vmware-skills/VMware-AIops/blob/main/README.md), [NSX README](https://github.com/vmware-skills/VMware-NSX/blob/main/README.md)
  - No SDDC Manager or VCF Installer coverage was found. VCF Operations is covered via vmware-aria.
- **veg-salad/vcenter_mcp**: read-only vCenter REST `/api/*` tools for clusters, datacenters, datastores, folders, hosts, networks, resource pools, VMs and guest details. Auth is `POST /api/session` — [README](https://github.com/veg-salad/vcenter_mcp/blob/main/README.md)
- **2501-ai/vmware-mcp**: wraps the `govc` CLI (govmomi, SOAP). Typed tools cover VM lifecycle, disks, networking, snapshots, datastore, session, vSAN and tasks. It also has three meta tools, including **`govc_run` ("escape hatch for any govc command")**. Output is in TOON format — [README](https://github.com/2501-ai/vmware-mcp/blob/main/README.md)
- **TheEvalon/vmware-vcenter-mcp**: TypeScript. 89 distinct `registerTool` names were counted in src (local count). It uses the vCenter REST API plus the "VI/JSON API (`/sdk/vim25/{release}/...`, vCenter 8.0 U1+)" and covers DRS/HA and vLCM remediation. Targets vCenter 8.0 — [README](https://github.com/TheEvalon/vmware-vcenter-mcp/blob/main/README.md)
- **Nginx1/esxi-mcp**: "63 stdio tools for a single standalone ESXi host" via pyVmomi, with optional ESXi SSH. It pins pyvmomi 9.1.1.0 in requirements-lock — [README](https://github.com/Nginx1/esxi-mcp/blob/main/README.md)
- **giuliolibrando/vmware-vsphere-mcp-server**: 41 `@mcp.tool` functions using pyVmomi and REST (local count) — [repo](https://github.com/giuliolibrando/vmware-vsphere-mcp-server)
- **InfraMCP/vsphere-mcp-server**: 17 tools (local count). README features: VM list/inspect/control, hosts/datacenters/datastores, networks and VLANs, folders — [README](https://github.com/InfraMCP/vsphere-mcp-server/blob/main/README.md)
- **michaelrice/vcenter-mcp**: 5 tools — list_vms, create_vm, power_on/off and delete_vm — via pyVmomi — [README](https://github.com/michaelrice/vcenter-mcp/blob/main/README.md)
- **ISH2YU/VMware-MCP**: 27 `vsphere_*` tools via pyVmomi — [Glama](https://glama.ai/mcp/servers/e2k11e5lpc)
- **bright8192/esxi-mcp-server**: about 6 tools (authenticate, create/clone/delete VM, power on/off, list) plus a performance resource, via pyVmomi — [server.py](https://github.com/bright8192/esxi-mcp-server/blob/main/server.py)
- No community MCP server targeting **NSX Manager outside vmware-skills, NiranEC77 and sentania** was found. The "nsx mcp" search returned only an Avi load-balancer server.
- Explicit VCF 9.x claims: a grep for "VCF 9 / vSphere 9 / 9.1." found them in NiranEC77, sentania and vmware-skills, and in none of the vSphere-only servers, which target vCenter 8.0 (local grep of clones).

### Inferences
- For SDDC Manager, VCF Installer or NSX Policy writes, the only full-coverage community options are NiranEC77 (generic, everything) and giulianoberteo (SDDC Manager + Ops + Fleet + vCenter, no NSX). sentania is read-only coverage and unverified against hardware.
- vSphere-only servers built on pyVmomi SOAP will probably work against vSphere 9.1, since SOAP API version negotiation is handled by pyVmomi; vmware-skills reports this and the lab confirms it. That is not tested for each server. TypeScript and REST servers that pin vCenter 8.0 paths (TheEvalon VI/JSON `release`) may need a release-string change.
- The generic design caps context cost. The trade-off: an MCP client cannot gate individual dangerous operations by tool name, because everything goes through one `vcf_call` / `call_api`.

### Gaps
- No project publishes a test matrix against VCF 9.1.1 specifically. NiranEC77 says "9.1" without a build number.
- The tool counts for TheEvalon (89), giuliolibrando (41) and InfraMCP (17) are grep counts from source, not counts from a running server.

## Q3. Safety model: read-only modes, write gating, confirm/dry-run, audit, annotations, credentials

### Takeaway
The safety models vary enormously.

Safest designs:
- **sentania**: read-only typed packs, per-key scopes, encrypted credentials, durable audit, auth lockout, signed packs.
- **veg-salad**: GET-only policy engine, keyring credentials.
- **vmware-skills/VMware-Monitor**: no write code at all.
- **ISH2YU**: read-only default.
- **TheEvalon**: confirm/dry-run plus a `VCENTER_READ_ONLY` kill switch.
- **Nginx1**: read_only / operator / administrator profiles with MCP annotations.

**NiranEC77 has no write gate by design.** In stdio mode every session has admin capability, and the only read-only mode is HTTP with a `VCF_READ_TOKEN` bearer.

Unsafe designs:
- **bright8192**: process-global "authenticated" flag, unmaintained.
- **giuliolibrando**: unauthenticated Streamable HTTP bound to 0.0.0.0.
- **2501-ai**: `govc_run` escape hatch, no gate.

### Cited Findings
- **NiranEC77**:
  - "There is no write gate. Any operation the API allows — including `DELETE /v1/domains/{id}` and host decommission — executes immediately when the agent calls it. This is deliberate." — [README Write safety](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - Audit: every POST/PATCH/PUT/DELETE is appended to JSONL with secrets redacted, and `vcf_audit` reads it back — [README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - Annotations: `vcf_call` and the four VM power tools are annotated destructive; the rest are read-only — [README Tools](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - In source, annotations are applied only "when the installed SDK supports them" — [server.py](https://github.com/NiranEC77/vcf-mcp/blob/main/src/vcf_mcp/server.py)
  - The source comment reads "HTTP mode only: stdio has no bearer tokens and capability stays 'admin'". With read capability, non-GET/HEAD/OPTIONS calls are refused — [server.py](https://github.com/NiranEC77/vcf-mcp/blob/main/src/vcf_mcp/server.py)
  - Credentials:
    - Passwords live in a `.env` file. Several targets try `NSX_ADMIN_PASSWORD` first.
    - Tokens are held only in memory.
    - Auth is capped at 3 attempts per target to avoid SSO lockout.
    - "No tool ever returns a secret".
    - **TLS verification is off by default** (`VCF_MCP_VERIFY_TLS=1` to enforce).

    — [README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
  - OAuth mode: a scope `urn:iam:agent:intent:<job>` grants **admin** and trusts an upstream agent gateway for per-tool authorization. "a job scope opens the whole server for a direct caller" — [README HTTP mode](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
- **giulianoberteo**:
  - The 2026-09-29 commit added "optional read-only mode and response size cap to call_api". `VCF_MCP_READ_ONLY=true` refuses non-GET/HEAD/OPTIONS calls — [server.py](https://github.com/giulianoberteo/vcf-mcp/blob/main/server.py)
  - No confirm step, no dry-run, no audit log and no annotations (local grep).
  - TLS verification is off by default for every spec (`*_VERIFY_SSL` default false) — [config/settings.py](https://github.com/giulianoberteo/vcf-mcp/blob/main/config/settings.py)
  - Credentials are username/password env vars, which the author flags as plaintext in claude_desktop_config.json — [ChangeLog 0.4.5](https://github.com/giulianoberteo/vcf-mcp/blob/main/ChangeLog.md)
  - Retries once with a fresh token on 401 — [commits](https://github.com/giulianoberteo/vcf-mcp/commits/main)
- **sentania-labs** (per its README):
  - Credentials and target CAs are stored with AES-256-GCM.
  - The admin password is scrypt-hashed, with no default.
  - Sensitive changes need password re-confirmation within 5 minutes.
  - API keys are scoped by endpoint, capability and target, default to no tool scope, and revocation applies on the next request.
  - An append-only SQLite audit ledger records the pack ID, digest, version and key owner.
  - **3 consecutive auth failures lock a target**.
  - Each tool has an explicit response-field allowlist.
  - Concurrency is bounded, with 429 backoff.
  - "Unsigned install is off by default ... and refused whenever any target permits actions".
  - New targets **start with TLS verification disabled** (labelled in the UI).
  - Container run flags include `--read-only`, `--cap-drop ALL` and `no-new-privileges`.

  — [README](https://github.com/sentania-labs/vcf-mcp/blob/main/README.md)
  - Its design ADRs describe a plan/apply mutation gate with pre-apply revalidation for future actions — [ADR 007](https://github.com/sentania-labs/vcf-mcp/blob/main/docs/decisions/007-mutation-gate-generalization.md)
- **vmware-skills**:
  - vmware-monitor has "zero destructive code in the codebase" — [AIops README](https://github.com/vmware-skills/VMware-AIops/blob/main/README.md)
  - In AIops, 23 of the 43 MCP write tools preview before running with `confirm=True`. `--dry-run` and double confirmation are **CLI only**. Annotations are present (readOnlyHint/destructiveHint found in 20+ files) — [AIops README](https://github.com/vmware-skills/VMware-AIops/blob/main/README.md)
  - vmware-policy provides audit and deny rules. This lab's runbook found it is library-level, with a `VMWARE_POLICY_DISABLED=1` bypass and a no-op `change_limits`. "The real boundary is the vCenter account" — [this repo's docs/vmware-skills-operator-runbook.md]
- **veg-salad**:
  - A central policy engine allows only `GET`, allowlisted path templates and allowlisted query keys.
  - The generic POST helper is disabled.
  - Upstream error bodies are sanitised.
  - Credentials are read from the OS keyring only, with the env fallback removed.
  - Concurrency caps and rate limits apply per tool.

  — [SECURITY_POSTURE.md](https://github.com/veg-salad/vcenter_mcp/blob/main/SECURITY_POSTURE.md)
- **TheEvalon**: "Every destructive tool ... requires an explicit `confirm: true` argument. Otherwise, the tool returns a structured dry-run ... A global `VCENTER_READ_ONLY=true` kill switch refuses every destructive tool regardless of `confirm`." It also ships a read-only integration test suite — [README](https://github.com/TheEvalon/vmware-vcenter-mcp/blob/main/README.md)
- **Nginx1**:
  - `ESXI_POLICY_PROFILE` is `read_only`, `operator` (default) or `administrator`, with an audit file.
  - Read tools are annotated `readOnlyHint=True`; write tools are annotated `destructiveHint=True`.
  - File upload/import commands preview until `--execute`.

  — [README](https://github.com/Nginx1/esxi-mcp/blob/main/README.md), [tools.py](https://github.com/Nginx1/esxi-mcp/blob/main/src/esxi_mcp/tools.py)
- **ISH2YU**:
  - `VMWARE_PERMISSION_MODE` takes read-only (default), write or destructive, enforced server-side.
  - `delete_vm` requires `confirm=true` and refuses while the VM is powered on.
  - The README says vCenter roles are the real boundary.

  — [Glama listing](https://glama.ai/mcp/servers/e2k11e5lpc)
- **Warezloder**: token → role → allowed-tools RBAC, JSONL audit (stdout or `AUDIT_LOG_PATH`), `confirm=True` for destructive operations, an allowed-host list and rate limiting — [README](https://github.com/Warezloder/vSphere-MCP-Pro/blob/main/README.md)
- **giuliolibrando**:
  - Destructive tools require `confirm=True`, otherwise they return a warning — [README](https://github.com/giuliolibrando/vmware-vsphere-mcp-server/blob/main/README.md)
  - **However**, `main()` runs Streamable HTTP with `SERVER_HOST` defaulting to `"0.0.0.0"`, and no bearer or auth check was found in server.py (local grep) — [server.py](https://github.com/giuliolibrando/vmware-vsphere-mcp-server/blob/main/src/vsphere_mcp_server/server.py)
- **michaelrice**: no server-side gate. It relies on the client: "The destructive tools ... are intentionally not in the default allow-list — Claude will prompt you per call." — [README](https://github.com/michaelrice/vcenter-mcp/blob/main/README.md)
- **2501-ai**: no read-only, allow or deny logic was found in src (local grep). It exposes `govc_run` for arbitrary govc commands, which has an "output sanitizer" per a commit message — [README](https://github.com/2501-ai/vmware-mcp/blob/main/README.md), [commits](https://github.com/2501-ai/vmware-mcp/commits/main)
- **bright8192**:
  - API-key auth is a **tool** (`authenticate`) that sets a process-wide `manager.authenticated = True`. `_check_auth` then only checks that flag, so once one client authenticates, every client is authorised.
  - Destructive tools (delete_vm, etc.) have no confirm step.

  — [server.py](https://github.com/bright8192/esxi-mcp-server/blob/main/server.py)
- **uldyssian-sh / adrianlizman**: the README security claims (MFA, SAML, RBAC, "certificate-based authentication") could not be tied to working tool code. The local grep found 0 conventional MCP tool decorators in either repo.

### Inferences
- For a single operator, the decisive control in every case is the **vCenter/SSO/NSX account's RBAC**, not the MCP server. Even the best server-side gates (vmware-policy) can be bypassed by environment variables. NiranEC77's HTTP read token is the one server gate that restricts by HTTP method rather than by tool.
- Read-only adoption candidates, ranked by how hard it is to cause harm by accident:
  1. vmware-monitor (no write code)
  2. sentania built-in packs (all GET)
  3. veg-salad (GET-only allowlist)
  4. giulianoberteo with `VCF_MCP_READ_ONLY=true`
  5. NiranEC77 behind HTTP + `VCF_READ_TOKEN`
- Note that NiranEC77 in **stdio** mode cannot be made read-only except by giving it read-only appliance accounts.
- TLS verification defaults to off in NiranEC77, giulianoberteo and sentania (new targets). That makes them vulnerable to MITM on an untrusted network, but the lab has its own CA (knowledgeondemand-LabRoot-CA), so verification can be turned on.

### Gaps
- None of the gates were tested at runtime here. The findings come from READMEs and source reading only.
- It was not verified whether NiranEC77's `vcf_validate` calls appliance-side validation endpoints, which would be writes on some APIs, or only checks locally against the spec.
- The README says 36 tests, but the local grep found 64 `def test` in tests/. Possibly the README is stale; not reconciled.

## Q4. Transport, auth, dependencies, footprint

### Takeaway
Most servers are stdio Python processes using pyVmomi or httpx with small dependency sets. sentania is a heavier container appliance with an admin UI. NiranEC77 offers both stdio and authenticated Streamable HTTP. NiranEC77 requires `mcp>=2.0.0`; sentania and vmware-skills cap at `mcp<2`. The lab already had an outage when mcp 2.0.0 broke mcp-sdg, so this split matters.

### Cited Findings
- **NiranEC77**:
  - stdio by default. Streamable HTTP at `/mcp` when `$PORT` is set or with `vcf-mcp serve-http`. HTTP "refuses to start with no bearer configured".
  - Deps: `mcp>=2.0.0`, `httpx>=0.27`, `PyYAML>=6.0`. The `[http]` extra adds uvicorn, starlette and PyJWT[crypto].
  - Specs ship inside the wheel. The index takes about 8 s and is then cached.

  — [pyproject](https://github.com/NiranEC77/vcf-mcp/blob/main/pyproject.toml), [README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
- **giulianoberteo**: stdio, with deps `mcp>=1.0.0`, `httpx>=0.27.0`, `pyyaml>=6.0`. Auth: Basic for Fleet (per Broadcom KB 409715), OpsToken exchange for VCF Ops, bearer from username/password for SDDC Manager, and a vCenter session — [pyproject](https://github.com/giulianoberteo/vcf-mcp/blob/main/pyproject.toml), [README](https://github.com/giulianoberteo/vcf-mcp/blob/main/README.md)
- **sentania**:
  - A Docker container serving Streamable HTTP endpoints on port 8000, with a bearer API key per client. Needs three volumes (`/data`, `/keys`, `/audit`) and Python ≥3.12.
  - Deps: `mcp>=1.2.0,<2` ("mcp 2.x removed mcp.server.fastmcp ... Raising the cap is a framework migration"), starlette, uvicorn, jinja2, python-multipart, cryptography, httpx, itsdangerous. Pack signature checks use cosign.

  — [pyproject](https://github.com/sentania-labs/vcf-mcp/blob/main/pyproject.toml), [README](https://github.com/sentania-labs/vcf-mcp/blob/main/README.md)
- **vmware-skills AIops**: `pyvmomi>=8.0.3.0,<10.0` and `mcp[cli]>=1.30,<2.0`. Ships a Dockerfile, smithery.yaml and server.json. stdio MCP — [pyproject](https://github.com/vmware-skills/VMware-AIops/blob/main/pyproject.toml)
- **veg-salad**: stdio, managed by VS Code, with `mcp[cli]>=1.6.0` and `pyvmomi>=9.0.0.0`. The setup wizard writes `.vscode/mcp.json` — [pyproject](https://github.com/veg-salad/vcenter_mcp/blob/main/pyproject.toml), [README](https://github.com/veg-salad/vcenter_mcp/blob/main/README.md)
- **2501-ai**: TypeScript on Bun, requires the `govc` binary in PATH. Runs as a Docker image `ghcr.io/2501-ai/vmware-mcp` over stdio, in ephemeral or persistent mode. Credentials via `GOVC_URL`/`GOVC_USERNAME`/`GOVC_PASSWORD`; `GOVC_INSECURE=1` is shown in the README — [README](https://github.com/2501-ai/vmware-mcp/blob/main/README.md)
- **TheEvalon**: Node ≥22, `@modelcontextprotocol/sdk ^1.29.0`, stdio — [README](https://github.com/TheEvalon/vmware-vcenter-mcp/blob/main/README.md)
- **Nginx1**: stdio, `mcp>=1.28,<2`, `pyvmomi>=8.0.0.1`. Not published to PyPI — [pyproject](https://github.com/Nginx1/esxi-mcp/blob/main/pyproject.toml)
- **InfraMCP**: stdio (`mcp.run()`), `mcp==1.28.1`. Credentials are cached by domain via GUI prompts and the macOS Keychain; env-var auth was added in 0.2.0 — [README](https://github.com/InfraMCP/vsphere-mcp-server/blob/main/README.md), [commits](https://github.com/InfraMCP/vsphere-mcp-server/commits/main)
- **giuliolibrando**: Streamable HTTP on 0.0.0.0:8000, `mcp==1.14.1`, pyvmomi, Docker — [server.py](https://github.com/giuliolibrando/vmware-vsphere-mcp-server/blob/main/src/vsphere_mcp_server/server.py)
- **bright8192**: SSE transport via `mcp.server.lowlevel` + `SseServerTransport`, `pyvmomi>=7.0` — [server.py](https://github.com/bright8192/esxi-mcp-server/blob/main/server.py)
- **Warezloder**: FastMCP, `mcp>=1.14.0`, Docker — [README](https://github.com/Warezloder/vSphere-MCP-Pro/blob/main/README.md)

### Inferences
- Of all these, only NiranEC77 runs on the mcp 2.x SDK. Given this lab's earlier mcp 2.0.0 outage, any adoption should pin `mcp` exactly and install into its own venv, with one venv per tool as already done on CT 190.
- sentania is the only one that fits the "service in an LXC behind an authenticated HTTP endpoint" pattern out of the box. It needs Docker inside an LXC (nesting) or a port to bare Python, and its maturity is "prototype".
- NiranEC77's HTTP mode with `VCF_READ_TOKEN` could run on CT 190 `lab-svc` as a shared read-only endpoint for opencode, similar to the existing vmware-skills deployment.

### Gaps
- Footprint figures (RAM, disk) were not measured for any server. The lab runbook notes that three vmware-skills venvs need about 2 GB of disk.

## Q5. Suitability for a single-operator homelab (synthesis of the above)

### Takeaway
For live VCF 9.1 work in this lab, reasonable choices are:
- **Keep vmware-skills/vmware-monitor read-only** (already deployed; best-maintained; MIT).
- If SDDC Manager, NSX or Installer reach is needed, add **NiranEC77/vcf-mcp** in HTTP mode with only `VCF_READ_TOKEN` configured, or in stdio with read-only appliance accounts. It is the only full-fleet server live-verified on 9.1, but it has no licence and no write gate.
- **giulianoberteo** with `VCF_MCP_READ_ONLY=true` is a simpler, smaller alternative for SDDC Manager + Ops + Fleet.
- **sentania** has the best governance design but is unverified against real appliances and read-only in its built-ins.

Avoid bright8192 (and its PyPI package), giuliolibrando, the uldyssian-sh repos, adrianlizman, and 2501-ai's `govc_run` with a writable account.

### Cited Findings
- NiranEC77 is the only server stating live verification on VCF 9.1 across all seven appliances — [README](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
- NiranEC77 explicitly lacks a write gate — [README Write safety](https://github.com/NiranEC77/vcf-mcp/blob/main/README.md)
- giulianoberteo added an env-var read-only switch on 2026-09-29 — [commits](https://github.com/giulianoberteo/vcf-mcp/commits/main)
- sentania's built-in packs are all read operations, and it is not proven on real appliances — [packs](https://github.com/sentania-labs/vcf-mcp/tree/main/src/vcf_mcp/packs), [README](https://github.com/sentania-labs/vcf-mcp/blob/main/README.md)
- vmware-skills has a large commit count (AIops 362, NSX 170), MIT licence, PyPI releases within the last week, and MCP-registry publication — [AIops](https://github.com/vmware-skills/VMware-AIops), [MCP registry](https://registry.modelcontextprotocol.io/v0/servers?search=vmware)
- The bright8192 global auth flag and the giuliolibrando unauthenticated 0.0.0.0 HTTP listener are covered in Q3 — [bright8192 server.py](https://github.com/bright8192/esxi-mcp-server/blob/main/server.py), [giuliolibrando server.py](https://github.com/giuliolibrando/vmware-vsphere-mcp-server/blob/main/src/vsphere_mcp_server/server.py)

### Inferences
- Activity as of 2026-10-22:
  - **Active** (commits in the last 30 days): NiranEC77 (2026-10-14), the vmware-skills family (2026-10-18/19), sentania (dependabot only since 2026-09-15), InfraMCP (2026-10-05), 2501-ai (2026-10-09), and Nginx1 (2026-10-04, but only 2 commits ever).
  - **Stale**: giulianoberteo (2026-09-29), veg-salad (April 2026), TheEvalon (May 2026), giuliolibrando (July 2026), michaelrice (June 2026), and Warezloder (December 2025).
  - **Abandoned**: bright8192 (July 2025) and adrianlizman (July 2025).
- If NiranEC77 is ever used for writes:
  - wrap `client.request()`, which the README calls "the single chokepoint", with a method/path allowlist;
  - turn on `VCF_MCP_VERIFY_TLS=1` against the lab CA;
  - keep the JSONL audit;
  - note the README's warning that `DELETE /v1/domains/{id}` executes immediately.
- Because NiranEC77, giulianoberteo and sentania have no licence, a local patched fork for private use is the practical limit. Publishing modifications is not cleanly permitted.

### Gaps
- No server was installed or run against the lab in this research pass. The safety-gate behaviour is taken from documentation and source, not from execution. The repo's own memory says to settle such questions by running them.
- No security advisories or CVEs were found for any of these projects, but no dedicated advisory search was done.
