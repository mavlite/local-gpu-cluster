## RAG refresh: budgeted sources re-fetch the same tranche forever, and some pages lose citations

Two defects, both found while validating keycloak-docs (6 documents) before it reached 983 on a timer.

**1. Budgeted sources never advance.** A budgeted source fetches the documents with the oldest
`last_fetched` first. But a document that is fetched and found **unchanged** never gets a new
`last_fetched`, so the same tranche is selected on every run.
- After a 17-hour VCF re-ingest reported as complete, 2,525 of 4,421 vcf-core-docs URLs still had a
  stale `last_fetched`.
- Only about 38% of the corpus had actually been re-ingested.
- The falling per-tranche update counts looked like convergence, but they were the same ~900 URLs
  being re-fetched.

**2. Some long pages get almost no `[Source: <url>]` markers.** The markers are interleaved through a
page's body so every chunk carries a citable URL. Keycloak's `server_admin.adoc` is about 5,000
characters of `include::topics/x.adoc[]` lines with **no spaces**. Its oversized block got one marker,
after the whole block: a 5,124-character stretch with no URL.

**Wanted:**
- Every collected URL gets its `last_fetched` stamped, changed or not.
- Oversized blocks are split at **any** whitespace for marker placement.
- No content is lost.
- A run with no whitespace at all stays a documented limit.

`scripts/rag/tests/test_source_interleave.py` covers the markers.
