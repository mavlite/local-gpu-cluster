# Long-horizon tasks: many files, consistent change required everywhere.
#
# The hard set finished in 5-6 turns, so error accumulation never had a chance
# to matter -- and that is exactly where 2-bit quantization is documented to
# hurt (Quesma measured ~25% more output tokens at Q2 on identical solved
# tasks). These need 15-30 turns of navigate/read/edit/verify, so drift and
# forgetting have room to show up.
#
# Run with MAXTURNS=40 so the cap is not the binding constraint; a task that
# fails at the cap measures the cap, not the model.

LONG_TASKS = [
{
 # Ten call sites across eight files, plus a test that greps the sources for
 # any surviving reference. Missing one file fails everything.
 "name": "wide_rename",
 "goal": ("Rename `get_conn` to `open_connection` everywhere in this repo -- "
          "definition, every import, and every call site. Nothing may still "
          "reference the old name. All tests must pass."),
 "files": {
   "pool.py": "def get_conn(dsn):\n    return {'dsn': dsn}\n",
   "users.py": ("from pool import get_conn\n\n"
                "def load(dsn):\n    return get_conn(dsn)['dsn']\n"),
   "orders.py": ("from pool import get_conn\n\n"
                 "def count(dsn):\n    c = get_conn(dsn)\n    return len(c['dsn'])\n"),
   "billing.py": ("import pool\n\n"
                  "def charge(dsn):\n    return pool.get_conn(dsn)['dsn'].upper()\n"),
   "reports.py": ("from pool import get_conn as gc\n\n"
                  "def head(dsn):\n    return gc(dsn)['dsn'][:2]\n"),
   "admin.py": ("from pool import get_conn\n\n"
                "def probe(dsn):\n    return bool(get_conn(dsn))\n"),
   "jobs.py": ("import pool\n\n"
               "def run(dsn):\n    conn = pool.get_conn(dsn)\n    return conn['dsn'] + '!'\n"),
   "audit.py": ("from pool import get_conn\n\n"
                "def trail(dsn):\n    return [get_conn(dsn)['dsn']]\n"),
   "test_all.py": (
     "import inspect, pool, users, orders, billing, reports, admin, jobs, audit\n"
     "\n"
     "def test_behaviour():\n"
     "    assert users.load('x') == 'x'\n"
     "    assert orders.count('abc') == 3\n"
     "    assert billing.charge('ab') == 'AB'\n"
     "    assert reports.head('abcd') == 'ab'\n"
     "    assert admin.probe('z') is True\n"
     "    assert jobs.run('q') == 'q!'\n"
     "    assert audit.trail('w') == ['w']\n"
     "\n"
     "def test_renamed_everywhere():\n"
     "    assert hasattr(pool, 'open_connection')\n"
     "    assert not hasattr(pool, 'get_conn')\n"
     "    mods = [pool, users, orders, billing, reports, admin, jobs, audit]\n"
     "    src = ''.join(inspect.getsource(m) for m in mods)\n"
     "    assert 'get_conn' not in src, 'a reference to the old name survives'\n"
   ),
 },
 "verify": "python3 -m pytest -q test_all.py",
},
{
 # A behaviour change that must be applied consistently to six validators, each
 # written in a slightly different style so a single find/replace will not work.
 "name": "consistent_validation",
 "goal": ("Every validate_* function must reject None by raising ValueError "
          "with a message containing the field name, instead of its current "
          "inconsistent behaviour. Keep all existing valid-input behaviour. "
          "All tests must pass."),
 "files": {
   "v_name.py": ("def validate_name(v):\n"
                 "    if not v:\n        return None\n"
                 "    return v.strip()\n"),
   "v_age.py": ("def validate_age(v):\n"
                "    if v is None:\n        return 0\n"
                "    return int(v)\n"),
   "v_email.py": ("def validate_email(v):\n"
                  "    try:\n        return v.lower()\n"
                  "    except AttributeError:\n        return ''\n"),
   "v_tag.py": ("def validate_tag(v):\n"
                "    return (v or 'untagged').replace(' ', '-')\n"),
   "v_port.py": ("def validate_port(v):\n"
                 "    if v is None:\n        v = 80\n"
                 "    return int(v)\n"),
   "v_path.py": ("def validate_path(v):\n"
                 "    return str(v) if v is not None else '/'\n"),
   "test_validators.py": (
     "import pytest\n"
     "from v_name import validate_name\n"
     "from v_age import validate_age\n"
     "from v_email import validate_email\n"
     "from v_tag import validate_tag\n"
     "from v_port import validate_port\n"
     "from v_path import validate_path\n"
     "\n"
     "CASES = [\n"
     "    (validate_name, 'name', '  bob ', 'bob'),\n"
     "    (validate_age, 'age', '31', 31),\n"
     "    (validate_email, 'email', 'A@B.C', 'a@b.c'),\n"
     "    (validate_tag, 'tag', 'a b', 'a-b'),\n"
     "    (validate_port, 'port', '8080', 8080),\n"
     "    (validate_path, 'path', '/tmp', '/tmp'),\n"
     "]\n"
     "\n"
     "def test_valid_inputs_unchanged():\n"
     "    for fn, _f, given, want in CASES:\n"
     "        assert fn(given) == want\n"
     "\n"
     "def test_none_rejected_with_field_name():\n"
     "    for fn, field, _g, _w in CASES:\n"
     "        with pytest.raises(ValueError) as ei:\n"
     "            fn(None)\n"
     "        assert field in str(ei.value).lower(), (\n"
     "            '%s: message must name the field' % field)\n"
   ),
 },
 "verify": "python3 -m pytest -q test_validators.py",
},
]
