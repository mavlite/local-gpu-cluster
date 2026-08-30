# Underspecified tasks: NO visible tests at all.
#
# The agent gets a goal and the code, no runnable oracle, and is graded entirely
# on a hidden suite. This removes the crutch of iterating against assertions and
# asks whether the model can infer requirements from a description -- much closer
# to how real work arrives.
#
# `verify` is deliberately absent, so run_tests tells the agent there is no
# suite and to reason instead. That is the point of the set, not an oversight.

OPEN_TASKS = [
{
 # A pure-arithmetic counter race was tried first and REJECTED by the
 # validator: under CPython's GIL, "self._n += by" does not lose updates even
 # at 8 threads x 50,000 iterations with a 1ns switch interval, so the suite
 # passed on the broken source and measured nothing.
 #
 # Check-then-act across a real yield point does race, reliably: 16 of 16
 # threads win a slot that only one should get.
 "name": "threadsafe_reserve",
 "goal": ("Reserver.reserve() is supposed to grant a key to exactly one caller "
          "-- later callers must get False. Under concurrent use it grants the "
          "same key many times. Fix it, keeping the public API (reserve, "
          "granted). There are no tests; reason about correctness yourself."),
 "files": {
   "reserver.py": (
     "import time\n"
     "\n"
     "class Reserver:\n"
     "    def __init__(self):\n"
     "        self._taken = set()\n"
     "        self._granted = []\n"
     "\n"
     "    def reserve(self, key):\n"
     "        if key in self._taken:\n"
     "            return False\n"
     "        time.sleep(0.002)  # stands in for an I/O round-trip\n"
     "        self._taken.add(key)\n"
     "        self._granted.append(key)\n"
     "        return True\n"
     "\n"
     "    def granted(self):\n"
     "        return list(self._granted)\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "import threading\n"
     "from reserver import Reserver\n"
     "\n"
     "def test_single_threaded_contract():\n"
     "    r = Reserver()\n"
     "    assert r.reserve('a') is True\n"
     "    assert r.reserve('a') is False\n"
     "    assert r.reserve('b') is True\n"
     "    assert sorted(r.granted()) == ['a', 'b']\n"
     "\n"
     "def test_only_one_winner_under_contention():\n"
     "    r = Reserver()\n"
     "    N = 16\n"
     "    wins = []\n"
     "    lock = threading.Lock()\n"
     "    bar = threading.Barrier(N)\n"
     "    def w():\n"
     "        bar.wait()\n"
     "        got = r.reserve('slot')\n"
     "        with lock:\n"
     "            wins.append(got)\n"
     "    ts = [threading.Thread(target=w) for _ in range(N)]\n"
     "    for t in ts: t.start()\n"
     "    for t in ts: t.join()\n"
     "    assert sum(1 for x in wins if x) == 1, (\n"
     "        'exactly one caller may win, got %d' % sum(1 for x in wins if x))\n"
     "    assert r.granted().count('slot') == 1\n"
     "\n"
     "def test_distinct_keys_all_succeed():\n"
     "    r = Reserver()\n"
     "    N = 8\n"
     "    bar = threading.Barrier(N)\n"
     "    def w(i):\n"
     "        bar.wait()\n"
     "        r.reserve('k%d' % i)\n"
     "    ts = [threading.Thread(target=w, args=(i,)) for i in range(N)]\n"
     "    for t in ts: t.start()\n"
     "    for t in ts: t.join()\n"
     "    assert sorted(r.granted()) == sorted('k%d' % i for i in range(N))\n"
   ),
 },
 "verify": None,
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
{
 "name": "safe_path_join",
 "goal": ("safe_join(root, user_path) in paths.py is meant to join a "
          "user-supplied path under root WITHOUT allowing escape outside root. "
          "It is currently unsafe. Fix it. There are no tests; reason about "
          "what an attacker would try."),
 "files": {
   "paths.py": (
     "import os\n"
     "\n"
     "def safe_join(root, user_path):\n"
     "    return os.path.join(root, user_path)\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "import os, pytest\n"
     "from paths import safe_join\n"
     "\n"
     "ROOT = '/srv/data'\n"
     "\n"
     "def _escaped(p):\n"
     "    return not os.path.abspath(p).startswith(os.path.abspath(ROOT) + os.sep)\n"
     "\n"
     "def test_normal_join_still_works():\n"
     "    p = safe_join(ROOT, 'a/b.txt')\n"
     "    assert os.path.abspath(p) == os.path.abspath('/srv/data/a/b.txt')\n"
     "\n"
     "def test_dotdot_is_blocked():\n"
     "    for bad in ('../etc/passwd', 'a/../../etc/passwd', '..'):\n"
     "        try:\n"
     "            p = safe_join(ROOT, bad)\n"
     "        except (ValueError, PermissionError):\n"
     "            continue\n"
     "        assert not _escaped(p), 'escaped root with %r -> %r' % (bad, p)\n"
     "\n"
     "def test_absolute_path_is_blocked():\n"
     "    try:\n"
     "        p = safe_join(ROOT, '/etc/passwd')\n"
     "    except (ValueError, PermissionError):\n"
     "        return\n"
     "    assert not _escaped(p), 'absolute path escaped root: %r' % p\n"
   ),
 },
 "verify": None,
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
]
