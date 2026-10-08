# AnythingLLM offline re-embed

Re-embeds every stored chunk **in place** after an embedder change (model, quant, or numerics such
as flash attention). Used 2026-10-05: 19,891 chunks, 1.5-minute outage.

**Why not AnythingLLM's own paths (v1.16.0):**
- `vector-cache/` is keyed by document path only, so remove-and-re-add reuses the OLD vectors.
- Changing the embedder setting empties every workspace and re-embeds nothing.

Each LanceDB row's `text` is exactly what the embedder received, so the text can be re-embedded
verbatim and only the `vector` column rewritten. IDs, documents and pins are kept.

| Step | Script | Writes | Outage |
|---|---|---|---|
| Pause `rag-refresh.timer` on the Proxmox host | — | — | no |
| Baseline | `node retrieval.js baseline` | `reembed/retrieval-baseline.json` | no |
| Embed | `node embed.js` (resumable) | `reembed/staging/*.jsonl` | no |
| Validate | `node validate.js` (ids, dim, finite, cos(old,new) ≥ 0.99 every row) | report | no |
| Stop container, `zfs snapshot`, copy db/documents/cache to the host | — | backup | **yes** |
| Build | `node build.js` via `docker run --rm --user 1000:1000` with the pinned image digest | `lancedb.new/` | yes |
| Swap `lancedb` / `vector-cache`, empty cache (`chown 101000`), start | — | — | yes |
| Verify | `node retrieval.js verify` + `python3 postcheck.py` (host) | report | no |
| Resume `rag-refresh.timer` | — | — | no |

The Node scripts run **inside** the `anythingllm` container from
`/app/server/storage/reembed/` (the host path is `/tank/anythingllm/storage/reembed/`), using its
`@lancedb/lancedb` and its embedding env. The router key is never printed.
`postcheck.py` runs on the Proxmox host and reads `ALLM_API_KEY` from `scripts/config.env`.
Full procedure and numbers: the session memory note `anythingllm_reembed_procedure`.
