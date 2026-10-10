## Gate window: the nightly RAG refresh can run against the stopped embed server

`scripts/tools/gate/sh/gate_env.sh pin` prepares the host for a measurement window. It saves the prior
state, stops every unit that could disturb the window, and `restore` puts back exactly what `pin`
found.

The window also stops embed and rerank (decision of 2026-10-05). But `rag-refresh.timer` is not among
the units `pin` handles, so the nightly refresh can fire during a window against a stopped embed
server and fail. Nothing restores it afterwards either.

**Wanted:**
- `pin` stops `rag-refresh.timer` along with the other host units, and records its prior state.
- `restore` starts it again only if it was active before.

`scripts/tools/gate/tests/test_gate_env.py` drives the script against fake `pct`/`systemctl`/`curl`.
