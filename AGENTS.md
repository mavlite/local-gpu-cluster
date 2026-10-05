# AGENTS.md — local-gpu-cluster

Automation for a local GPU cluster: a Proxmox host (`192.168.6.175`) plus LXCs running
llama.cpp (ROCm/V620), an OpenAI-compatible router, AnythingLLM, and MCP servers. The code
is mostly **Bash** (`scripts/*.sh`) and **Python** (`scripts/rag/`, `scripts/tools/`,
`scripts/files/`).

## Checks — run before declaring a coding task done
No project-wide linter/formatter is configured. Match the surrounding style; do **not** add
ruff/black/mypy or change build config without asking.

- **Python tests:** `python3 -m pytest scripts/rag/tests scripts/files/tests -q`
  - Deps: `scripts/rag/requirements.txt` (PyYAML, requests, trafilatura). If imports fail,
    `pip install -r scripts/rag/requirements.txt` first. On the host, the RAG code runs under
    `/opt/vcf-scraper-venv`.
- **Bash scripts:** `bash -n <file>.sh` for every edited script; also run `shellcheck <file>.sh`
  if shellcheck is available.
- Show the **real** command output, not a summary of it. Fix failures and re-run — never weaken
  a test or delete an assertion to make it pass.

## Conventions & footguns (each learned the hard way)
- **LXC deployment model:** every service runs in an LXC; new services and migrations follow
  that pattern.
- **Reaching the cluster:** `pct exec <ctid> -- …` from the host. Nested ssh/pct/docker quoting
  breaks — transfer scripts via `base64 -d` or `pct push`, never inline complex quotes/heredocs.
- **`/dev/tcp` gives false negatives inside containers** — verify connectivity with `curl`.
- **ROCm/GPU tooling lives in LXC 151, not the host** — `pct exec 151 -- rocm-smi`.
- **Commits:** conventional commits (`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:`),
  and **no `Co-Authored-By` trailer** — commits are attributed solely to the author.
- The `cluster-ops` and `vcf-lookup` opencode skills carry the full topology, router aliases,
  and RAG-query conventions — consult them for infra/RAG work.
