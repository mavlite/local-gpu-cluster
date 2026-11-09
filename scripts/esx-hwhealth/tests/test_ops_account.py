"""Workstation tests for the pure parts of ops/ops_account.py.

The host calls (pyVmomi account creation, vAPI JSON-RPC) are proven against the real hosts; these
tests pin the request shapes from spike-findings S3, the least-privilege grant, the secret handling
and the verdicts the script prints.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ops_account", ROOT / "ops" / "ops_account.py")
oa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oa)

CP = "com.vmware.esx.authentication.client_profiles"
REPORTS = "com.vmware.esx.observability.reports"


# --- credentials -------------------------------------------------------------------------------

def test_load_key_returns_only_the_named_key(tmp_path):
    f = tmp_path / "credentials.env"
    f.write_text('LAB_ESX_USER=root\nLAB_HWH_OPS_PASS="s3cr3t!Pw"\nOTHER=x\n', encoding="utf-8")
    assert oa.load_key("LAB_HWH_OPS_PASS", f) == "s3cr3t!Pw"
    assert oa.load_key("LAB_ESX_USER", f) == "root"


def test_load_key_missing_names_the_key_but_no_value(tmp_path):
    f = tmp_path / "credentials.env"
    f.write_text("LAB_ESX_PASS=topsecret\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        oa.load_key("LAB_HWH_OPS_PASS", f)
    assert "LAB_HWH_OPS_PASS" in str(e.value)
    assert "topsecret" not in str(e.value)


def test_load_key_does_not_match_a_key_prefix(tmp_path):
    f = tmp_path / "credentials.env"
    f.write_text("LAB_HWH_OPS_PASS_OLD=old\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        oa.load_key("LAB_HWH_OPS_PASS", f)


# --- input validation --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["svc-hwh-ops", "ops_reader", "abc"])
def test_valid_user_names(name):
    assert oa.validate_user(name) == name


@pytest.mark.parametrize("name", ["root", "", "ab", "Svc", "svc hwh", "svc;rm", "-svc", "a" * 33,
                                  "dcui", "vpxuser"])
def test_invalid_or_reserved_user_names(name):
    with pytest.raises(ValueError):
        oa.validate_user(name)


@pytest.mark.parametrize("pw", ["Abcdefgh1!xy", "longer-Passw0rd"])
def test_password_meeting_the_floor(pw):
    """Floor: 12+ characters from 3+ classes (stricter than ESX's default 7 and 3)."""
    oa.check_password(pw)


@pytest.mark.parametrize("pw", ["Short1!", "Abcdefgh1!x", "alllowercaseletters", "ALLUPPER123456"])
def test_password_below_the_floor_does_not_echo_it(pw):
    with pytest.raises(ValueError) as e:
        oa.check_password(pw)
    assert pw not in str(e.value)


@pytest.mark.parametrize("host", ["172.16.10.145", "hyp01.lab.knowledgeondemand.net"])
def test_valid_hosts(host):
    assert oa.validate_host(host) == host


@pytest.mark.parametrize("host", ["", "https://hyp01", "hyp01/api", "a b", "172.16.10.145:443"])
def test_invalid_hosts(host):
    with pytest.raises(ValueError):
        oa.validate_host(host)


# --- vAPI JSON-RPC shapes (spike-findings S3) -----------------------------------------------------

def test_rpc_body_and_headers_match_the_recipe():
    body = oa.rpc_body(REPORTS, "get", {"report": "lab.hwHealth"}, oa.oauth_ctx("T"))
    assert body == {
        "jsonrpc": "2.0", "method": "invoke", "id": "1",
        "params": {"serviceId": REPORTS, "operationId": "get",
                   "input": {"STRUCTURE": {"operation-input": {"report": "lab.hwHealth"}}},
                   "ctx": {"appCtx": {}, "securityCtx": {
                       "schemeId": "com.vmware.vapi.std.security.oauth", "accessToken": "T"}}}}
    assert oa.rpc_headers(REPORTS, "get") == {
        "vapi-service": REPORTS, "vapi-operation": "get", "Content-Type": "application/json"}


def test_user_pass_ctx():
    assert oa.user_pass_ctx("u", "p") == {
        "schemeId": "com.vmware.vapi.std.security.user_pass", "userName": "u", "password": "p"}


@pytest.mark.parametrize("tok", ["a.b.c", {"SECRET": "a.b.c"}])
def test_extract_token_plain_and_secret_forms(tok):
    resp = {"result": {"output": {"STRUCTURE": {
        "com.vmware.esx.authentication.token.token_info": {"access_token": tok}}}}}
    assert oa.extract_token(resp) == "a.b.c"


def test_extract_token_on_error_raises_with_the_error_kind():
    resp = {"result": {"error": {"ERROR": {"com.vmware.vapi.std.errors.unauthenticated": {}}}}}
    with pytest.raises(RuntimeError) as e:
        oa.extract_token(resp)
    assert "unauthenticated" in str(e.value)


def test_profile_id():
    assert oa.profile_id("svc-hwh-ops") == "LU###svc-hwh-ops"


def test_create_spec_grants_only_observability():
    spec = oa.create_profile_spec("svc-hwh-ops")["STRUCTURE"][f"{CP}.create_spec"]
    assert spec["local_user_name"] == {"OPTIONAL": "svc-hwh-ops"}
    assert oa.entitlements(spec["grants"]) == ["OBSERVABILITY"]
    assert spec["privileges"] == {"OPTIONAL": None}


def test_entitlements_reads_profile_info():
    info = {"grants": [
        {"STRUCTURE": {f"{CP}.access_grant": {"resource_type": "ENTITLEMENT",
                                             "entitlement": {"OPTIONAL": "IDENTITY_MGMT"}}}},
        {"STRUCTURE": {f"{CP}.access_grant": {"resource_type": "ENTITLEMENT",
                                             "entitlement": {"OPTIONAL": "OBSERVABILITY"}}}}]}
    assert oa.entitlements(info["grants"]) == ["IDENTITY_MGMT", "OBSERVABILITY"]
    assert oa.entitlements(None) == []


@pytest.mark.parametrize("resp,kind", [
    ({"result": {"output": None}}, None),
    ({"result": {"error": {"ERROR": {"com.vmware.vapi.std.errors.unauthorized": {}}}}}, "unauthorized"),
    ({"result": {"error": {"ERROR": {"com.vmware.vapi.std.errors.not_found": {}}}}}, "not_found"),
    ({"result": {"error": {"ERROR": {"com.vmware.vapi.std.errors.already_exists": {}}}}}, "already_exists"),
    ({"error": {"code": -31001, "message": "x"}}, "rpc_error"),
])
def test_error_kind(resp, kind):
    assert oa.error_kind(resp) == kind


# --- verdicts ----------------------------------------------------------------------------------

def _report(status, data):
    return {"result": {"output": {"STRUCTURE": {f"{REPORTS}.info": {
        "health_data": json.dumps(data), "status": status,
        "last_update_time": "2026-11-09T16:43:13.244Z"}}}}}


GOOD = {"schema": 1, "errors": [], "sampled_at": "2026-11-09T16:42:11+00:00",
        "cpu": {"supported": True, "tctl_c": 78.1, "ccd_c": [72.4, 63.3], "package_w": 72.2},
        "nvme": [{"adapter": "vmhba0", "temp_c": 43}]}


def test_report_verdict_ok():
    ok, line = oa.report_verdict(_report("OK", GOOD))
    assert ok and "tctl 78.1" in line and "status OK" in line


def test_report_verdict_flags_missing_cpu_and_errors():
    bad = dict(GOOD, errors=[{"reading": "tctl_c", "reason": "Operation not permitted"}],
               cpu={"supported": True, "tctl_c": None, "ccd_c": [], "package_w": None})
    ok, line = oa.report_verdict(_report("WARNING", bad))
    assert not ok and "errors 1" in line and "cpu missing" in line


def test_report_verdict_on_unauthorized():
    ok, line = oa.report_verdict(
        {"result": {"error": {"ERROR": {"com.vmware.vapi.std.errors.unauthorized": {}}}}})
    assert not ok and "unauthorized" in line


def test_scrub_hides_every_secret():
    s = oa.Scrubber(["pw1", "tok.en", None, ""])
    assert s('{"a":"pw1","b":"tok.en"}') == '{"a":"[x]","b":"[x]"}'


# --- review fixes: credentials, scrubbing, TLS ---------------------------------------------------

def test_load_key_strips_one_pair_of_quotes_only(tmp_path):
    f = tmp_path / "credentials.env"
    f.write_text("A=\"ends-with-quote\"\"\nB='x'\nC=no\"quote\n", encoding="utf-8")
    assert oa.load_key("A", f) == 'ends-with-quote"'
    assert oa.load_key("B", f) == "x"
    assert oa.load_key("C", f) == 'no"quote'


def test_scrub_hides_json_escaped_secret():
    pw = 'Ab"cd\\efgh1234'
    s = oa.Scrubber([pw])
    assert "efgh" not in s(json.dumps({"password": pw}))


def test_http_error_body_is_scrubbed_before_truncation(monkeypatch):
    import io
    import urllib.error
    pw = "Secr3t-Passw0rd!x"
    body = ("x" * 290 + pw + "y" * 50).encode()

    def boom(req, context, timeout):
        raise urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(body))

    monkeypatch.setattr(oa.urllib.request, "urlopen", boom)
    api = oa.Api("h", None, oa.Scrubber([pw]))
    with pytest.raises(RuntimeError) as e:
        api.call(REPORTS, "get", {}, {})
    assert "Secr3t" not in str(e.value) and "HTTP 500" in str(e.value)


def test_tls_needs_an_explicit_choice():
    with pytest.raises(ValueError):
        oa.ssl_context(None, False)
    assert oa.ssl_context(None, True).verify_mode == oa.ssl.CERT_NONE


# --- review fixes: grant decisions -----------------------------------------------------------------

def test_entitlements_names_a_non_entitlement_grant_by_type():
    g = [{"STRUCTURE": {f"{CP}.access_grant": {"resource_type": "PRIVILEGE", "entitlement": None}}}]
    assert oa.entitlements(g) == ["<PRIVILEGE>"]


@pytest.mark.parametrize("grants,action", [(None, "create"), ([], "add"), (["OBSERVABILITY"], "ok")])
def test_profile_action(grants, action):
    assert oa.profile_action(grants) == action


@pytest.mark.parametrize("grants", [["IDENTITY_MGMT"], ["IDENTITY_MGMT", "OBSERVABILITY"],
                                    ["<PRIVILEGE>"], ["OBSERVABILITY", "OBSERVABILITY"]])
def test_profile_action_refuses_anything_but_exactly_observability(grants):
    with pytest.raises(ValueError):
        oa.profile_action(grants)


def test_update_input_sets_exactly_observability():
    inp = oa.update_profile_input("svc-hwh-ops")
    assert inp["profile"] == "LU###svc-hwh-ops"
    grants = inp["spec"]["STRUCTURE"][f"{CP}.update_spec"]["grants"]["OPTIONAL"]
    assert oa.entitlements(grants) == ["OBSERVABILITY"]


@pytest.mark.parametrize("grants,roles,ok", [
    (["OBSERVABILITY"], [], True), (["OBSERVABILITY"], ["ReadOnly"], True),
    (["OBSERVABILITY", "IDENTITY_MGMT"], [], False), (None, [], False),
    (["OBSERVABILITY"], ["Admin"], False), (["OBSERVABILITY"], ["Admin", "ReadOnly"], False)])
def test_least_privilege(grants, roles, ok):
    assert oa.least_privilege(grants, roles)[0] is ok


def test_token_hint_only_suggests_a_role_for_unauthorized():
    assert "--readonly-role" in oa.token_hint("unauthorized")
    assert "--readonly-role" not in oa.token_hint("unauthenticated")


# --- apply and verify against a fake host ----------------------------------------------------------

PW = "Abcdefgh1!xy"
ADMIN = oa.oauth_ctx("admin-token")
USER = "svc-hwh-ops"


def _err(kind):
    return {"result": {"error": {"ERROR": {f"com.vmware.vapi.std.errors.{kind}": {}}}}}


def _ok(struct_name=None, value=None):
    if struct_name is None:
        return {"result": {"output": None}}
    return {"result": {"output": {"STRUCTURE": {struct_name: value}}}}


class FakeHost:
    def __init__(self, user_exists=False, roles=(), lockdown="lockdownDisabled",
                 implicit_profile=None):
        self.user_exists, self.roles, self.lockdown = user_exists, list(roles), lockdown
        self.implicit_profile, self.calls, self.api = implicit_profile, [], None

    def state(self, user):
        return {"lockdown": self.lockdown, "user_exists": self.user_exists,
                "roles": sorted(self.roles)}

    def create_user(self, user, pw):
        self.calls.append("create_user")
        self.user_exists = True
        if self.implicit_profile is not None:
            self.api.grants = list(self.implicit_profile)

    def grant_readonly(self, user):
        self.calls.append("grant_readonly")
        self.roles = ["ReadOnly"]


class FakeApi(oa.Api):
    """A host's vAPI as far as the script uses it; `grants` is LU###<user>'s state."""

    def __init__(self, grants=None, user_token_kind=None, list_kind="unauthorized", report=None,
                 lossy_create=False):
        super().__init__("h", None, oa.Scrubber())
        self.grants, self.user_token_kind, self.list_kind = grants, user_token_kind, list_kind
        self.report, self.lossy_create, self.calls = report, lossy_create, []

    def call(self, service, op, inp, sec):
        self.calls.append(f"{service.rsplit('.', 1)[-1]}.{op}")
        if service == oa.TOKEN:
            if self.user_token_kind:
                return _err(self.user_token_kind)
            return _ok(f"{oa.TOKEN}.token_info", {"access_token": "user-token"})
        if service == REPORTS:
            return self.report or _ok(f"{REPORTS}.info", {
                "health_data": json.dumps(GOOD), "status": "OK"})
        if op == "get":
            if self.grants is None:
                return _err("not_found")
            return _ok(f"{CP}.info", {"grants": [oa.grant(e) for e in self.grants]})
        if op == "create":
            if self.grants is not None:
                return _err("already_exists")
            self.grants = [] if self.lossy_create else ["OBSERVABILITY"]
            return _ok()
        if op == "update":
            self.grants = oa.entitlements(
                inp["spec"]["STRUCTURE"][f"{CP}.update_spec"]["grants"]["OPTIONAL"])
            return _ok()
        if op == "list":
            return _err(self.list_kind) if self.list_kind else _ok()
        raise AssertionError(op)


HOST_KW = ("user_exists", "roles", "lockdown", "implicit_profile")


def _pair(**kw):
    host = FakeHost(**{k: v for k, v in kw.items() if k in HOST_KW})
    api = FakeApi(**{k: v for k, v in kw.items() if k not in HOST_KW})
    host.api = api
    return host, api


def _changed(host, api):
    return host.calls or any(c.endswith((".create", ".update")) for c in api.calls)


def test_apply_fresh_host_creates_user_and_profile(capsys):
    host, api = _pair()
    assert oa.do_apply(host, api, ADMIN, USER, PW, False) == 0
    assert host.calls == ["create_user"] and "client_profiles.create" in api.calls
    assert api.grants == ["OBSERVABILITY"]
    assert "PASS postcondition" in capsys.readouterr().out


def test_apply_when_an_empty_profile_appears_with_the_user():
    host, api = _pair(implicit_profile=[])
    assert oa.do_apply(host, api, ADMIN, USER, PW, False) == 0
    assert api.grants == ["OBSERVABILITY"] and "client_profiles.update" in api.calls


def test_apply_stops_when_a_profile_with_other_grants_appears_with_the_user():
    host, api = _pair(implicit_profile=["IDENTITY_MGMT"])
    with pytest.raises(ValueError):
        oa.do_apply(host, api, ADMIN, USER, PW, False)
    assert api.grants == ["IDENTITY_MGMT"]


def test_apply_is_idempotent():
    host, api = _pair(user_exists=True, grants=["OBSERVABILITY"])
    assert oa.do_apply(host, api, ADMIN, USER, PW, False) == 0
    assert not _changed(host, api)


@pytest.mark.parametrize("kw", [dict(lockdown="lockdownNormal"),
                                dict(user_exists=True, roles=["Admin"]),
                                dict(user_exists=True, grants=["IDENTITY_MGMT"]),
                                dict(user_exists=True, grants=["OBSERVABILITY", "IDENTITY_MGMT"])])
def test_apply_refuses_before_any_change(kw):
    host, api = _pair(**kw)
    with pytest.raises((RuntimeError, ValueError)):
        oa.do_apply(host, api, ADMIN, USER, PW, True)
    assert not _changed(host, api)


def test_apply_refuses_a_weak_password_before_any_change():
    host, api = _pair()
    with pytest.raises(ValueError):
        oa.do_apply(host, api, ADMIN, USER, "weak", False)
    assert not _changed(host, api)


def test_apply_readonly_role():
    host, api = _pair()
    assert oa.do_apply(host, api, ADMIN, USER, PW, True) == 0
    assert host.calls == ["create_user", "grant_readonly"] and host.roles == ["ReadOnly"]


def test_apply_fails_the_postcondition_when_the_grant_does_not_stick(capsys):
    host, api = _pair(lossy_create=True)
    assert oa.do_apply(host, api, ADMIN, USER, PW, False) == 1
    assert "FAIL postcondition" in capsys.readouterr().out


def test_verify_pass(capsys):
    host, api = _pair(user_exists=True, grants=["OBSERVABILITY"])
    assert oa.do_verify(host, api, ADMIN, USER, PW) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 3 and "FAIL" not in out


@pytest.mark.parametrize("kw,needle", [
    (dict(grants=["OBSERVABILITY", "IDENTITY_MGMT"]), "FAIL least privilege"),
    (dict(grants=["OBSERVABILITY"], roles=["Admin"]), "FAIL least privilege"),
    (dict(grants=["OBSERVABILITY"], list_kind=None), "ALLOWED"),
    (dict(grants=["OBSERVABILITY"], list_kind="service_unavailable"), "FAIL client_profiles.list"),
    (dict(grants=["OBSERVABILITY"], report=_err("unauthorized")), "FAIL report"),
])
def test_verify_fails(kw, needle, capsys):
    host, api = _pair(user_exists=True, **kw)
    assert oa.do_verify(host, api, ADMIN, USER, PW) == 1
    assert needle in capsys.readouterr().out


@pytest.mark.parametrize("kind,hint", [("unauthorized", "--readonly-role"),
                                       ("unauthenticated", "wrong password")])
def test_verify_token_refused_gives_the_right_hint(kind, hint, capsys):
    host, api = _pair(user_exists=True, grants=["OBSERVABILITY"], user_token_kind=kind)
    assert oa.do_verify(host, api, ADMIN, USER, PW) == 1
    assert hint in capsys.readouterr().out
