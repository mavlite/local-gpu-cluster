# Governed-write agent skills and MCP toolkits for vSphere/VCF (as of 2026-10-22)

## vmware-skills family (Wei Zhou): what writes are gated, and how (aiops, pilot, policy, nsx-security, others)

### Takeaway
The vmware-skills family is the most complete governed-write toolkit available. It offers preview-then-`confirm=True` on destructive MCP tools, a shared `vmware-policy` layer (deny rules, maintenance windows, roles, append-only SQLite audit), and `vmware-pilot` workflows with approval gates and explicit, best-effort rollback. Every README says the same thing, though: tool exposure and policy are not authorization. The real boundary is the vCenter account's role. Policy can be bypassed with `VMWARE_POLICY_DISABLED`, 20 of 43 AIops write tools act on the first call, and Pilot first ran workflows on real vSphere only on 2026-10-18 (v2.4.0).

### Cited Findings

**Org, licences, versions, cadence**
- The org has 16 repos, all pushed 2026-10-18/20. They include VMware-AIops (79 stars, MIT), VMware-Monitor (read-only, "code-enforced safety", MIT), VMware-Pilot (4 stars), VMware-NSX-Security, VMware-NSX (32 MCP tools), VMware-VKS, VMware-Storage, VMware-Harden, VMware-AVI, VMware-VDI and VMware-PrivateAI. Every package is described as "a community-driven project by a VMware engineer, not an official VMware product". — [vmware-skills org](https://github.com/vmware-skills); [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot)
- Latest PyPI versions (queried from the PyPI JSON API on 2026-10-22):

  | Package | Version | Date | Releases |
  |---|---|---|---|
  | vmware-aiops | 2.4.2 | 2026-10-19 | 105 |
  | vmware-pilot | 2.4.0 | 2026-10-18 | 55 |
  | vmware-policy | 1.24.2 | 2026-10-18 | 59 |
  | vmware-debug | 2.3.0 | 2026-10-16 | 27 |
  | vmware-nsx-security | 1.17.1 | 2026-10-18 | 80 |
  | vmware-vks | 3.4.1 | 2026-10-19 | 96 |
  | vmware-aria | 2.4.1 | 2026-10-18 | 91 |
  | vmware-monitor | 2.4.2 | 2026-10-19 | 101 |

  — [PyPI vmware-aiops](https://pypi.org/project/vmware-aiops/), [vmware-pilot](https://pypi.org/project/vmware-pilot/), [vmware-policy](https://pypi.org/project/vmware-policy/), [vmware-nsx-security](https://pypi.org/project/vmware-nsx-security/), [vmware-vks](https://pypi.org/project/vmware-vks/), [vmware-aria](https://pypi.org/project/vmware-aria/), [vmware-debug](https://pypi.org/project/vmware-debug/), [vmware-monitor](https://pypi.org/project/vmware-monitor/)
- **Name collision:** on PyPI, `vmware-nsx` is version 17.0.0 from 2020-11-02, an unrelated OpenStack package. The vmware-skills NSX networking skill installs as `vmware-nsx-mgmt`. — [PyPI vmware-nsx](https://pypi.org/project/vmware-nsx/); [VMware-NSX-Security README](https://github.com/vmware-skills/VMware-NSX-Security)
- **Release cadence is very fast.** vmware-policy shipped 1.18.0 through 1.24.2 (13 releases) between 2026-10-14 and 2026-10-18. vmware-pilot shipped 2.0.0 through 2.4.0 between 2026-10-15 and 2026-10-18. Earlier releases cluster in bursts in Mar–Sep 2026. — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/); [PyPI vmware-pilot](https://pypi.org/project/vmware-pilot/)
- PyPI shows no provenance attestations and no Trusted Publishing for vmware-pilot releases. — [PyPI vmware-pilot](https://pypi.org/project/vmware-pilot/)
- Licence fields are MIT in the READMEs and repo listings. The PyPI JSON `license` field is empty for most packages; only vmware-vks sets it to MIT. — [org page](https://github.com/vmware-skills); [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)

**vmware-aiops (VM lifecycle, writes)**
- The README header says "61 tools". The Related Projects table says 49, and the text says "43 write tools an agent sees over MCP". The README is internally inconsistent. — [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops)
- **Gated:** 23 of the 43 write tools require `confirm=True`. These are "every tool annotated destructive, plus `migrate_vm` and the network/DRS authoring tools", for example `power_off_vm`, `migrate_vm`, `delete_cluster`, `reset_vcenter_alarm`, and the snapshot (revert/delete), guest, TTL, Clean Slate and plan-apply tools. Without `confirm=True` the call returns a no-write preview. `delete_vm` additionally requires the preview's `acknowledge_with` value. — [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops)
- **Ungated:** 20 write tools act on the first call: "creates, clones, deploys, power-on, reconfigure, snapshot create, host add, template and ISO operations, guest download, plan creation, alarm acknowledge". — [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops)
- The CLI asks for two sequential confirmations on destructive and deploy commands, with no bypass flag. The README itself notes that an agent can satisfy both with `yes |`. CLI `--dry-run` covers every write except `deploy iso`, `deploy mark-template`, `vm cancel-ttl` and `vm guest-download`. — [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops)
- There is "no read-only switch". `VMWARE_READ_ONLY` "stays removed", and the README recommends a read-only vCenter role instead. — [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops)
- The README names two audit locations: `~/.vmware-aiops/audit.log` (JSONL, "with before/after state") and `~/.vmware/audit.db`. It is unclear whether both are still written. — [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops)

**vmware-policy 1.24.2 (shared governance library, 2026-10-18)**
- **Deny rules** live in `~/.vmware/rules.yaml` (or `$OPS_HOME/rules.yaml`). Each rule has a `name`, `operations` (globs such as `"delete_*"` or `"remove_cluster_host"`), `environments` (e.g. `["production"]`) and a `reason`. Rules can also target `clients:` (token names, e.g. `"intern-*"`) and `roles:`. Glob semantics are not formally defined, and the README does not say how environment labels are assigned. Rules hot-reload. — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Maintenance window:** `start`/`end` (e.g. 22:00–06:00). Outside the window, "high/critical operations allowed only inside the window" applies. — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Risk levels:**
  - low: list/get/info/status
  - medium: reconfigure/update
  - high: power off, migrate, snapshot revert
  - critical: delete VM, delete cluster

  — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **`change_limits` is not enforced.** The README says `max_cpu_change_pct` and `max_memory_change_pct` are "reserved and not enforced". — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Fail modes depend on the rule source:**
  - A user rules file that exists but is unreadable denies everything.
  - A missing user file with an unreadable baseline denies everything.
  - A missing user file with a readable baseline loads the packaged default, which "denies nothing".

  — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Bypass:** with `VMWARE_POLICY_DISABLED`, "every policy check is skipped, including the unreadable-rules denial". Calls are still audited, with a `_bypassed` status suffix (`ok_bypassed`/`error_bypassed`). — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Audit:** `~/.vmware/audit.db` is an "append-only SQLite (WAL) audit log". It rotates at 100 MB and keeps 5 archives, with a 0700 directory and 0600 files. It records params, result, status, OS user and inferred agent, and redacts `sensitive_params` and credential-named keys. It is local only. — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Roles and tokens for network (HTTPS) callers:** only the SHA-256 hash of each token is stored, in `$OPS_HOME/mcp-tokens`. Built-in roles are `reader` (read-only tools) and `admin`. Custom roles go in an owner-only `$OPS_HOME/roles.yaml`, managed through `vmware-audit roles enable` and `token create --role`. Escape tools such as `issue_*_kubeconfig` and Aria `get_api_path` are flagged: "A role holding an escape tool is equivalent to the account." — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- **Tool exposure:** `--expose read` (env `VMWARE_<SKILL>_MCP_EXPOSE`) keeps only tools annotated `readOnlyHint: true`. Unannotated tools count as writes. `--allow-write` (env `VMWARE_<SKILL>_MCP_ALLOW_WRITE`) adds named writes back. The README says: "This limits what clients are offered. It is not authorization." — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/)
- This is the only place in the family where MCP tool annotations drive behaviour: `readOnlyHint` controls exposure, and "annotated destructive" controls which AIops tools require `confirm=True`. — [PyPI vmware-policy](https://pypi.org/project/vmware-policy/); [VMware-AIops README](https://github.com/vmware-skills/VMware-AIops); [Pilot RELEASE_NOTES 1.12.1](https://raw.githubusercontent.com/vmware-skills/VMware-Pilot/main/RELEASE_NOTES.md)

**vmware-pilot 2.4.0 (multi-step workflows)**
- **13 MCP tools:** `get_skill_catalog`, `list_workflows`, `review_workflow`, `design_workflow`, `update_draft`, `confirm_draft`, `create_workflow_from_template`, `create_workflow`, `run_workflow`, `get_workflow_status`, `approve`, `rollback` and `cancel_workflow`. The README describes them as 4 read and 9 write tools. — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot)
- **15 templates:** `clone_and_test`, `incident_response`, `investigate_alert`, `plan_and_approve`, `compliance_scan`, `network_segment_setup`, `vks_cluster_deploy`, `rolling_restart` (2+3n steps), `capacity_expansion`, `disaster_recovery`, `patch_deployment`, `storage_expansion`, `baseline_capture`, `baseline_audit` and `baseline_remediate`. Custom workflows are also supported, with a draft/confirm flow. — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot)
- **Approval gates:**
  - `create_workflow` is refused if a destructive step has no approval gate before it.
  - Since 1.12.0, steps that send `confirm=True` must come after a gate.
  - Since 2.4.0, `approve` refuses a gate whose earlier steps are `not_executed` unless the caller passes `acknowledge_not_executed=True`.
  - Since 2.3.0, `approve` can be restricted by role, and the creator can be barred from approving their own workflow. This needs policy 1.24.0 or later with roles enabled.

  — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot); [Pilot RELEASE_NOTES](https://raw.githubusercontent.com/vmware-skills/VMware-Pilot/main/RELEASE_NOTES.md)
- **Rollback** is "Explicit, best-effort undo of steps pilot recorded as succeeded — never automatic". It previews the `blast_radius` unless `confirm=True` is passed.
  - Since 2.4.0, a step with no undo is reported `not_reversible`, and a failed undo returns `rollback_incomplete`.
  - A step that exceeds `VMWARE_PILOT_DISPATCH_TIMEOUT` (default 1800 s) becomes `interrupted` with unknown effect, and rollback waits for manual verification.
  - `cancel_workflow` only stops future steps.

  — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot); [Pilot RELEASE_NOTES](https://raw.githubusercontent.com/vmware-skills/VMware-Pilot/main/RELEASE_NOTES.md); [Glama cancel_workflow](https://glama.ai/mcp/servers/vmware-skills/VMware-Pilot/tools/cancel_workflow)
- **Dispatch modes:**
  - **Default (agent-performed):** Pilot executes nothing. `run_workflow` returns steps as `pending_dispatch`, and the calling agent performs them through the companion skills. Under this default, gate enforcement depends on the agent's honesty.
  - **Pilot-run:** `--dispatch` or `VMWARE_PILOT_DISPATCH=stdio`, new in 2.4.0 and off by default. Pilot runs each step in that skill's own `vmware-<skill>-mcp` server. It is stdio only and refused with `--transport http/https`.

  — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot); [Pilot RELEASE_NOTES 2.4.0](https://raw.githubusercontent.com/vmware-skills/VMware-Pilot/main/RELEASE_NOTES.md)
- **Maturity:** the 2.4.0 release is titled "honest gates and rollback (first real-vSphere run)". It says the fixes were "Found running Pilot workflows on vCenter 8.0.3 for the first time".
  - Bugs found on that run: `clone_and_test` assumed a snapshot named `current`; it resized a clone after power-on, which vCenter refuses without hot-add; and an AIops linked-clone failure came back as prose and went undetected.
  - Earlier, in 1.10.0, failed steps were recorded as successes, so workflows reported `completed` after failing, and failed rollbacks were recorded as successes. That bug was found on live vCenter 8.0.3 on 2026-09-12.
  - In 1.10.1, `investigate_alert` called three tools that did not exist.
  - No test counts are published.

  — [Pilot RELEASE_NOTES](https://raw.githubusercontent.com/vmware-skills/VMware-Pilot/main/RELEASE_NOTES.md)
- **Remote access:** HTTPS mode (2.2.0) requires a bearer token. "Everyone who can reach the endpoint acts with this server's account". Tokens identify callers but "grant nothing on the platform". The README recommends one endpoint per privilege level. — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot); [PyPI vmware-pilot](https://pypi.org/project/vmware-pilot/)
- **`create_workflow_from_template` is a write, not a dry run.** The README says it "always created and saved a workflow from a template". — [VMware-Pilot README](https://github.com/vmware-skills/VMware-Pilot)

**vmware-nsx-security (blast-radius previews)**
- `delete_dfw_policy`, `delete_dfw_rule` and `delete_group` preview by default. Without `confirm=True` they return `blast_radius`, `blockers` and `unmeasured`. `confirm=True` is refused while a blocker remains or a read fails, and group deletion aborts if the reference scan fails. — [VMware-NSX-Security README](https://github.com/vmware-skills/VMware-NSX-Security)
- The README does not say whether create, update or tag tools need confirmation. CLI writes support `--dry-run`. Audit is written to `~/.vmware-nsx-security/audit.log`. Correlation IDs (`traceparent`/`X-Request-ID`) can be queried with `vmware-audit log --correlation-id`. — [VMware-NSX-Security README](https://github.com/vmware-skills/VMware-NSX-Security)

**Local context (this repo)**
- The homelab currently runs vmware-monitor 2.4.2, vmware-storage 2.4.2 and vmware-harden 1.16.0 with vmware-policy 1.24.2, under a vCenter Read-only account. The runbook already concludes that "the real boundary is the vCenter account, not the skill's policy layer". — `docs/vmware-skills-operator-runbook.md` (local)

### Inferences
- A safe graduation path is: a dedicated vCenter service account with a custom role scoped to a test folder/resource pool; `--expose read` plus a narrow `--allow-write` list; a user `rules.yaml` (so the default "deny nothing" baseline never applies); a maintenance window; and client-side `ask` permissions on every write tool. Each control below the vCenter role is advisory, because the agent process holds the credentials and could set `VMWARE_POLICY_DISABLED` or call pyVmomi directly if it has shell access.
- Prefer the gated AIops tools (power_off, migrate, snapshot revert/delete, delete_vm) for early experiments. The 20 ungated writes, particularly clone, deploy, reconfigure and host add, need a client-side `ask` rule, because nothing in the server pauses them.
- Pilot's approval gates are meaningful only in `--dispatch` mode or when the approver is a different principal from the agent (HTTPS plus roles plus creator-barred). In the default agent-performed mode, the agent that requests approval can also call `approve`.
- Pilot after only one real-vSphere run (four days ago) and policy at 13 releases in five days should be treated as beta. Pin exact versions.

### Gaps
- No published test counts or coverage for any vmware-skills package.
- vmware-debug (incident correlation) and vmware-aria details were not fetched beyond their PyPI version and date.
- vmware-vks write gating is not covered, apart from the fact that kubeconfig issuance is an "escape tool".
- The exact environment-label mechanism in rules.yaml is not documented.
- It is unclear whether AIops still writes `~/.vmware-aiops/audit.log` as well as `audit.db`.
- The full per-tool annotation table was not obtained.

## vchaindz/claude-vsphere-skill and other govc/PowerCLI-based skills or MCP servers

### Takeaway
claude-vsphere-skill is a govc-based Claude Code skill with an optional `guard/` PreToolUse hook. The hook classifies every `govc` invocation into readonly, standard or full tiers and fails closed to readonly. Its own README says it is text matching and "not a security boundary". No PowerCLI-based governed MCP server could be confirmed. Several community vCenter MCP servers claim RBAC and audit, but these claims were not verified.

### Cited Findings
- **claude-vsphere-skill:** Apache-2.0, 59 commits, no release or version; its example reports are dated 2026-07-30/31. — [claude-vsphere-skill](https://github.com/vchaindz/claude-vsphere-skill)
- **Guard tiers:**
  - `readonly`: reads only.
  - `standard`: reads and mutations, but destroy-class denied.
  - `full`: destroy-class allowed but "requires confirmation in the UI".

  The policy file is `~/.config/govc-guard/policy`. A missing or invalid file falls back to `readonly`. The installer adds settings.json deny rules that protect the policy file and the hook. — [claude-vsphere-skill](https://github.com/vchaindz/claude-vsphere-skill)
- **Classification:**
  - Reads: `*.info`, `find`, `collect`, `events` and metrics.
  - Destroy-class: an explicit list (e.g. `vm.destroy`, `snapshot.remove`) plus suffixes (`rm`, `remove`, `destroy`, `shutdown`, `reboot`, `unregister`) and the flags `-off`, `-reset`, `-suspend`, `-s` and `-r`.
  - Unknown subcommands are treated as mutations. Indeterminate invocations (`xargs govc`, `govc $(…)`) are treated as destroy-class.
  - `govc env` is denied at every tier unless it names one specific non-secret variable.

  — [claude-vsphere-skill](https://github.com/vchaindz/claude-vsphere-skill)
- **Known bypasses, per the README:** the guard matches command text and cannot see `curl … | bash`, and "a deliberately hostile agent could obfuscate around text matching". The README recommends a least-privilege vCenter role as the hard boundary. The base skill's "list objects and get explicit yes" rule is a model instruction, not code. — [claude-vsphere-skill](https://github.com/vchaindz/claude-vsphere-skill)
- **Tests:** `guard/test-policy.sh` (classifier, hook protocol, deployment) and 37 vcsim smoke tests. The guard runs on Linux and macOS; on Windows it needs WSL or Git Bash. — [claude-vsphere-skill](https://github.com/vchaindz/claude-vsphere-skill)
- **Other community MCP servers:**
  - mcp-vmware (Hokonoken): 39 tools for vSphere 7/8 with viewer/operator/vm_admin/infra_admin roles.
  - vSphere-MCP-Pro (Warezloder): claims audit logging, RBAC and rate limiting for vCenter 8.0+.
  - A read-only vSphere MCP server using the REST API.

  None was confirmed to use PowerCLI, and none was inspected in depth. — [Glama mcp-vmware](https://glama.ai/mcp/servers/Hokonoken/mcp-vmware); [Glama vSphere-MCP-Pro](https://glama.ai/mcp/servers/@Warezloder/vSphere-MCP-Pro); [Glama read-only vSphere MCP](https://glama.ai/mcp/servers/g7qpn61ltx)

### Inferences
- For a Claude Code user, the govc guard is a useful pattern. Its tier model (fail-closed, unknown treated as mutation, indeterminate treated as destroy) is stricter than AIops' "20 writes act immediately". It only applies, however, when the agent reaches vSphere through shell `govc`, not through MCP.

### Gaps
- No PowerCLI MCP server with governance was found; a "PowerCLI model context protocol" search was not run.
- The RBAC claims of mcp-vmware and vSphere-MCP-Pro, and whether they are enforced in code, were not verified.

## Generic MCP governance layers in front of any VMware MCP server

### Takeaway
Gateways can strip denied tools from `tools/list` and authorize per tool. Two examples are hoophq/mcpproxy (glob allow/deny, human approval holds over REST, JSONL audit) and agentgateway (CEL rules on tool name and JWT claims). Human pre-execution approval is rare in open source: mcpproxy has it, and the major gateways (Docker MCP Gateway, IBM ContextForge, Kong) reportedly do not.

### Cited Findings
- **hoophq/mcpproxy** (MIT, no tagged releases, 37 commits):
  - Policy is allow/deny globs, and denied tools are removed from `tools/list`.
  - Flagged calls are held for approval via `GET /approvals/` and `POST /approvals/{id}/approve|reject`, behind a bearer token. The timeout and its default outcome are undocumented.
  - Audit events (`mcp.tool_call`, `mcp.tool_denied`, `mcp.tools_changed`) go to logs and to per-session JSONL when `wal_dir` is set.
  - It can wrap stdio children and exposes streamable-HTTP `/mcp`.
  - It is single-replica, with state in memory.
  - It refuses the stateless MCP `2026-07-28` revision (error `-32022`) because rug-pull detection and budgets need sessions.

  — [hoophq/mcpproxy](https://github.com/hoophq/mcpproxy)
- **agentgateway:** CEL expressions evaluated on MCP `list_tools`/`call_tools`, and disallowed tools are filtered from list responses. Its examples gate tools by JWT `sub` or nested claims. In one version of the docs, rules OR together. — [agentgateway MCP authz](https://agentgateway.dev/docs/standalone/latest/mcp/mcp-authz/)
- **Agentic governance gateway (TH07008):** OPA Rego plus a JS evaluator, a human-in-the-loop gateway and an audit store. Maturity is unverified. — [Glama agentic-governance-gateway](https://glama.ai/mcp/servers/TH07008/agentic-governance-gateway)
- **Enkrypt AI MCP Gateway:** open-source inline gateway with "require approval" enforcement and approval receipts. Policy packs and audit exports are enterprise-tier. — [Enkrypt MCP gateway](https://www.enkryptai.com/product/mcp-gateway)
- **Vendor comparison (Maxim/Bifrost; vendor source, treat with caution):** rates Docker MCP Gateway and IBM ContextForge as partial on deny-by-default and lacking pre-execution approval, and Kong as ACL-only. — [Maxim article](https://www.getmaxim.ai/articles/top-5-mcp-gateways-for-securing-agent-tool-access-in-2026/); [IBM ContextForge](https://mcpservers.org/servers/IBM/mcp-context-forge)
- **MCP spec 2025-11-25 tool annotations:**
  - "clients **MUST** consider tool annotations to be untrusted unless they come from trusted servers".
  - "there **SHOULD** always be a human in the loop with the ability to deny tool invocations".
  - Clients SHOULD "Prompt for user confirmation on sensitive operations" and "Log tool usage for audit purposes".
  - Servers MUST "Implement proper access controls" and "Rate limit tool invocations".

  — [MCP spec: Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

### Inferences
- A gateway in front of vmware-* MCP servers adds an approval hold and audit that the agent's host cannot alter, but only if the gateway runs out of reach of the agent: a separate LXC that holds the vCenter credentials. If the agent can reach the MCP server or vCenter directly, the gateway is bypassable.
- Gateways that filter on `readOnlyHint` inherit the spec's warning that annotations are trustworthy only when the server is trusted. Name-based allowlists are more robust.

### Gaps
- Docker MCP Gateway and ContextForge features were not checked in primary docs.
- mcpproxy's approval-timeout behaviour is unknown.
- No current agentgateway version number was obtained.

## How MCP clients (Claude Code, opencode, Cursor) gate or disable MCP tools

### Takeaway
Claude Code has the strongest client-side controls: deny, ask and allow rules on `mcp__<server>__<tool>` with globs, where deny and ask always beat allow and hooks, and managed settings cannot be overridden. opencode can disable MCP tools by `servername_*` glob (legacy `tools`, now merged into `permission` with allow/ask/deny), but its permissions page does not document MCP tool keys explicitly. Cursor was not researched.

### Cited Findings
- **Claude Code rule syntax:** `mcp__puppeteer` or `mcp__puppeteer__*` matches every tool on a server, and `mcp__puppeteer__puppeteer_navigate` matches one tool. Rules are evaluated deny, then ask, then allow, so an allow can't carve an exception out of a deny. Deny and ask accept globs such as `"mcp__*"`. Allow globs must be anchored on a literal `mcp__<server>__` prefix. — [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- **Claude Code hooks and parameter rules:** PreToolUse hook decisions don't bypass deny or ask rules. A hook that exits with code 2 blocks the call even when an allow rule matches. Rules on MCP parameters can't go in settings files: `mcp__` rules with parentheses are skipped, and parameter-level denies must use `--disallowedTools`. Managed settings can't be overridden by any other level. — [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- **opencode tool naming:** "MCP server tools are registered with server name as prefix". Tools are disabled with `"tools": {"mymcpservername_*": false}` and re-enabled per agent with `agent.<name>.tools`. — [opencode MCP servers](https://opencode.ai/docs/mcp-servers/)
- **opencode permission:** keys take `allow`/`ask`/`deny` with `*` and `?` wildcards, and the last matching rule wins, so `"*"` goes first. Most permissions default to `allow`. The legacy `tools` boolean "is deprecated and has been merged into `permission`". The permissions page lists only built-in keys and does not mention MCP tool names. — [opencode permissions](https://opencode.ai/docs/permissions/)

### Inferences
- In opencode, `"permission": {"vmware-aiops_*": "ask"}` (with read tools set to `allow`) is likely to work given the merge, but it is not documented. Test it on a harmless tool before relying on it.
- opencode's default is `allow`, so an unconfigured MCP write tool runs without a prompt. Claude Code defaults to asking for MCP tools unless they are allowed.
- Client-side approval is per call and visible to the human. Combined with server-side `confirm=True` previews, it gives two checks: the preview shows the blast radius, and the human approves the confirm call.

### Gaps
- Cursor's MCP tool approval and allowlist controls were not researched.
- opencode's handling of `permission` keys for MCP tools was not verified against source or a live test.
