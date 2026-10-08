"""Hidden checks for w2-10: a page of many short paragraphs plus one long block is covered."""
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("trafilatura", "requests"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from handlers.sphinx_sitemap import _interleave_source  # noqa: E402

URL = "https://techdocs.example/vcf/9-1/page.html"


def test_every_2000_char_window_holds_a_marker():
    paras = ["Short paragraph %d about vSAN storage policies." % i for i in range(120)]
    paras.insert(60, " ".join("row%04d | value | value | value" % i for i in range(200)))
    out = _interleave_source("\n\n".join(paras), URL)
    pos = [m.start() for m in re.finditer(r"\[Source:", out)]
    bounds = [0] + pos + [len(out)]
    assert max(bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)) < 2000
    assert all(URL in out[p:p + 200] for p in pos)


def test_a_short_page_is_left_alone():
    body = "One short paragraph.\n\nAnother one."
    assert _interleave_source(body, URL).replace("[Source: %s]" % URL, "").strip() == body
