## RAG: middle chunks of long pages have no citable URL, and the monitor warns about GPUs forever

**1. Citation gap.** AnythingLLM chunks a document without keeping per-document metadata, so the URL
written once in the ingest header reaches only the **first** chunk. When retrieval surfaces a middle
chunk of a long page, the model gets no link. It either omits the citation or invents one from a
filename. Measured: 8 of 10 chunks carried a URL on typical queries, and a 4,165-character answer
cited nothing.

**Wanted:** the Sphinx handler repeats a `[Source: <url>]` marker through the page body, so **every**
chunk contains one, for chunk sizes from 2,000 to 8,200 characters (production chunks measured
2,700–8,200). Two shapes must be covered:
- the tail after the last marker;
- a single paragraph longer than the marker spacing, such as a table or code block.

Keep the text overhead modest, and leave short pages untouched.

**2. A GPU warning that never clears.** The monitor's GPU card check reports a permanent warning on a
healthy cluster. Bring it back to ok when the cards are fine.

`scripts/rag/tests/test_source_interleave.py` shows the expected interface.
