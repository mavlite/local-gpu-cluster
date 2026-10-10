"""Hidden checks for w2-09: the searchindex fetch identifies itself; nested docnames map to URLs."""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("trafilatura",):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import handlers.sphinx_sitemap as sm  # noqa: E402

BASE = "https://api.truenas.com/v27.0"


class Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200
        self.ok = True

    def raise_for_status(self):
        pass


def test_searchindex_is_fetched_with_an_identifying_user_agent(monkeypatch):
    calls = []

    def get(url, *a, **k):
        calls.append((url, k))
        return Resp("Search.setIndex(%s)" % json.dumps({"docnames": ["api/nfs", "index"]}))

    monkeypatch.setattr(sm.requests, "get", get, raising=False)
    urls = sm.SphinxSitemapHandler()._try_searchindex(BASE + "/searchindex.js", BASE, 10)
    assert urls == [BASE + "/api/nfs.html", BASE + "/index.html"]
    assert calls and calls[0][0] == BASE + "/searchindex.js"
    ua = (calls[0][1].get("headers") or {}).get("User-Agent", "")
    assert ua and "python-requests" not in ua
