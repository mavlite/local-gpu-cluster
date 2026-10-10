## Router: `qwen3.8-nothink` lost and runs with thinking on

Since the 2026-09-08 router deploy, requests for the `qwen3.8-nothink` alias have fallen through as
an unknown model. They run with thinking **on**, at the chat template's highest effort, which is the
runaway mode the alias exists to prevent. The deploy replaced the live `app.py` with the repo copy,
and the repo copy never had the alias.

Separately, clients that send no sampling parameters (AnythingLLM, for example) get llama-server's
generic defaults rather than the Qwen3.8 model card's.

**Wanted:**
- `qwen3.8-nothink` is back, mapped to the `qwen3.8` backend with thinking off.
- Every Qwen3.8 alias injects the model card's sampling for its mode. For non-thinking that includes
  presence penalty 1.5.
- Defaults never override a value the client sent, including an explicit `null`.
- The per-alias default logic lives in a small stdlib-only module, so it can be unit-tested without
  FastAPI. `scripts/files/tests/test_alias_defaults.py` shows the expected interface.
