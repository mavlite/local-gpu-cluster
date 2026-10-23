# Broadcom/VMware Official AI-Agent and MCP Offerings for VCF 9.x (as of 2026-10-22)

Scope note: "Official" means published or shipped by Broadcom. William Lam (williamlam.com) is a Broadcom employee, but his blog is a personal site. It is the main public source for the VCF-AI Fling and the 9.1.1 AI Assistant walkthroughs. The Broadcom TechDocs pages and blogs.vmware.com are the primary corporate sources. Research ran about 20 tool calls. Several Broadcom portal pages (the Flings licence, the 9.1.1 release notes, the PAIS 3.0 release notes) were either inaccessible or not found. Those gaps are marked below.

## 1. VCF Agentic AI Hub ("VCF-AI") Code Assist MCP Server Fling

### Takeaway
VCF-AI 0.1.1 (Lam post dated 2026-10-05) is a free Broadcom Fling OVA. Its MCP server is code-assist only: it generates PowerCLI and VCF Python SDK code from bundled OpenAPI specs, SDK docs and VCF-Engineering skill files. No public source describes it calling live VCF APIs. It is heavy (16 vCPU / 32 GB, 4 IPs, Kubernetes on VCFMS, Keycloak OAuth), and the Fling licence limits it to internal evaluation.

### Cited Findings
- **Release and version:** Lam's post is dated 10/05/2026. It names release 0.1.1, with download/manifest name `vcf-ai-0.1.1-25796386`. It describes VCF-AI as a Fling "delivered as a standalone virtual appliance". The Fling is a free download from the Broadcom Support Portal and comes with a PDF guide. — [William Lam, VCF Agentic AI Hub (Code Assist) MCP Server Fling](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Scope:** The "VCF Code Assist MCP Server" "currently supports code generation" for the listed SDKs. The post describes code generation and documentation only, with no live calls to running environments. — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **SDK coverage, VCF PowerCLI:** vCenter Server, vSAN, VCF Operations, SDDC Manager, NSX, vSphere Supervisor, HCX, SRM. — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **SDK coverage, VCF Python SDK:** vCenter Server, vSAN, vSAN Data Protection, VCF Operations, VCF Installer, SDDC Manager, NSX, vSphere Supervisor, VCF Operations for Networks, Log Management, VCF Fleet LCM and SDDC LCM. — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Contents:** OpenAPI specifications and documentation for the two SDKs, plus "skill files" written by VCF Engineering for agentic coding tools. — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Tool names:** The post lists none. — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Deployment requirements:**
  - The OVA needs 16 vCPU and 32 GB RAM.
  - It needs 4 consecutive IPs, and the first must have an FQDN. Lam's example is 172.30.0.245-248, with .245 mapped to hub.vcf.lab.
  - It is "a Kubernetes-based application" built on VCF Management Services (VCFMS).
  - A browser-based VCF-AI Installer runs on a client machine, which needs bidirectional connectivity to the OVA. An Ubuntu or Windows VM on the same network also works.
  - An "embedded instance of Keycloak" handles auth between the MCP server and the coding agent. A local user must be created as described in the PDF.

  — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Client connection:**
  - The remote MCP URL is `https://hub.vcf.lab/mcp`.
  - Clients connect through the npm `mcp-remote` package, installed with `--strict-ssl=false`. Lam's configs set `NODE_TLS_REJECT_UNAUTHORIZED="0"`, so the setup relies on a self-signed certificate.
  - Including "Using vcf-ai MCP ..." in a prompt starts the OAuth redirect to Keycloak.
  - Lam gives example configs for Claude Desktop (`claude_desktop_config.json`), Cursor (`~/.cursor/mcp.json`) and Codex (`~/.codex/config.toml`, via npx mcp-remote).
  - The Cursor redirect to Keycloak got stuck for Lam. He completed auth through Claude instead.

  — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Known install issues (from the post's comments):**
  - Platform token acquisition failed with curl exit 6 / HTTP_STATUS:000, followed by 404 NotFound and "component installation failed".
  - Responder "Elton" traced it to the FQDN not resolving from one VM. He suggested checking with `curl --resolve <fqdn>:443:<IP>`.
  - The installer does not detect its own success or failure, so VMs from failed runs must be deleted by hand.

  — [Lam comments](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Roadmap:**
  - No official roadmap or Fling-to-product path is stated. Lam invites feedback and says he will forward it to the product team.
  - Commenters asked for operational (read-only vCenter, Ops, Log Insight) MCP servers and for vSphere 8 / Aria 8 support. Those questions are unanswered on the page.
  - One commenter pointed to the third-party "vmware-skills" GitHub repo for read-only monitoring MCP servers. That repo is community, not Broadcom.

  — [Lam](https://williamlam.com/2026/10/vcf-agentic-ai-hub-code-assist-mcp-server-fling.html)
- **Fling licence terms:**
  - Flings are distributed through Broadcom Support Portal Free Downloads (since July 2025). — [VMware blog, Flings in BSP](https://blogs.vmware.com/cloud-foundation/2025/07/28/vmware-flings-is-now-available-in-free-downloads-of-broadcom-support-portal-bsp/)
  - The "VMware by Broadcom Flings License Agreement" (May 2025 version) is between the user and CA, Inc. (US) or VMware International Unlimited Company (outside the US).
  - It grants a "non-exclusive, non-transferable license (without the right to sublicense)" limited to internal testing and evaluation, including evaluating a possible commercial version.
  - Users who do not accept it "must not install or use a Flings Offering".

  — [Broadcom Flings License Agreement (BSP ID 25729)](https://support.broadcom.com/web/ecx/support-content-notification/-/external/content/ReleaseAnnouncements/0/25729). This came from a search snippet. A direct fetch returned HTTP 403.
- **Other VCF Flings:** Another 2026 VCF Fling, VCF Inspector, is a standalone diagnostic and health tool for VCF 9.1 (2026-07-28). It is not an MCP server per the available sources. — [VMware blog, VCF Inspector Fling](https://blogs.vmware.com/cloud-foundation/2026/07/28/vcf-inspector-fling/)

### Inferences
- The VCF-AI MCP server is a retrieval/codegen aid, which matches the existing note "0.1.1, no live tools". Live VCF actions would still have to come from code the agent writes and a human (or agent) runs separately.
- The Fling licence's internal-evaluation-only grant and no-sublicense clause make wholesale extraction and redistribution of the bundled skill files or docs risky. The underlying OpenAPI specs are separately available under Apache-2.0 (section 5), so specs can be sourced there instead of from the Fling.
- 16 vCPU / 32 GB / 4 IPs is a large footprint for a docs/codegen service.

### Gaps
- MCP tool names and categories are unknown. Neither Lam's post nor any other public source lists them, and the PDF guide was not accessible.
- No release newer than 0.1.1 was found as of 2026-10-22. The BSP Fling page itself could not be checked.
- The full Fling licence text could not be read (403). Clauses on reverse engineering, derivative works, feedback, term and warranty are unverified.
- Whether VCF-AI requires an existing VCF deployment, or the VCFMS is self-contained in the OVA, is unclear.

## 2. VCF Private AI Services (PAIS) 2.1 / 3.0: MCP consumer, tool gallery, Agent Builder

### Takeaway
PAIS is an MCP client/consumer, not a host. From PAIS 2.1 (VCF 9.1, 2026-05-12) it connects to remote third-party MCP servers in a namespace via VCF Automation. An admin reviews and approves each tool in an "MCP Tools Gallery" before Agent Builder agents can use it. Only tool calling is supported. CPU inference (llama.cpp) exists, and AMD GPUs appear to work only as DirectPath passthrough (informal). Officially documented GPU support is NVIDIA-only.

### Cited Findings
- **Release history:**
  - PAIS 2.1: "12 MAY 2026 for VCF 9.1" and "20 MAR 2026 for VCF 9.0.2".
  - Earlier: 2.0.89 (11 SEP 2025) and 2.0.51 (17 JUN 2025) for VCF 9.0.

  — [PAIS Release Notes](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-release-notes/vmware-private-ai-services-release-notes.html)
- **MCP in 2.1:**
  - The MCP Tools Gallery is "Available in Private AI Services 2.1 and later". 2.1 adds a gallery "for centralized MCP server management and tool enablement".
  - 2.1 exposes knowledge bases over MCP, and Data Indexing and Retrieval is itself an MCP tool.
  - Models without MCP tool calling need the header `x-pais-force-static-tool-execution: true`.

  — [PAIS Release Notes](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-release-notes/vmware-private-ai-services-release-notes.html)
- **Agent Builder:** Developers test agents in an in-app playground and save them, so they can "use it as the back end for their AI-powered applications". — [PAIS Release Notes](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-release-notes/vmware-private-ai-services-release-notes.html)
- **Connecting an MCP server:**
  - The workflow runs in VCF Automation, under Build&Deploy, in a namespace where PAIS is available.
  - The server must be reachable over HTTP/HTTPS and support Streamable HTTP or SSE. HTTPS servers need their issuer certificate added as a CA trust bundle.
  - Optional static auth tokens and custom headers are supported. Headers cannot be edited, only deleted and recreated.

  — [Connect an MCP Server to Private AI Services](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/adding-mcp-servers-for-real-time-data-access-and-specialized-ai-capabilities/connect-to-an-mcp-server.html)
- **Approval:** After adding a server, the user reviews its tools and approves only those "suitable and safe" for agents. Approved tools then become usable in Agent Builder. — [Connect an MCP Server](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/adding-mcp-servers-for-real-time-data-access-and-specialized-ai-capabilities/connect-to-an-mcp-server.html); [Explore the MCP Tools in Your Namespace](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/adding-mcp-servers-for-real-time-data-access-and-specialized-ai-capabilities/exploring-the-mcp-tools-avaiable-in-your-namespace.html)
- **Unsupported MCP features:** Integration is tool-calling only. "MCP prompts, sampling, and resources" don't apply. — [Connect an MCP Server](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/adding-mcp-servers-for-real-time-data-access-and-specialized-ai-capabilities/connect-to-an-mcp-server.html)
- **Hosting:** The docs describe connecting to existing remote servers ("must first connect to the MCP servers that provide them"), not hosting them. — [Connect an MCP Server](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-foundation-9-x/what-is-private-ai-services/adding-mcp-servers-for-real-time-data-access-and-specialized-ai-capabilities/connect-to-an-mcp-server.html)
- **Access control:** Namespace-level RBAC governs both PAIS model endpoints and their associated MCP servers. — [VCF 9.1 Design Library, Private AI Services](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/design/design-library/private-ai-platform-detailed-design/private-ai-services.html), via search snippet
- **VCF 9.1 launch blog:**
  - The feature heading is "Model Context Protocol (MCP) Support with Governance".
  - The blog cites "external MCP tools from Oracle, Microsoft SQL Server, ServiceNow, GitHub, Slack, PostgreSQL, and more".
  - "VCF Private AI Services now support CPU based inferencing, in addition to GPU-based deployments" (llama.cpp).
  - Hardware mentions are NVIDIA-only (Blackwell, HGX, ConnectX-7, BlueField-3).

  — [VMware blog, 2026-05-05](https://blogs.vmware.com/cloud-foundation/2026/05/05/streamline-simplify-and-protect-all-your-ai-workloads-with-vcf-9-1/)
- **2.1 runtimes and limits:**
  - Engines: vLLM v0.11.2, llama.cpp b7739 (completions and embeddings), Infinity 0.0.76 (embeddings).
  - NVIDIA GPU Operator 25.10.1 by default.
  - Kubernetes: VKr 1.33 with VKS 3.5.0+.
  - Up to 15 model endpoint replicas per namespace.
  - Known issue: GPU ModelRuntime pods may fail to start with GPU Operator v25.10.1.

  — [PAIS Release Notes](https://techdocs.broadcom.com/us/en/vmware-cis/private-ai/foundation-with-nvidia/9-0/private-ai-release-notes/vmware-private-ai-services-release-notes.html)
- **PAIS 3.0 (VCF 9.1.1):**
  - Engines: "PAIS 3.0 ships with three engines": Infinity 0.0.76 (CPU), vLLM v0.20.0 (GPU), LlamaCPP server-b9309 (CPU). GPU llama.cpp needs an override to a CUDA image (e.g. ghcr.io/ggml-org/llama.cpp).
  - GPU Operator: 25.10.1 by default, can be overridden up to 26.3.1. "The GPU Operator will not be deployed when only CPU support is selected."
  - Prerequisite: "NVIDIA GPU for use with ESX DirectPath I/O or vGPU". Asked about AMD GPUs, Lam replied "Yes, it's just DirectPath (passthru)". That contradicts the NVIDIA-only prerequisite and is informal.
  - Example GPU VM class: 8 vCPU / 32 GB reserved.

  — [Lam, Deploying Your Own AI Models with PAIS 3.0 (2026-09-10)](https://williamlam.com/2026/09/vcf-9-1-1-deploying-your-own-ai-models-with-vcf-private-ai-services-pais-3-0.html)
- **Air-gapped install:** PAIS 9.1.1 can be installed air-gapped using the Artifact Mirroring Tool (AMT). — [Lam, PAIS with AMT](https://williamlam.com/2026/09/vcf-9-1-1-setting-up-vcf-private-ai-services-pais-using-artifact-mirroring-tool-amt-for-air-gapped-environments.html)

### Inferences
- PAIS could consume a self-built VCF MCP server, for example one generated from vcf-api-specs, provided it speaks Streamable HTTP/SSE. Admin tool approval would then act as a governance gate.
- PAIS requires the full VCF Automation + Supervisor/VKS stack.
- Without NVIDIA GPUs, the documented path is CPU inference via llama.cpp/Infinity. AMD passthrough with a custom ROCm llama.cpp image is plausible from Lam's comment but unsupported and unverified.

### Gaps
- Official PAIS 3.0 release notes were not found. The 3.0 MCP/Agent Builder changes versus 2.1 are unknown.
- The exact RBAC role that can add MCP servers or approve tools is not named in the docs fetched.
- Whether PAIS can run (host) an MCP server container itself is not documented. Only remote connection is described.

## 3. VCF Operations "AI Assistant for VCF" / VMware Intelligent Assist (VCF 9.1.1, Tech Preview)

### Takeaway
VCF 9.1.1 (GA 2026-09-03) adds a Tech Preview chat assistant inside VCF Operations. It runs on VCFMS, uses either Google Gemini or PAIS-hosted models, is read-only with 25 built-in "skills", and exposes no documented MCP endpoint.

### Cited Findings
- **Official blog (2026-09-03):**
  - VCF 9.1.1 "introduces a [Tech Preview] feature" that brings a chat interface into VCF Operations.
  - It "natively supports different large language models (LLMs) running on Private AI Services (PAIS)" and "your private instance of Google Gemini".
  - Capabilities named: conversational diagnostics; correlating health alerts, configuration and logs; and building management packs "with AI without API expertise".

  — [VMware blog, New AI and Kubernetes capabilities in VCF 9.1.1](https://blogs.vmware.com/cloud-foundation/2026/09/03/new-ai-and-kubernetes-private-cloud-operations-capabilities-in-vmware-cloud-foundation-9-1-1/)
- **Enablement:**
  - The chat is branded "VMware Intelligent Assist". It is enabled via Extensions > Extension Management (VMware Intelligent Assist + Advanced Configuration). LLM Providers are PAIS or Google Gemini.
  - Gemini is "currently the only supported cloud-based LLM provider".
  - When enabled, "it deploys the service on VCFMS". All VCFMS worker nodes need outbound access to the Gemini endpoint.
  - With VCF SSO, the Extensions permission is off by default.
  - Chat history "is currently not persisted".
  - AI Observability shows tokens, latency, skill usage and audit logs.
  - Asked about local models outside PAIS (e.g. Gemma), Lam says it will be PAIS only for now.
  - One commenter on VVF 9.1.1 did not see the option.

  — [Lam, Enabling the new AI Assistant for VCF (2026-09-08)](https://williamlam.com/2026/09/vcf-9-1-1-enabling-the-the-new-ai-assistant-for-vcf-in-vcf-operations.html)
- **Read-only, skills and roadmap:**
  - It "operates in read-only mode" and "includes 25 built-in tools, commonly referred to as skills". Tool names are not listed.
  - The PAIS endpoint must use an FQDN.
  - Roadmap per Lam: "In a future release, users will have the ability to create their own skills". He envisions a skills marketplace and possibly "read-write capabilities with the appropriate access controls and guardrails".

  — [Lam, Using PAIS models with AI Assistant for VCF](https://williamlam.com/2026/09/vcf-9-1-1-using-vcf-private-ai-services-pais-models-with-ai-assistant-for-vcf.html)

### Inferences
- The AI Assistant is not an MCP server and cannot be driven by an external agent. It is an in-UI assistant only.
- Using it locally requires PAIS, which in turn requires VKS/Supervisor and VCF Automation.

### Gaps
- The 25 skill names, the model requirements (tool-calling support, context size) and the VCF 9.1.1 release-note wording were not retrieved.
- Whether it works on VVF is unconfirmed.

## 4. Official Broadcom MCP servers by product (VCF and others)

### Takeaway
No official Broadcom MCP server performs live operations on vSphere, vCenter, NSX, SDDC Manager, VCF Operations, VCF Automation or Tanzu/VKS as of 2026-10-22. VCF's only first-party MCP artefacts are the VCF-AI code-assist Fling and PAIS's MCP-consumer features. Elsewhere, Broadcom does ship production MCP servers: Tanzu Greenplum MCP Server 1.0 and the Rally MCP server (mcp.rallydev.com, GA early 2026). So the company does ship MCP servers, just not yet for VCF operations.

### Cited Findings
- **Tanzu Greenplum MCP Server 1.0 (official TechDocs):**
  - It is an enterprise gateway connecting AI clients to Greenplum 6/7.
  - Security: OAuth 2.1 with external IdPs (Entra ID, Okta), plus a policy file to restrict to read-only SELECT or to allow/deny statement types.
  - It binds to 127.0.0.1 by default, and binding to 0.0.0.0 requires auth.

  — [Overview of Tanzu Greenplum MCP server](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-greenplum-mcp-server/1-0/tnz-gp-mcp-server/index.html); [MCP Tools reference](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-greenplum-mcp-server/1-0/tnz-gp-mcp-server/tools-reference.html); [Tanzu blog, Securing Database MCP](https://blogs.vmware.com/tanzu/from-prototype-to-production-securing-database-mcp-at-enterprise-scale/)
- **Rally MCP server:**
  - Hosted at mcp.rallydev.com, with no Rally-side setup and no extra fee. Customers bring their own LLM.
  - GA for US customers around Feb 2026 and for EMEA on 2026-04-08. An earlier beta required a support ticket (superseded).

  — [KB 433691, Requirements to Enable MCP in Rally](https://knowledge.broadcom.com/external/article/433691/requirements-to-enable-mcp-in-rally.html); [Rally office hours](https://academy.broadcom.com/valueops/rally/office-hours-recording-management-and-control-plane-servers); [Community, EMEA availability](https://community.broadcom.com/discussion/rally-mcp-server-available-in-emea-on-april-8); [KB 418781 (old beta)](https://knowledge.broadcom.com/external/article/418781/rally-switch-on-mcp-server-for-us.html)
- **Tanzu Platform Agent Foundations:** Gives developers "governed access to models, Model Context Protocol (MCP) servers and marketplace services, all pre-curated by the IT organization". That is MCP consumption, not a VCF MCP server. — [Broadcom IR press release](https://investors.broadcom.com/news-releases/news-release-details/broadcom-announces-tanzu-platform-agent-foundations-bringing)
- **Community lookalikes:** The rallyuxr.com Rally MCP and "BroadcomSupportMCP" on Glama are not Broadcom products. — [rallyuxr](https://help.rallyuxr.com/articles/5139669410); [Glama BroadcomSupportMCP](https://glama.ai/mcp/servers/ttonogai/BroadcomSupportMCP/tools/search_documentation)

### Inferences
- The Greenplum server's pattern (OAuth 2.1 + policy file restricting statement types + localhost default bind) is Broadcom's own reference design for a governed, read-mostly MCP server. It is a useful template for a self-built VCF ops MCP.

### Gaps
- Searches found no official MCP server for vSphere, vCenter, NSX, SDDC Manager, PowerCLI, the VCF Python SDK, VCF Automation (Aria) or VKS. Absence was not confirmed against a complete product catalogue.
- The press release "Broadcom Makes VMware Cloud Foundation an AI Native Platform and Accelerates Developer Productivity" (broadcom.com/company/news/product-releases/63401) only returned its headline. Its content (likely Explore 2026 announcements) was not read.
- VMware Explore 2026 session content was not reviewed.

## 5. Building blocks: vmware/vcf-api-specs, SDKs, and William Lam guidance

### Takeaway
Broadcom publishes Apache-2.0 OpenAPI/WSDL specs for VCF 9.1 components on GitHub. That is the licence-clean basis for self-built MCP tooling, unlike the Fling-licensed VCF-AI bundle. Lam's 2026 output treats MCP as code-assist (VCF-AI) and in-product assistant (AI Assistant/PAIS). He does not publish an official live-ops MCP server.

### Cited Findings
- **vmware/vcf-api-specs:**
  - Licence: Apache-2.0.
  - vSphere specs: WSDL plus OpenAPI (REST and VI JSON).
  - NSX specs: OpenAPI 2.0.
  - OpenAPI 3.0 specs: SDDC Manager, VCF Installer, vSAN Data Protection, VCF Fleet Lifecycle, VCF SDDC Lifecycle, VCF Operations, VCF Operations for Networks, VCF Log Management, VCF Real-time Metrics.
  - The repo references VCF 9.1 official Java/Python SDKs, which live in separate repos.
  - The build scripts are unsupported. The repo does not mention AI or MCP.

  — [github.com/vmware/vcf-api-specs](https://github.com/vmware/vcf-api-specs)
- **VCF 9.1.1 GA:** 2026-09-03 alongside VMware Explore 2026, the first maintenance release of 9.1. — [VMware blog 2026-09-03](https://blogs.vmware.com/cloud-foundation/2026/09/03/new-ai-and-kubernetes-private-cloud-operations-capabilities-in-vmware-cloud-foundation-9-1-1/); [Lam, 10 enhancements in VCF 9.1.1](https://williamlam.com/2026/09/10-exciting-enhancements-in-vmware-cloud-foundation-9-1-1.html) (search snippet)

### Inferences
- The VCF-AI Fling's SDK coverage list maps closely onto the vcf-api-specs product list. The Fling's OpenAPI content is therefore likely obtainable from the Apache-2.0 repo. The VCF-Engineering skill files are the Fling-unique part.

### Gaps
- The vcf-api-specs last-commit date and per-9.x-version coverage were not shown on the page.
- No Lam post on building a live-ops MCP server for VCF was found.
