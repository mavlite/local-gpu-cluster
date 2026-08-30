# Hard agentic tasks. The original four saturated: qwen3.8 and Coder-Next tied
# 30/32, which cannot discriminate between models.
#
# Each task here is built so the OBVIOUS solution FAILS. Passing requires
# reading the tests carefully, reasoning about a non-local interaction, and
# verifying by running the suite rather than pattern-matching a familiar shape.
#
# Every task still ends in a deterministic hidden verify command, so grading
# stays objective. Difficulty comes from the problem, not from ambiguity.

HARD_TASKS = [
{
 # Trap: shared mutable default. The common "fix" (copy inside the method)
 # still leaks, because every instance shares the SAME default dict object.
 # A second test then forbids aliasing the caller's dict on the way out.
 "name": "shared_state_leak",
 "goal": ("Registry instances are leaking state into each other. Fix it so each "
          "instance is independent. Do not change the public API."),
 "files": {
   "registry.py": (
     "class Registry:\n"
     "    def __init__(self, items={}):\n"
     "        self.items = items\n"
     "\n"
     "    def add(self, k, v):\n"
     "        self.items[k] = v\n"
     "        return self\n"
     "\n"
     "    def snapshot(self):\n"
     "        return self.items\n"
   ),
   "test_registry.py": (
     "from registry import Registry\n"
     "\n"
     "def test_instances_are_independent():\n"
     "    a = Registry(); b = Registry()\n"
     "    a.add('x', 1)\n"
     "    assert b.snapshot() == {}, 'b saw a-s data'\n"
     "\n"
     "def test_explicit_dict_still_works():\n"
     "    seed = {'k': 0}\n"
     "    r = Registry(seed)\n"
     "    r.add('j', 1)\n"
     "    assert r.snapshot()['k'] == 0\n"
     "\n"
     "def test_snapshot_is_not_live():\n"
     "    r = Registry()\n"
     "    s = r.snapshot()\n"
     "    r.add('later', 9)\n"
     "    assert 'later' not in s, 'snapshot must not alias internal state'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_registry.py",
},
{
 # Trap: three distinct edge cases (duplicates, absent-between, empty) that a
 # textbook binary search gets wrong in different ways, plus a timing test that
 # rejects a linear scan.
 "name": "search_range_edges",
 "goal": ("Implement search_range(arr, target) in ranges.py. It returns the "
          "(first, last) indices of target in a sorted list, or (-1, -1) if "
          "absent. Read the tests for the exact contract."),
 "files": {
   "ranges.py": "# implement search_range here\n",
   "test_ranges.py": (
     "import time\n"
     "from ranges import search_range\n"
     "\n"
     "def test_duplicates():\n"
     "    assert search_range([1,2,2,2,3], 2) == (1, 3)\n"
     "\n"
     "def test_single():\n"
     "    assert search_range([1,2,3], 2) == (1, 1)\n"
     "\n"
     "def test_absent_between():\n"
     "    assert search_range([1,3,5], 4) == (-1, -1)\n"
     "\n"
     "def test_absent_beyond():\n"
     "    assert search_range([1,3,5], 9) == (-1, -1)\n"
     "\n"
     "def test_empty():\n"
     "    assert search_range([], 1) == (-1, -1)\n"
     "\n"
     "def test_all_same():\n"
     "    assert search_range([7,7,7,7], 7) == (0, 3)\n"
     "\n"
     "def test_must_be_logarithmic():\n"
     "    big = [0]*1000000 + [1]*1000000\n"
     "    t0 = time.time()\n"
     "    for _ in range(200):\n"
     "        assert search_range(big, 1) == (1000000, 1999999)\n"
     "    assert time.time() - t0 < 2.0, 'too slow - must be O(log n)'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_ranges.py",
},
{
 # Trap: the obvious rewrite is set()-based, which explodes on unhashable
 # items. Must stay order-preserving AND handle lists.
 "name": "quadratic_dedupe",
 "goal": ("dedupe() in uniq.py is correct but far too slow on large inputs. "
          "Make it fast without changing its behaviour."),
 "files": {
   "uniq.py": (
     "def dedupe(items):\n"
     "    out = []\n"
     "    for it in items:\n"
     "        if it not in out:\n"
     "            out.append(it)\n"
     "    return out\n"
   ),
   "test_uniq.py": (
     "import time\n"
     "from uniq import dedupe\n"
     "\n"
     "def test_behaviour_preserved():\n"
     "    assert dedupe([3,1,3,2,1]) == [3,1,2]\n"
     "    assert dedupe([]) == []\n"
     "    assert dedupe(['b','a','b']) == ['b','a']\n"
     "\n"
     "def test_unhashable_still_works():\n"
     "    assert dedupe([[1],[2],[1]]) == [[1],[2]]\n"
     "\n"
     "def test_fast_enough():\n"
     "    data = list(range(30000)) * 2\n"
     "    t0 = time.time()\n"
     "    out = dedupe(data)\n"
     "    assert out == list(range(30000))\n"
     "    assert time.time() - t0 < 1.0, 'still quadratic'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_uniq.py",
},
{
 # Trap: None must NOT mask a lower-precedence value, but empty string MUST.
 # A plain dict-update chain gets both wrong.
 "name": "config_precedence",
 "goal": ("Implement resolve(explicit, env, filecfg, defaults) in conf.py to "
          "merge configuration with the precedence the tests require. Note "
          "carefully how None and empty string differ."),
 "files": {
   "conf.py": "# implement resolve(explicit, env, filecfg, defaults) here\n",
   "test_conf.py": (
     "from conf import resolve\n"
     "\n"
     "D = {'host': 'localhost', 'port': 80, 'tag': 'default'}\n"
     "\n"
     "def test_explicit_wins():\n"
     "    assert resolve({'port': 9}, {'port': 8}, {'port': 7}, D)['port'] == 9\n"
     "\n"
     "def test_env_beats_file():\n"
     "    assert resolve({}, {'port': 8}, {'port': 7}, D)['port'] == 8\n"
     "\n"
     "def test_falls_through_to_default():\n"
     "    assert resolve({}, {}, {}, D)['host'] == 'localhost'\n"
     "\n"
     "def test_none_means_unset_not_a_value():\n"
     "    assert resolve({'tag': None}, {'tag': 'fromenv'}, {}, D)['tag'] == 'fromenv'\n"
     "\n"
     "def test_empty_string_IS_a_value():\n"
     "    assert resolve({'tag': ''}, {'tag': 'fromenv'}, {}, D)['tag'] == ''\n"
     "\n"
     "def test_unknown_keys_preserved():\n"
     "    r = resolve({'extra': 1}, {}, {}, D)\n"
     "    assert r['extra'] == 1 and r['host'] == 'localhost'\n"
     "\n"
     "def test_inputs_not_mutated():\n"
     "    e = {'port': 9}\n"
     "    resolve(e, {}, {}, D)\n"
     "    assert e == {'port': 9}\n"
     "    assert D == {'host': 'localhost', 'port': 80, 'tag': 'default'}\n"
   ),
 },
 "verify": "python3 -m pytest -q test_conf.py",
},
{
 # Trap: the failure surfaces in report/aggregate, but the cause is parse_row
 # returning a string qty. A local fix in aggregate.py (int() there) passes the
 # first test and fails the second, which pins parse_row's contract.
 "name": "indirect_root_cause",
 "goal": ("The report test fails. Find the real cause and fix it at its source. "
          "Both tests must pass."),
 "files": {
   "parse.py": (
     "def parse_row(line):\n"
     "    # returns (name, qty)\n"
     "    name, qty = line.split(',')\n"
     "    return name.strip(), qty.strip()\n"
   ),
   "aggregate.py": (
     "from parse import parse_row\n"
     "\n"
     "def totals(lines):\n"
     "    acc = {}\n"
     "    for ln in lines:\n"
     "        name, qty = parse_row(ln)\n"
     "        acc[name] = acc.get(name, 0) + qty\n"
     "    return acc\n"
   ),
   "report.py": (
     "from aggregate import totals\n"
     "\n"
     "def render(lines):\n"
     "    return sorted((k, v) for k, v in totals(lines).items())\n"
   ),
   "test_report.py": (
     "from report import render\n"
     "from parse import parse_row\n"
     "\n"
     "def test_render_sums():\n"
     "    assert render(['a, 2', 'b, 3', 'a, 4']) == [('a', 6), ('b', 3)]\n"
     "\n"
     "def test_parse_row_returns_int_qty():\n"
     "    assert parse_row('x, 7') == ('x', 7)\n"
   ),
 },
 "verify": "python3 -m pytest -q test_report.py",
},
]
