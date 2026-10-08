"""Hidden checks for w2-08: tabs and CRLF count as whitespace for marker placement too."""
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("trafilatura", "requests"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

from handlers.sphinx_sitemap import _interleave_source  # noqa: E402

URL = "https://example.org/docs/page.html"


def gaps(out):
    pos = [m.start() for m in re.finditer(r"\[Source:", out)]
    bounds = [0] + pos + [len(out)]
    return len(pos), max(bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1))


def test_a_tab_separated_block_is_interleaved():
    body = "\t".join("cell%04d" % i for i in range(700))
    n, widest = gaps(_interleave_source(body, URL))
    assert n > 1 and widest < 2000


def test_a_crlf_separated_block_is_interleaved_without_losing_text():
    body = "\r\n".join("include::topics/part%03d.adoc[]" % i for i in range(180))
    out = _interleave_source(body, URL)
    n, widest = gaps(out)
    assert n > 1 and widest < 2000
    stripped = out.replace("[Source: %s]" % URL, "")
    assert re.sub(r"\s+", "", stripped) == re.sub(r"\s+", "", body)
