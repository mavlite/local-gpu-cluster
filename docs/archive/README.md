# Archive — historical reference

This folder holds documentation for **superseded** revisions of the cluster. It's
kept for context and for anyone replicating the older hardware, but it does **not**
describe the current build.

For the current environment, start at the repo [`README.md`](../../README.md) and
follow [`setup-runbook.md`](../../setup-runbook.md).

## Contents

| File | What it is |
|---|---|
| [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md) | **v1 build** — Dell Precision T7910 + 3× RTX 3060 + Ollama. Superseded by the v2 build (ASUS ProArt X870E + 2× AMD V620 + llama.cpp). Still useful for v1-style hardware and for a few patterns not carried into v2 (the SQLite-backed doc auto-updater, the 2500/500 RAG chunk-size finding, the no-think SSE proxy). |
| [`migration-ollama-to-llama-cpp.md`](./migration-ollama-to-llama-cpp.md) | **v1 migration plan (never executed)** — replacing Ollama with llama.cpp + llama-swap in place on the v1 host, at the same port and API surface. Overtaken by the v2 rebuild, which installed llama.cpp fresh. Useful for its parity-test gate, sub-minute rollback staging, and no-think SSE proxy notes. |
| [`migration-phase2-v620-hardware-swap.md`](./migration-phase2-v620-hardware-swap.md) | **superseded topology** — swapping two 3060s for two V620s and keeping one 3060, with per-card model assignment and no cross-card tensor split. The hybrid was abandoned for a V620-only build that does tensor-split. Useful for the cooling shroud / power work and the staged one-card-at-a-time rollout. |
