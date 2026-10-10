## RAG: truenas-api-v27 silently misses documents, and our fetches have no User-Agent

Found while auditing the truenas-api-v27 source. There are two silent-degradation bugs.

**1. Discovery quietly falls back.**
- `api.truenas.com/v27.0/sitemap.xml` returns a 143-byte HTML stub, which parses as zero `<loc>`
  entries.
- The handler then falls back to scraping index pages and still reports healthy.
- It reached 1,098 documents. Sphinx's own inventory, `searchindex.js`, lists 1,105.
- The 8 missing include `api_methods.html`, `rbac.html`, `jobs.html`, and one method page that no index
  page links to.

**2. No User-Agent.** Every request goes out with the HTTP library's default User-Agent. Some sites
throttle or block that.

**Wanted:**
- For Sphinx sites, discovery tries `searchindex.js` first (`docnames` → `<base>/<docname>.html`),
  then the sitemap, then index scraping.
- Each rung says which one was used, so a fallback never looks like success.
- `searchindex.js` failures (HTTP errors, malformed JSON, a missing key) fall through rather than
  crash.
- All requests the handler makes send an identifying User-Agent.

`scripts/rag/tests/test_searchindex.py` shows the expected interface.
