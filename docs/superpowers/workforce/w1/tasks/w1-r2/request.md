## RAG refresh: a re-ingest times out because one embedding call carries every removal

The refresh sends the workspace embedding update in batches of `embed_batch_size` added documents,
but every removal goes along with the **first** call. That was fine for backfills, where removals are
a handful. A re-ingest turns every document into an update, so removals equal additions.

On vcf-core-docs (610 additions, 610 removals, batch size 50) the first call carried 50 additions and
all 610 removals. It ran past the 1800 s client timeout and took the run down, leaving 574 documents
uploaded and tracked but not attached. A 118-removal pass had already taken 1140 s, so the cost
follows the number of removals.

**Wanted:** neither side of any single call exceeds the batch size, nothing is dropped or sent twice,
and a run with only removals or only additions still works. The batching should be testable on its
own; `scripts/rag/tests/test_embed_batches.py` shows the interface the tests expect.
