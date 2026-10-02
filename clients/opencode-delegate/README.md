# opencode-delegate overlay

Trusted opencode configuration for local-delegate agentic jobs. Consumed by
`scripts/delegate/overlay.py`; never edited per job.

- `agent/delegate.md` - locked-down `delegate` agent (edit in-dir; bash limited to
  cat/ls/rg/sed -n; webfetch and external_directory denied; only `delegate-*` skills).
- `allowlist.toml` - argv-prefix allow-list of CHECK commands the server may run
  after a job. Distinct from the agent's own bash permissions.
- `opencode.json` - base project config; `install_overlay` writes the effective one
  (router provider, `{env:LOCAL_DELEGATE_ROUTER_TOKEN}` key, searxng only if allow_web).

Per job: `strip_project_config` removes attacker `opencode.json*`, `.opencode/`,
`AGENTS.md`, `CLAUDE.md` from the exported dir, then `install_overlay` writes ours.
`opencode_env` points HOME/USERPROFILE/XDG_CONFIG_HOME at an isolated scratch home so
the user's global RULES.md, skills and MCPs are not inherited.
