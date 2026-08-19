# Agentic tasks. Each builds a REAL temp repo; the agent must drive tools until
# the hidden verification command passes. No eyeballing, no single-turn answers.

TASKS = [
{
 "name": "fix_failing_test",
 "goal": "The test suite in this repo is failing. Find the bug, fix it, and make all tests pass.",
 "files": {
   "calc.py": (
     "def apply_discount(price, pct):\n"
     "    # pct is a percentage, e.g. 20 means 20% off\n"
     "    return price - pct\n"
     "\n"
     "def total(items):\n"
     "    return sum(i['price'] * i['qty'] for i in items)\n"
   ),
   "test_calc.py": (
     "from calc import apply_discount, total\n"
     "\n"
     "def test_discount():\n"
     "    assert apply_discount(100, 20) == 80\n"
     "    assert apply_discount(50, 10) == 45\n"
     "\n"
     "def test_total():\n"
     "    assert total([{'price':2,'qty':3},{'price':5,'qty':1}]) == 11\n"
   ),
 },
 "verify": "python3 -m pytest -q test_calc.py",
},
{
 "name": "implement_from_tests",
 "goal": "test_stack.py describes a class that does not exist yet. Read the tests and implement it so they all pass.",
 "files": {
   "test_stack.py": (
     "import pytest\n"
     "from stack import BoundedStack\n"
     "\n"
     "def test_push_pop():\n"
     "    s = BoundedStack(3)\n"
     "    s.push(1); s.push(2)\n"
     "    assert s.pop() == 2\n"
     "    assert len(s) == 1\n"
     "\n"
     "def test_overflow():\n"
     "    s = BoundedStack(2)\n"
     "    s.push(1); s.push(2)\n"
     "    with pytest.raises(OverflowError):\n"
     "        s.push(3)\n"
     "\n"
     "def test_underflow():\n"
     "    s = BoundedStack(2)\n"
     "    with pytest.raises(IndexError):\n"
     "        s.pop()\n"
     "\n"
     "def test_peek_does_not_remove():\n"
     "    s = BoundedStack(2)\n"
     "    s.push(9)\n"
     "    assert s.peek() == 9\n"
     "    assert len(s) == 1\n"
   ),
 },
 "verify": "python3 -m pytest -q test_stack.py",
},
{
 "name": "multifile_refactor",
 "goal": ("Rename the function `fetch_data` to `load_records` everywhere in this repo, "
          "updating every caller and import. All tests must still pass afterwards."),
 "files": {
   "db.py": "def fetch_data(conn):\n    return conn.get('rows')\n",
   "service.py": (
     "from db import fetch_data\n"
     "\n"
     "def summarize(conn):\n"
     "    rows = fetch_data(conn)\n"
     "    return len(rows)\n"
   ),
   "report.py": (
     "from db import fetch_data\n"
     "\n"
     "def build(conn):\n"
     "    return {'n': len(fetch_data(conn))}\n"
   ),
   "test_all.py": (
     "from service import summarize\n"
     "from report import build\n"
     "import db, inspect\n"
     "\n"
     "class FakeConn:\n"
     "    def get(self, k): return [1,2,3]\n"
     "\n"
     "def test_behaviour():\n"
     "    c = FakeConn()\n"
     "    assert summarize(c) == 3\n"
     "    assert build(c) == {'n': 3}\n"
     "\n"
     "def test_renamed():\n"
     "    assert hasattr(db, 'load_records'), 'db.load_records must exist'\n"
     "    assert not hasattr(db, 'fetch_data'), 'old name must be gone'\n"
     "    src = inspect.getsource(db) + inspect.getsource(__import__('service')) + inspect.getsource(__import__('report'))\n"
     "    assert 'fetch_data' not in src, 'no references to the old name may remain'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_all.py",
},
{
 "name": "error_recovery",
 "goal": ("Add a function `slugify(text)` to the module that already contains the other string "
          "helpers in this repo, then make sure the test suite passes. You do not know the "
          "filename yet."),
 "files": {
   "strhelp.py": (
     "def titlecase(s):\n"
     "    return ' '.join(w.capitalize() for w in s.split())\n"
   ),
   "test_slug.py": (
     "from strhelp import slugify, titlecase\n"
     "\n"
     "def test_slug():\n"
     "    assert slugify('Hello World') == 'hello-world'\n"
     "    assert slugify('  A  B  ') == 'a-b'\n"
     "    assert slugify('Already-Slug') == 'already-slug'\n"
     "\n"
     "def test_existing_untouched():\n"
     "    assert titlecase('a b') == 'A B'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_slug.py",
},
]
