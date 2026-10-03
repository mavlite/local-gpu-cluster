import ast
import pytest
from scripts.redteam.apply_auth_guard import apply_guard, MARKER

SAMPLE = (
    "from __future__ import annotations\n"
    "from fastapi import APIRouter, HTTPException, status\n"
    "from .. import db\n"
    "router = APIRouter(prefix='/auth')\n"
    "\n"
    "@router.post('/register')\n"
    "def register(request: RegisterRequest) -> UserResponse:\n"
    "    if db.get_user_by_username(request.username) is not None:\n"
    "        raise HTTPException(status_code=400, detail='exists')\n"
    "    return _user_response(db.create_user(request.username))\n"
)

def test_inserts_import_and_guard_and_stays_valid_python():
    out = apply_guard(SAMPLE)
    assert "    import os" in out
    assert MARKER in out
    assert 'REDTEAM_ALLOW_REGISTRATION' in out
    ast.parse(out)  # still valid Python
    # guard sits inside register(), before the existing first statement
    body = out.split("def register(", 1)[1]
    assert body.index(MARKER) < body.index("if db.get_user_by_username")

def test_idempotent_applied_twice_equals_once():
    once = apply_guard(SAMPLE)
    assert apply_guard(once) == once

def test_does_not_duplicate_existing_import_os():
    src = SAMPLE.replace("import os\n", "")  # ensure absent
    src = "import os\n" + src
    out = apply_guard(src)
    assert out.count("\nimport os\n") + (1 if out.startswith("import os\n") else 0) == 1

def test_raises_when_register_absent():
    with pytest.raises(ValueError):
        apply_guard("from fastapi import status\n# no register here\n")
