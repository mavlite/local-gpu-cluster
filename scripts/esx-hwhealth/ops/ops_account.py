#!/usr/bin/env python3
"""Least-privilege ESX account for the VCF Operations hwhealth pack (plan Task 8).

Creates, on one host, a local user with no shell whose only vAPI entitlement is OBSERVABILITY, so
Operations can call `com.vmware.esx.observability.reports.get` for `lab.hwHealth` without root.

Usage (from the workstation, with a Python that has pyVmomi; one host per run):
    ops_account.py <host> plan     read-only: lockdown, user, host roles, ClientProfile grants
    ops_account.py <host> apply    create the user and its ClientProfile (LIVE CHANGE)
    ops_account.py <host> verify   read-only: grants and roles are exactly least-privilege, and the
                                   user itself can read the report but not administer profiles

TLS: pass --ca-file <knowledgeondemand-LabRoot-CA pem>, or --insecure to skip verification
explicitly (the admin password is sent to the host either way).

Credentials come from ~/.vcflab/credentials.env, one named key at a time, never printed:
    LAB_ESX_USER / LAB_ESX_PASS   the host admin that makes the change and checks it
    LAB_HWH_OPS_PASS              the new account's password (the owner adds this key first)

apply refuses, before changing anything, when: the host is in lockdown; the connection is not
host-direct; the user already holds a host role other than ReadOnly; or its ClientProfile holds
any grant other than OBSERVABILITY. An existing user keeps its password. After the change it
re-reads the state and exits non-zero unless the result is exactly least-privilege.

Whether a user with no host role can get a vAPI token is not yet proven. If verify reports that
the token was refused for lack of permission, re-run apply with --readonly-role, which adds the
built-in ReadOnly role on the host root (no write privilege anywhere).
"""
import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

CRED = Path.home() / ".vcflab" / "credentials.env"
CP = "com.vmware.esx.authentication.client_profiles"
TOKEN = "com.vmware.esx.authentication.token"
REPORTS = "com.vmware.esx.observability.reports"
REPORT = "lab.hwHealth"
OBS = "OBSERVABILITY"
DEFAULT_USER = "svc-hwh-ops"
DESCRIPTION = "VCF Operations hwhealth reader (OBSERVABILITY only)"
RESERVED = {"root", "dcui", "vpxuser", "daemon", "nfsnobody"}
PW_MIN_LEN, PW_MIN_CLASSES = 12, 3
ALLOWED_ROLES = ([], ["ReadOnly"])


# --- credentials and input validation -------------------------------------------------------------

def load_key(key, path=CRED):
    """Return the value of exactly one key from the credentials file. Never reads by line range.
    One pair of surrounding quotes is removed; nothing else is interpreted."""
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = pat.match(line)
            if m:
                v = m.group(1)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
    raise SystemExit(f"{key} not found in {path}")


def validate_user(name):
    if name in RESERVED or not re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", name or ""):
        raise ValueError(f"invalid or reserved user name: {name!r}")
    return name


def check_password(pw):
    classes = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if len(pw) < PW_MIN_LEN or classes < PW_MIN_CLASSES:
        raise ValueError(f"LAB_HWH_OPS_PASS must be at least {PW_MIN_LEN} characters from at least "
                         f"{PW_MIN_CLASSES} of: lower, upper, digit, symbol")


def validate_host(host):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?", host or ""):
        raise ValueError(f"host must be a bare name or IPv4 address: {host!r}")
    return host


class Scrubber:
    """Replaces every known secret, raw or JSON-escaped, in text before it is printed."""

    def __init__(self, secrets=()):
        self.secrets = []
        for s in secrets:
            self.add(s)

    def add(self, secret):
        if secret:
            for form in {secret, json.dumps(secret)[1:-1]}:
                if form not in self.secrets:
                    self.secrets.append(form)
            self.secrets.sort(key=len, reverse=True)

    def __call__(self, text):
        for s in self.secrets:
            text = text.replace(s, "[x]")
        return text


# --- vAPI JSON-RPC (spike-findings S3) ------------------------------------------------------------

def user_pass_ctx(user, pw):
    return {"schemeId": "com.vmware.vapi.std.security.user_pass", "userName": user, "password": pw}


def oauth_ctx(token):
    return {"schemeId": "com.vmware.vapi.std.security.oauth", "accessToken": token}


def rpc_body(service, op, inp, security_ctx):
    return {"jsonrpc": "2.0", "method": "invoke", "id": "1",
            "params": {"serviceId": service, "operationId": op,
                       "input": {"STRUCTURE": {"operation-input": inp}},
                       "ctx": {"appCtx": {}, "securityCtx": security_ctx}}}


def rpc_headers(service, op):
    return {"vapi-service": service, "vapi-operation": op, "Content-Type": "application/json"}


def error_kind(resp):
    """None on success, else the short vAPI error name (unauthorized, not_found, ...)."""
    if "error" in resp:
        return "rpc_error"
    err = (resp.get("result") or {}).get("error")
    if not err:
        return None
    name = next(iter(err.get("ERROR") or {"unknown": None}))
    return name.rsplit(".", 1)[-1]


def extract_token(resp):
    kind = error_kind(resp)
    if kind:
        raise TokenError(kind)
    info = next(iter(resp["result"]["output"]["STRUCTURE"].values()))
    tok = info["access_token"]
    return tok.get("SECRET", tok) if isinstance(tok, dict) else tok


class TokenError(RuntimeError):
    def __init__(self, kind):
        super().__init__(f"token create refused: {kind}")
        self.kind = kind


def token_hint(kind):
    if kind == "unauthenticated":
        return "wrong password, or the user does not exist (an existing user keeps its old password)"
    if kind == "unauthorized":
        return "the user may need a host role: re-run apply with --readonly-role"
    return "unexpected; check the host"


def profile_id(user):
    return f"LU###{user}"


def grant(entitlement):
    return {"STRUCTURE": {f"{CP}.access_grant": {"resource_type": "ENTITLEMENT",
                                                "entitlement": {"OPTIONAL": entitlement}}}}


def create_profile_spec(user):
    return {"STRUCTURE": {f"{CP}.create_spec": {
        "local_user_name": {"OPTIONAL": user}, "external_group_name": {"OPTIONAL": None},
        "external_user_name": {"OPTIONAL": None}, "privileges": {"OPTIONAL": None},
        "issuer_alias": {"OPTIONAL": None}, "domain": {"OPTIONAL": None},
        "grants": [grant(OBS)]}}}


def update_profile_input(user):
    """Set the grants to exactly [OBSERVABILITY]. Only used when the profile has no grants."""
    return {"profile": profile_id(user),
            "spec": {"STRUCTURE": {f"{CP}.update_spec": {"grants": {"OPTIONAL": [grant(OBS)]}}}}}


def entitlements(grants):
    """Entitlement names; a grant of another resource type is reported by its type."""
    out = []
    for g in grants or []:
        ag = g["STRUCTURE"][f"{CP}.access_grant"]
        out.append((ag.get("entitlement") or {}).get("OPTIONAL") or f"<{ag.get('resource_type')}>")
    return out


def profile_action(grants):
    """What apply does with the profile: create, add (empty grants), ok, or refuse."""
    if grants is None:
        return "create"
    if grants == []:
        return "add"
    if grants == [OBS]:
        return "ok"
    raise ValueError(f"ClientProfile already holds {grants}; a least-privilege account must hold "
                     f"exactly [{OBS}]. Fix it by hand, then re-run.")


def least_privilege(grants, roles):
    """(ok, reason) for the final state of the account."""
    if grants != [OBS]:
        return False, f"profile grants {grants}, expected [{OBS}]"
    if roles not in ALLOWED_ROLES:
        return False, f"host roles {roles}, expected none or [ReadOnly]"
    return True, f"grants [{OBS}], host roles {roles or 'none'}"


def report_verdict(resp):
    """(ok, one-line summary) for a reports.get lab.hwHealth response."""
    kind = error_kind(resp)
    if kind:
        return False, f"{REPORT}: {kind}"
    info = resp["result"]["output"]["STRUCTURE"][f"{REPORTS}.info"]
    data = json.loads(info["health_data"])
    cpu, errors = data.get("cpu") or {}, data.get("errors") or []
    cpu_ok = cpu.get("tctl_c") is not None and cpu.get("package_w") is not None and cpu.get("ccd_c")
    parts = [f"status {info.get('status')}", f"sampled {data.get('sampled_at')}",
             f"tctl {cpu.get('tctl_c')}", f"W {cpu.get('package_w')}",
             f"nvme {len(data.get('nvme') or [])}", f"errors {len(errors)}"]
    if not cpu_ok:
        parts.append("cpu missing")
    return bool(cpu_ok) and not errors, "  ".join(parts)


# --- transport -----------------------------------------------------------------------------------

class Api:
    def __init__(self, host, ssl_ctx, scrub):
        self.url, self.ctx, self.scrub = f"https://{host}/api", ssl_ctx, scrub

    def call(self, service, op, inp, security_ctx):
        data = json.dumps(rpc_body(service, op, inp, security_ctx)).encode()
        req = urllib.request.Request(self.url, data=data, method="POST",
                                     headers=rpc_headers(service, op))
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = self.scrub(e.read().decode("utf-8", "replace"))[:300]
            raise RuntimeError(f"{service}.{op}: HTTP {e.code} {body}") from None

    def token(self, user, pw):
        self.scrub.add(pw)
        tok = extract_token(self.call(TOKEN, "create", {}, user_pass_ctx(user, pw)))
        self.scrub.add(tok)
        return oauth_ctx(tok)

    def profile_grants(self, admin, user):
        """Grants of LU###<user>, or None when the profile does not exist."""
        resp = self.call(CP, "get", {"profile": profile_id(user)}, admin)
        kind = error_kind(resp)
        if kind == "not_found":
            return None
        if kind:
            raise RuntimeError(f"client_profiles.get: {kind}")
        info = next(iter(resp["result"]["output"]["STRUCTURE"].values()))
        return entitlements(info.get("grants"))

    def checked(self, op, inp, admin, ok_kinds=()):
        resp = self.call(CP, op, inp, admin)
        kind = error_kind(resp)
        if kind and kind not in ok_kinds:
            raise RuntimeError(f"client_profiles.{op}: {kind}")
        return kind


def ssl_context(ca_file, insecure):
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    if insecure:
        print("TLS: certificate NOT verified (--insecure)")
        return ssl._create_unverified_context()
    raise ValueError("pass --ca-file <knowledgeondemand-LabRoot-CA pem>, or --insecure")


# --- host side (pyVmomi, imported only when needed) ------------------------------------------------

class Host:
    """The few host-direct vim calls the script needs."""

    def __init__(self, si):
        self.si = si
        about = si.content.about
        if about.apiType != "HostAgent":
            raise RuntimeError(f"not a host-direct connection (apiType {about.apiType})")

    def _host(self):
        return self.si.content.rootFolder.childEntity[0].hostFolder.childEntity[0].host[0]

    def state(self, user):
        c = self.si.content
        found = c.userDirectory.RetrieveUserGroups(
            domain=None, searchStr=user, exactMatch=True, findUsers=True, findGroups=False)
        names = {r.roleId: r.name for r in c.authorizationManager.roleList}
        roles = []
        for entity in (c.rootFolder, self._host()):
            roles += [names.get(p.roleId, str(p.roleId))
                      for p in c.authorizationManager.RetrieveEntityPermissions(entity, False)
                      if p.principal == user and not p.group]
        return {"lockdown": str(self._host().config.lockdownMode), "user_exists": bool(found),
                "roles": sorted(set(roles))}

    def create_user(self, user, pw):
        from pyVmomi import vim
        self.si.content.accountManager.CreateUser(
            vim.host.LocalAccountManager.PosixAccountSpecification(
                id=user, password=pw, description=DESCRIPTION, shellAccess=False))

    def grant_readonly(self, user):
        from pyVmomi import vim
        am = self.si.content.authorizationManager
        ro = next(r.roleId for r in am.roleList if r.name == "ReadOnly")
        am.SetEntityPermissions(self.si.content.rootFolder, [vim.AuthorizationManager.Permission(
            principal=user, group=False, roleId=ro, propagate=True)])


# --- modes -----------------------------------------------------------------------------------------

def read_state(host, api, admin, user):
    st = host.state(user)
    st["profile_grants"] = api.profile_grants(admin, user)
    return st


def show(st):
    for k, v in st.items():
        print(f"  {k}: {v}")


def do_plan(host, api, admin, user):
    st = read_state(host, api, admin, user)
    show(st)
    return 0


def do_apply(host, api, admin, user, pw, readonly_role):
    st = read_state(host, api, admin, user)
    print("before:")
    show(st)
    # Every refusal happens here, before the first change.
    if st["lockdown"] != "lockdownDisabled":
        raise RuntimeError("host is in lockdown mode: refusing")
    if st["roles"] not in ALLOWED_ROLES:
        raise RuntimeError(f"{user} already holds host roles {st['roles']}: refusing")
    action = profile_action(st["profile_grants"])
    check_password(pw)

    if not st["user_exists"]:
        print(f"step: create local user {user} (no shell)")
        host.create_user(user, pw)
    else:
        print(f"step: user {user} exists, password left unchanged")
    if readonly_role and st["roles"] != ["ReadOnly"]:
        print(f"step: grant ReadOnly on the host root to {user}")
        host.grant_readonly(user)
    if action == "create":
        print(f"step: create ClientProfile {profile_id(user)} with [{OBS}]")
        if api.checked("create", {"spec": create_profile_spec(user)}, admin,
                       ok_kinds=("already_exists",)):
            # The profile may appear implicitly with the user (root's did): decide again.
            action = profile_action(api.profile_grants(admin, user))
            print(f"step: profile already existed, now: {action}")
    if action == "add":
        print(f"step: set ClientProfile {profile_id(user)} grants to [{OBS}]")
        api.checked("update", update_profile_input(user), admin)

    after = read_state(host, api, admin, user)
    print("after:")
    show(after)
    ok, reason = least_privilege(after["profile_grants"], after["roles"])
    ok = ok and after["user_exists"]
    print(f"{'PASS' if ok else 'FAIL'} postcondition: {reason}")
    return 0 if ok else 1


def do_verify(host, api, admin, user, pw):
    """Admin view: exactly least-privilege. User view: report readable, profile admin refused."""
    st = read_state(host, api, admin, user)
    ok_lp, reason = least_privilege(st["profile_grants"], st["roles"])
    print(f"{'PASS' if ok_lp else 'FAIL'} least privilege (admin view): {reason}")
    try:
        ctx = api.token(user, pw)
    except TokenError as e:
        print(f"FAIL token as {user}: {e.kind} ({token_hint(e.kind)})")
        return 1
    ok_rep, line = report_verdict(api.call(REPORTS, "get", {"report": REPORT}, ctx))
    print(f"{'PASS' if ok_rep else 'FAIL'} report as {user}: {line}")
    kind = error_kind(api.call(CP, "list", {}, ctx))
    denied = kind == "unauthorized"
    print(f"{'PASS' if denied else 'FAIL'} client_profiles.list as {user}: "
          f"{kind or 'ALLOWED (too much privilege)'}")
    return 0 if ok_lp and ok_rep and denied else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("host")
    ap.add_argument("mode", choices=["plan", "apply", "verify"])
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--readonly-role", action="store_true")
    tls = ap.add_mutually_exclusive_group()
    tls.add_argument("--ca-file")
    tls.add_argument("--insecure", action="store_true")
    a = ap.parse_args(argv)
    host_name, user = validate_host(a.host), validate_user(a.user)
    ctx = ssl_context(a.ca_file, a.insecure)
    scrub = Scrubber()
    api = Api(host_name, ctx, scrub)
    print(f"== {host_name} {a.mode} user={user}")
    admin_user, admin_pw = load_key("LAB_ESX_USER"), load_key("LAB_ESX_PASS")
    scrub.add(admin_pw)
    pw = None
    if a.mode in ("apply", "verify"):
        pw = load_key("LAB_HWH_OPS_PASS")
        scrub.add(pw)
    try:
        admin = api.token(admin_user, admin_pw)
        from pyVim.connect import Disconnect, SmartConnect
        si = SmartConnect(host=host_name, user=admin_user, pwd=admin_pw, sslContext=ctx)
        try:
            host = Host(si)
            if a.mode == "plan":
                return do_plan(host, api, admin, user)
            if a.mode == "apply":
                return do_apply(host, api, admin, user, pw, a.readonly_role)
            return do_verify(host, api, admin, user, pw)
        finally:
            Disconnect(si)
    except Exception as e:  # noqa: BLE001 - every failure is reported, scrubbed, with exit 2
        print(f"error: {type(e).__name__}: {scrub(str(e))[:500]}", file=sys.stderr)
        if a.mode == "apply":
            print("apply stopped: the 'step:' lines above are what completed; run plan to see "
                  "the state", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
