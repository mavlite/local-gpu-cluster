# IaC and Automation-Platform MCP Servers for VCF 9.x (as of 2026-10-22)

Scope: whether a VCF 9.x / vSphere / NSX lab can use an "AI writes and plans IaC, human applies" path, as an alternative or complement to MCP servers that call the VCF APIs directly. Research ran 2026-10-22 with about 15 searches and fetches. Primary sources (GitHub releases, vendor docs) are preferred. Some claims come from secondary or aggregator sources, and those are flagged.

## 1. HashiCorp Terraform MCP server (hashicorp/terraform-mcp-server)

### Takeaway
The official Terraform MCP server (MPL-2.0, v1.3.x) has two halves:
- **Registry and docs half:** provider, module and policy lookup. It works locally over stdio with no licence, and it is the useful part for a homelab.
- **Workspace and run half:** creating, planning and applying runs. This only drives **HCP Terraform or Terraform Enterprise** through `TFE_TOKEN`/`TFE_ADDRESS`. It does not wrap a local `terraform plan` or `apply`.

The run half has reasonable gating. Apply, discard, cancel, auto-approve, destroy and the delete operations all require `ENABLE_TF_OPERATIONS=true`, which defaults to false.

### Cited Findings
- **Licence, transports, version:** MPL-2.0. Two transports: stdio (the default) and StreamableHTTP on `/mcp` with a `/health` check (`TRANSPORT_MODE=streamable-http`). The README's Docker examples reference image `hashicorp/terraform-mcp-server:1.3.0`. — [GitHub README](https://github.com/hashicorp/terraform-mcp-server)
- **Toolsets:** `registry`, `registry-private`, `terraform`, `all` and `default`. `--toolsets` and `--tools` filter what is exposed and cannot be combined. — [GitHub README](https://github.com/hashicorp/terraform-mcp-server)
- **Registry tools:** `search_providers`, `get_provider_details`, `get_provider_capabilities`, `get_latest_provider_version`, `search_modules`, `get_module_details`, `get_latest_module_version`, `search_policies`, `get_policy_details`. The private-registry equivalents are `search_private_modules`, `get_private_module_details`, `search_private_providers` and `get_private_provider_details`. — [HashiCorp MCP reference](https://developer.hashicorp.com/terraform/mcp-server/reference)
- **HCP Terraform / TFE tools (`terraform` toolset):**
  - Workspaces: `list_workspaces`, `create_workspace`, `update_workspace`, `delete_workspace_safely`, `force_unlock_workspace`
  - Runs: `list_runs`, `get_run_details`, `create_run`, `action_run`, `get_plan_details`, `get_plan_logs`, `get_plan_json_output`, `get_apply_details`, `get_apply_logs`
  - State, variable sets, policy sets, teams, projects and stacks also have tools.

  — [HashiCorp MCP reference](https://developer.hashicorp.com/terraform/mcp-server/reference)
- **How runs are applied:** `create_run` creates runs. `action_run` applies, discards or cancels them. A `create_run` of type `plan_and_apply` applies only after approval, unless `auto_approve` is used, and `auto_approve` is gated. — [HashiCorp MCP reference](https://developer.hashicorp.com/terraform/mcp-server/reference)
- **Tools gated by `ENABLE_TF_OPERATIONS=true`:**
  - `action_run`
  - `delete_project`, `delete_team`, `delete_workspace_safely`
  - `force_unlock_workspace`
  - `create_run` with `auto_approve` or `is_destroy`

  — [HashiCorp MCP reference](https://developer.hashicorp.com/terraform/mcp-server/reference). The 1.3.0 changelog calls `delete_team` "a destructive operation and must set `ENABLE_TF_OPERATIONS=true`". — [CHANGELOG](https://github.com/hashicorp/terraform-mcp-server/blob/main/CHANGELOG.md)
- **Other safety controls:**
  - `MCP_ORGANIZATION_ALLOWLIST`
  - CORS controls, strict by default (`MCP_ALLOWED_ORIGINS`, `MCP_CORS_MODE`)
  - Global and per-session rate limits
  - TLS is required for non-localhost HTTP
  - In HTTP mode, clients cannot override `TFE_ADDRESS` (403)
  - Tokens passed in query strings are rejected (400)
  - Optional `TF_MCP_SHARED_SECRET`

  — [GitHub README](https://github.com/hashicorp/terraform-mcp-server)
- **Recent changelog:**
  - 1.3.1: breaking rename of `search_providers` to `search_provider_docs`; `list_runs` status filter fixed.
  - 1.3.0: team tools and a Helm chart.
  - 1.2.0: state-version tools, org allowlist, shared secret.

  No dates are shown in the changelog. The reference page and changelog disagree on the search tool's name, so check the installed version. — [CHANGELOG](https://github.com/hashicorp/terraform-mcp-server/blob/main/CHANGELOG.md)

### Inferences
- **Homelab use:** with no HCP or TFE licence, use `--toolsets registry`. The agent then gets accurate, version-current schemas for `vmware/vcf`, `vmware/nsxt` and `hashicorp/vsphere`, which cuts hallucinated HCL arguments. Planning and applying stay on the local `terraform` or `tofu` CLI, run by a human or CI.
- **The run tools are a good plan-only template.** Expose `create_run` (plan only) and the plan-log and JSON tools, and keep `ENABLE_TF_OPERATIONS=false`. The agent can then plan and read plans but cannot apply.
- **Remote state with HCP Terraform:** the HCP free tier could hold remote state and runs. However, a cloud runner cannot reach an on-prem vCenter, so on-prem applies need a self-hosted HCP agent. This is not verified here.

### Gaps
- No exact release dates were found for 1.3.0 or 1.3.1; the GitHub Releases section appeared empty in the fetch.
- No source found showing that the server wraps a local Terraform CLI, so it does not appear to.
- Whether HCP Terraform's free tier allows self-hosted agents in October 2026 was not checked.

## 2. Terraform providers for VCF 9.x (vmware/vcf, vmware/nsxt, hashicorp/vsphere)

### Takeaway
All three providers are active and have explicit VCF 9.1-era support:
- **vmware/vcf v0.19.0 (2026-10-07):** updates the domain and instance resources for VCF 9.1.1.
- **vmware/nsxt v3.12.0 / v3.12.1:** adds NSX 9.1.0 and 9.1.1.
- **vsphere provider:** lists vSphere 9.x from v2.15.2. v2.16.0 is the VCF 9.1-highlighted release, now in the VMware registry namespace and supported through Broadcom GSS. The current release is v2.17.1.

The vcf provider is still pre-1.0.

### Cited Findings
- **vmware/vcf (SDDC Manager / VCF Installer):**
  - v0.19.0, 2026-10-07: "Updated `r/domain` and `r/instance` for VCF 9.1.1" (#447, #445); fixed an unsafe pointer dereference in `FlattenCredentials`; vcf-sdk-go 0.8.1.
  - v0.18.2, 2026-09-14: password attributes marked `Sensitive`, so plan and show no longer print them in cleartext.
  - v0.18.1, 2026-06-10: chores only.

  — [terraform-provider-vcf releases](https://github.com/vmware/terraform-provider-vcf/releases)
- **vmware/nsxt:**
  - v3.12.1, 2026-09-11: "add NSX 9.1.1 support".
  - v3.12.0, 2026-05-17: adds 9.1.0 to supported versions, plus TEP-less 9.1.0 changes for IP block, Distributed VLAN Connection and VPC subnet resources.
  - v3.11.1, 2026-04-21.

  — [terraform-provider-nsxt releases](https://github.com/vmware/terraform-provider-nsxt/releases)
- **vsphere provider releases:**
  - v2.17.1, 2026-09-14: `product_key` marked sensitive.
  - v2.17.0, 2026-09-01: new `r/network_protocol_profile`, `r/sso_user` and `r/sso_group`; OVF/OVA deploy to a datastore cluster.
  - v2.15.2: docs updated to list vSphere 9.x support.
  - v2.16.0: added `r/supervisor_v2`.

  — [terraform-provider-vsphere releases](https://github.com/vmware/terraform-provider-vsphere/releases)
- **VCF 9.1 blog on vsphere provider v2.16.0:**
  - "Now in the VMware Terraform registry and officially supported", open source, and supportable through Broadcom GSS.
  - Adds Project VPC network data source support, vSphere Zones, alarms, cluster and VM EVC, and `r/supervisor_v2`.

  — [VMware VCF blog, 2026-05-25](https://blogs.vmware.com/cloud-foundation/2026/05/25/unlocking-the-full-potential-of-programmable-infrastructure-with-vmware-cloud-foundation-9-1-new-features-and-capabilities/)
- **OpenAPI as the generation source:** VCF 9.1 positions the OpenAPI specs as "the single source of truth" for SDK generation across Python, Java, PowerShell and Terraform. — [VMware VCF blog, 2026-05-25](https://blogs.vmware.com/cloud-foundation/2026/05/25/unlocking-the-full-potential-of-programmable-infrastructure-with-vmware-cloud-foundation-9-1-new-features-and-capabilities/)

### Inferences
- The vsphere provider was moved from `hashicorp/vsphere` to the `vmware/` namespace. New code should check which registry address current docs use, and the Terraform MCP `get_latest_provider_version` tool can answer that.
- **Where the vcf provider fits:** it suits bring-up and day-2 domain or cluster lifecycle, but has rough edges: pre-1.0 and recent fixes for nil-dereference and cleartext-password bugs.
- **Secret handling for plan-only review:** before v0.18.2, plan output leaked passwords. Any agent-reads-plan flow must pin provider versions of at least vcf 0.18.2 and vsphere 2.17.1, and still treat plan JSON as sensitive.
- **Lab impact:** the user's lab runs 9.1.1, which matches vcf 0.19.0 and nsxt 3.12.1.

### Gaps
- No explicit compatibility matrix found for vsphere provider 2.17.x against vCenter 9.1.1. The release notes don't mention it, so 9.1.1 support is **unverified**.
- No VCF Automation (formerly Aria Automation / vRA) Terraform provider status for 9.1 was researched.
- How well `vmware/vcf` covers VCF 9.1 fleet services (VCF Operations, fleet LCM) is unverified.

## 3. Ansible: AAP MCP server, Ansible dev-tools MCP, and VMware collections

### Takeaway
Red Hat ships two different MCP servers:
- **AAP MCP server:** needs an AAP subscription. Technology Preview in 2.6.4; reportedly GA in 2.7, which is unconfirmed. Six toolsets, read-only by default, and RBAC-scoped by the token.
- **Ansible Development Tools MCP server:** runs fully locally over stdio (npm or container) and needs no licence. It covers lint with fixes, scaffolding and `ansible-navigator` execution.

For VMware, `vmware.vmware` is the actively developed Red Hat-certified collection. `community.vmware` is being hollowed out in its favour. No explicit vSphere 9 / VCF 9 support statement was found in the vmware.vmware changelog.

### Cited Findings
- **AAP MCP server availability:** organisation admins can deploy it on an operator-based or containerized AAP 2.6 installation as a Technology Preview. That means no production SLA and possibly not functionally complete. — [Red Hat AAP docs / search summary](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/html/release_notes/patch_releases). A Red Hat blog places it in AAP 2.6.4. — [Red Hat blog](https://www.redhat.com/en/blog/it-automation-agentic-ai-introducing-mcp-server-red-hat-ansible-automation-platform) (the URL returned 404 on direct fetch; content seen only via search snippet)
- **AAP MCP server design** (third party, 2026-07-16):
  - Six toolsets, each with its own endpoint `https://<aap>:8448/<toolset>/mcp`: job_management, inventory_management, system_monitoring, user_management, security_compliance, platform_configuration.
  - Read-only by default; the AAP admin sets the mode globally at deploy time.
  - Read-write is still bounded by the token's RBAC.
  - Bearer token with Read or Write scope.

  — [techbeatly](https://techbeatly.com/claude-code-ansible-automation-platform-mcp-server/)
- **GA status conflict:** the same article says it is "GA as of AAP 2.7" but also calls it a Technology Preview. **Not verified** against Red Hat 2.7 release notes. — [techbeatly](https://techbeatly.com/claude-code-ansible-automation-platform-mcp-server/)
- **Ansible Development Tools MCP server:**
  - Capabilities: best-practice and Zen-of-Ansible info, environment setup, playbook and collection scaffolding, Ansible Lint with fixes, an execution environment builder, and Ansible Navigator, which **executes playbooks**.
  - Transport is stdio only. Install with `npx -y @ansible/ansible-mcp-server --stdio` (needs Node 24+) or the image `ghcr.io/ansible/devtools-mcp-server`.
  - Labelled a technical preview.

  — [docs.ansible.com](https://docs.ansible.com/projects/vscode-ansible/mcp/)
- **`ansible.mcp` collection (Technology Preview):** inverts the direction. Playbooks query and call MCP servers' tools. — [Red Hat catalog](https://catalog.redhat.com/en/software/collection/ansible/mcp)
- **Community servers:** `iamenr0s/ansible-mcp` and an `ansible-mcp` PyPI package exist but are unofficial. — [ecosyste.ms](https://awesome.ecosyste.ms/projects/github.com%2Fiamenr0s%2Fansible-mcp), [piwheels](https://www.piwheels.org/project/ansible-mcp/)
- **vmware.vmware recent releases:**
  - v2.10.0: `esxi_info`, `esxi_powerstate`, `esxi_service` and `esxi_service_info` (some migrated from community.vmware); `guest_info` renamed to `vm_info`; a `vcenter_event_manager` EDA plugin.
  - v2.11.0: `folder_info` module; **check mode** for the `vm` module (create, update, delete).
  - v2.12.0: event_query and vm_info fixes.
  - No release dates in the changelog. **No vSphere 9 / VCF 9 mention.**

  — [vmware.vmware CHANGELOG](https://raw.githubusercontent.com/ansible-collections/vmware.vmware/main/CHANGELOG.rst)
- **community.vmware hollowing out:**
  - `vmware_cluster`, `cluster_dpm`, `cluster_drs`, `cluster_drs_recommendations` and `cluster_vcls` were removed and redirected to vmware.vmware equivalents.
  - `vmware_guest_snapshot` is deprecated, with removal in 8.0.0.

  — [community.vmware releases](https://github.com/ansible-collections/community.vmware/releases). The rationale is that `vmware_guest` is hard to maintain and is being broken up in vmware.vmware. — [Red Hat Jira ACA-2140](https://issues.redhat.com/browse/ACA-2140)

### Inferences
- **The dev-tools MCP server is the licence-free Ansible fit for the lab.**
  - The agent writes playbooks and runs `ansible-lint`, which fixes them.
  - `--check` (now supported by `vmware.vmware.vm`) gives an Ansible "plan".
  - The human then runs the real `ansible-navigator run`.
- **Its navigator tool can execute.** A plan-only setup must not expose it, or must wrap it so it only runs with `--check --diff`.
- **The AAP MCP server's read-only toolsets** (job_management launching only pre-approved job templates) are a strong "human-curated actions" model. However, they need an AAP subscription.
- **vmware.vmware_rest:** status for VCF 9 was not researched. It is generated from the vSphere REST API and is plausibly version-tolerant, but that is unverified.

### Gaps
- vmware.vmware: no release dates and no stated vSphere 9.x / VCF 9.1 support matrix found, so it is **unverified**.
- vmware.vmware_rest: current version and VCF 9 status not researched.
- AAP 2.7 GA status of the MCP server was not confirmed from Red Hat primary sources; the Red Hat blog URL 404'd.
- No Ansible collection for SDDC Manager / VCF Installer (an equivalent of the vcf Terraform provider) was found in this pass.

## 4. Other options: Pulumi, OpenTofu, GitOps, PowerCLI 9.1 and the VCF SDK as targets, plan-only patterns

### Takeaway
- **Pulumi:** its MCP server can run `preview` and `up` locally, but needs a Pulumi access token and has no first-class VCF story. Pulumi's vSphere provider was not researched.
- **PowerCLI 9.1 and the unified VCF Python SDK 9.1 / 9.1.1:** the best-covered code-generation targets for VCF 9.1, with VPC, SSO/OAuth and NSX/Ops/fleet-LCM coverage.
- **Recommended plan-only pattern:** the agent writes HCL or playbooks, runs `plan` / `--check`, and opens a PR. A human applies. It composes cleanly from the local Terraform MCP registry toolset, the Ansible dev-tools MCP, and git.

### Cited Findings
- **Pulumi MCP server (local, `@pulumi/mcp-server`, stdio):**
  - Exposes registry lookups plus `pulumi-cli-preview`, `pulumi-cli-up`, `pulumi-stack-output`, `pulumi-cli-refresh` and a Neo bridge.
  - Shells out to the local Pulumi CLI and needs `PULUMI_ACCESS_TOKEN` for deploy or Cloud-reading tools.
  - The hosted server adds stack, resource search, policy violation and user tools.
  - Tool names vary across sources.

  — [Pulumi docs](https://www.pulumi.com/docs/ai/mcp-server/), [Docker Hub mcp/pulumi](https://hub.docker.com/r/mcp/pulumi), [Glama](https://glama.ai/mcp/servers/@pulumi/mcp-server/tree)
- **PowerCLI 9.1:**
  - Download `VCF-PowerCLI-9.1.0-25380678.zip`. — [Broadcom developer](https://developer.broadcom.com/tools/VCF-powercli/latest/)
  - New: `VMware.VimAutomation.Vpc` (`New-VpcIpBlock`, `New-VpcTransitGateway`, `New-VpcSpan`, `New-VpcGroup`, `New-VpcConnectivityPolicy`); `VMware.Vcf.Sso` (`New-VcfOAuthSecurityContext`, VcfApiToken for `Connect-VIServer`, `Connect-NsxServer`, `Connect-VcfOpsServer`); `Get-VsanEffectiveCapacity`; NVMe/TCP vmk.

  — [VMware VCF blog, 2026-05-25](https://blogs.vmware.com/cloud-foundation/2026/05/25/unlocking-the-full-potential-of-programmable-infrastructure-with-vmware-cloud-foundation-9-1-new-features-and-capabilities/)
- **VCF Python SDK 9.1:**
  - The unified SDK covers vSphere (pyVmomi incl. vSAN), NSX, VCF Operations, Log Management, Ops for Networks, and Fleet and SDDC Lifecycle. Distributed via PyPI and the Broadcom portal. — [Broadcom VCF Python SDK](https://developer.broadcom.com/sdks/vcf-python-sdk/latest/), [GitHub vcf-sdk-python](https://github.com/vmware/vcf-sdk-python)
  - SDK 9.1.1 (build 25714914), 2026-09-03: dependency-only, Python 3.10–3.14. Seen via search summary of the Broadcom and GitHub pages.
- **No first-party MCP:** the VCF 9.1 programmability blog mentions no MCP server from Broadcom. — [VMware VCF blog](https://blogs.vmware.com/cloud-foundation/2026/05/25/unlocking-the-full-potential-of-programmable-infrastructure-with-vmware-cloud-foundation-9-1-new-features-and-capabilities/)
- **vCenter 9.1 build timeline:** 9.1 GA 2026-05-12; 9.1.0.0300 2026-07-29; 9.1.1.0 2026-09-03. — [Broadcom KB 326316](https://knowledge.broadcom.com/external/article/326316/vmware-vcenter-server-versions-and-build.html)

### Inferences
- **OpenTofu:** the vmware/vcf, vmware/nsxt and vsphere providers are ordinary Terraform-protocol providers and should work under `tofu` via the OpenTofu registry mirror. That gives a fully open-source, licence-free plan/apply engine. This is not verified in this research.
- **Recommended homelab "AI plans, human applies" stack (all local):**
  1. Terraform MCP with `--toolsets registry` for schema grounding, plus the Ansible dev-tools MCP for lint and scaffolding.
  2. The agent edits HCL or playbooks in a git branch.
  3. A local CI runner (e.g. Gitea/Forgejo Actions or GitHub Actions self-hosted on an LXC) runs `terraform plan -out` or `ansible-playbook --check --diff` and posts the plan as a PR comment.
  4. The human reviews and merges.
  5. A separate, human-triggered job runs `apply` with the saved plan.

  The agent's credentials should be read-only vCenter/NSX/SDDC accounts, enough for `plan`/refresh. Only the apply job holds write credentials.
- **Relation to direct API-calling MCP:** this complements it rather than replacing it. Use a read-only API MCP for discovery and state questions, and IaC for changes. The IaC path gives diffable, reviewable, reproducible changes. Direct-API MCPs give speed for one-off queries.
- **Where IaC coverage runs out:** VCF 9.1 surfaces such as fleet LCM, VCF Operations and the depot are poorly covered by Terraform or Ansible. There, PowerCLI 9.1 scripts or VCF Python SDK code generated by the agent, reviewed and run by a human, is the most complete "write code, human executes" target. Unlike Terraform, it has no built-in plan/diff, so a dry-run convention has to be hand-built, e.g. `-WhatIf` in PowerCLI where supported.
- **Plan JSON is sensitive:** feeding plan JSON back to an LLM must account for remaining sensitive values. The vcf and vsphere providers only recently marked passwords and product keys `Sensitive`.

### Gaps
- OpenTofu compatibility with the vmware/vcf provider and OpenTofu's own MCP or registry tooling: **no sources gathered**.
- Pulumi vSphere / NSX provider currency and VCF 9.1 support: not researched.
- No published, VCF-specific "agent opens PR, CI plans, human applies" reference implementation was found.
- PowerCLI 9.1.1 release notes, and whether `-WhatIf` coverage extends to the new Vpc/Sso modules: not found.
- Exact PyPI package names and versions for the VCF Python SDK 9.1.x were not confirmed directly on PyPI.
