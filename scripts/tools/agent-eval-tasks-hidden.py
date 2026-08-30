# Hidden-test tasks. The agent sees test_visible.py and is graded on
# test_hidden.py, which is written only after it stops.
#
# The design rule for every task here: it must be possible to pass the VISIBLE
# tests with a solution that fails the HIDDEN ones. If a naive solution passes
# both, the hidden tests are decorative and the task measures nothing new.
# validate_all.py checks exactly that with a deliberately naive "cheat"
# solution -- a task where the cheat passes hidden is rejected.
#
# Visible tests cover the obvious cases. Hidden tests cover what those cases
# IMPLY: boundaries, type contracts, mutation, degenerate inputs.

HIDDEN_TASKS = [
{
 "name": "slugify_intent",
 "goal": ("Implement slugify(text) in slug.py. The visible tests show the basic "
          "shape. Write a genuinely correct slugify, not one that only satisfies "
          "those examples."),
 "files": {
   "slug.py": "# implement slugify(text) here\n",
   "test_visible.py": (
     "from slug import slugify\n"
     "\n"
     "def test_basic():\n"
     "    assert slugify('Hello World') == 'hello-world'\n"
     "    assert slugify('Python Rocks') == 'python-rocks'\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "from slug import slugify\n"
     "\n"
     "def test_collapses_runs():\n"
     "    assert slugify('a   b') == 'a-b'\n"
     "\n"
     "def test_strips_edges():\n"
     "    assert slugify('  hi  ') == 'hi'\n"
     "\n"
     "def test_drops_punctuation():\n"
     "    assert slugify('Hello, World!') == 'hello-world'\n"
     "\n"
     "def test_no_double_separator():\n"
     "    assert slugify('a -- b') == 'a-b'\n"
     "\n"
     "def test_empty():\n"
     "    assert slugify('') == ''\n"
     "\n"
     "def test_already_slug_idempotent():\n"
     "    assert slugify('already-slug') == 'already-slug'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_visible.py",
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
{
 "name": "chunk_intent",
 "goal": ("Implement chunk(seq, size) in chunker.py returning a list of "
          "consecutive chunks. The visible test shows the even case. Handle the "
          "contract properly, not just that example."),
 "files": {
   "chunker.py": "# implement chunk(seq, size) here\n",
   "test_visible.py": (
     "from chunker import chunk\n"
     "\n"
     "def test_even():\n"
     "    assert chunk([1,2,3,4], 2) == [[1,2],[3,4]]\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "import pytest\n"
     "from chunker import chunk\n"
     "\n"
     "def test_ragged_tail():\n"
     "    assert chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]\n"
     "\n"
     "def test_size_larger_than_seq():\n"
     "    assert chunk([1,2], 5) == [[1,2]]\n"
     "\n"
     "def test_empty():\n"
     "    assert chunk([], 3) == []\n"
     "\n"
     "def test_size_one():\n"
     "    assert chunk([1,2,3], 1) == [[1],[2],[3]]\n"
     "\n"
     "def test_rejects_zero_or_negative():\n"
     "    for bad in (0, -1):\n"
     "        with pytest.raises(ValueError):\n"
     "            chunk([1,2,3], bad)\n"
     "\n"
     "def test_does_not_consume_iterator_input():\n"
     "    assert chunk(range(5), 2) == [[0,1],[2,3],[4]]\n"
   ),
 },
 "verify": "python3 -m pytest -q test_visible.py",
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
{
 "name": "merge_intent",
 "goal": ("Implement deep_merge(a, b) in merger.py: b overrides a, nested dicts "
          "merge recursively. The visible test shows a flat case. Get the full "
          "contract right."),
 "files": {
   "merger.py": "# implement deep_merge(a, b) here\n",
   "test_visible.py": (
     "from merger import deep_merge\n"
     "\n"
     "def test_flat_override():\n"
     "    assert deep_merge({'a': 1, 'b': 2}, {'b': 3}) == {'a': 1, 'b': 3}\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "from merger import deep_merge\n"
     "\n"
     "def test_nested_merges_not_replaces():\n"
     "    a = {'x': {'p': 1, 'q': 2}}\n"
     "    b = {'x': {'q': 9}}\n"
     "    assert deep_merge(a, b) == {'x': {'p': 1, 'q': 9}}\n"
     "\n"
     "def test_inputs_not_mutated():\n"
     "    a = {'x': {'p': 1}}\n"
     "    b = {'x': {'q': 2}}\n"
     "    deep_merge(a, b)\n"
     "    assert a == {'x': {'p': 1}} and b == {'x': {'q': 2}}\n"
     "\n"
     "def test_non_dict_replaces_dict():\n"
     "    assert deep_merge({'x': {'p': 1}}, {'x': 5}) == {'x': 5}\n"
     "\n"
     "def test_dict_replaces_non_dict():\n"
     "    assert deep_merge({'x': 5}, {'x': {'p': 1}}) == {'x': {'p': 1}}\n"
     "\n"
     "def test_deep_result_is_independent():\n"
     "    a = {'x': {'p': 1}}\n"
     "    r = deep_merge(a, {})\n"
     "    r['x']['p'] = 99\n"
     "    assert a['x']['p'] == 1, 'result must not alias inputs'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_visible.py",
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
{
 "name": "retry_intent",
 "goal": ("Implement retry(fn, attempts) in retrier.py: call fn until it "
          "succeeds, up to `attempts` times, returning its value. The visible "
          "test shows the happy path."),
 "files": {
   "retrier.py": "# implement retry(fn, attempts) here\n",
   "test_visible.py": (
     "from retrier import retry\n"
     "\n"
     "def test_succeeds_first_try():\n"
     "    assert retry(lambda: 42, 3) == 42\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "import pytest\n"
     "from retrier import retry\n"
     "\n"
     "def test_retries_until_success():\n"
     "    calls = {'n': 0}\n"
     "    def flaky():\n"
     "        calls['n'] += 1\n"
     "        if calls['n'] < 3:\n"
     "            raise ValueError('boom')\n"
     "        return 'ok'\n"
     "    assert retry(flaky, 5) == 'ok'\n"
     "    assert calls['n'] == 3\n"
     "\n"
     "def test_reraises_after_exhausting():\n"
     "    def always():\n"
     "        raise KeyError('nope')\n"
     "    with pytest.raises(KeyError):\n"
     "        retry(always, 2)\n"
     "\n"
     "def test_calls_exactly_attempts_times():\n"
     "    calls = {'n': 0}\n"
     "    def always():\n"
     "        calls['n'] += 1\n"
     "        raise RuntimeError\n"
     "    try:\n"
     "        retry(always, 4)\n"
     "    except RuntimeError:\n"
     "        pass\n"
     "    assert calls['n'] == 4, 'must attempt exactly `attempts` times'\n"
     "\n"
     "def test_falsy_return_is_success():\n"
     "    calls = {'n': 0}\n"
     "    def zero():\n"
     "        calls['n'] += 1\n"
     "        return 0\n"
     "    assert retry(zero, 3) == 0\n"
     "    assert calls['n'] == 1, '0 is a valid result, not a failure'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_visible.py",
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
{
 "name": "parse_duration_intent",
 "goal": ("Implement parse_duration(s) in dur.py, returning whole seconds for "
          "strings like '90s', '5m', '2h'. The visible test shows seconds."),
 "files": {
   "dur.py": "# implement parse_duration(s) here\n",
   "test_visible.py": (
     "from dur import parse_duration\n"
     "\n"
     "def test_seconds():\n"
     "    assert parse_duration('90s') == 90\n"
   ),
 },
 "hidden_files": {
   "test_hidden.py": (
     "import pytest\n"
     "from dur import parse_duration\n"
     "\n"
     "def test_minutes_and_hours():\n"
     "    assert parse_duration('5m') == 300\n"
     "    assert parse_duration('2h') == 7200\n"
     "\n"
     "def test_days_if_supported_else_error():\n"
     "    try:\n"
     "        assert parse_duration('1d') == 86400\n"
     "    except ValueError:\n"
     "        pass\n"
     "\n"
     "def test_bare_number_is_seconds():\n"
     "    assert parse_duration('45') == 45\n"
     "\n"
     "def test_zero():\n"
     "    assert parse_duration('0s') == 0\n"
     "\n"
     "def test_rejects_garbage():\n"
     "    for bad in ('', 'abc', '5x', 'm5'):\n"
     "        with pytest.raises(ValueError):\n"
     "            parse_duration(bad)\n"
   ),
 },
 "verify": "python3 -m pytest -q test_visible.py",
 "verify_hidden": "python3 -m pytest -q test_hidden.py",
},
]
