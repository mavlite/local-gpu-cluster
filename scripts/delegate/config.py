import os
from dataclasses import dataclass

class ConfigError(Exception):
    pass

@dataclass(frozen=True)
class Config:
    bearer_token: str
    router_token: str
    host: str = "127.0.0.1"
    port: int = 3006
    router_url: str = "http://192.168.6.153:8000/v1"
    allowed_repo_roots: tuple = ()
    jobs_dir: str = ""
    ledger_path: str = ""
    lease_path: str = ""
    opencode_exe: str = ""
    overlay_dir: str = ""
    ask_timeout_s: int = 600
    job_timeout_s: int = 1800
    ask_max_input_bytes: int = 400_000

def _default_local_appdata(sub: str) -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, "local-delegate", sub)

def load_config(env) -> Config:
    bearer = env.get("LOCAL_DELEGATE_BEARER_TOKEN", "")
    router = env.get("LOCAL_DELEGATE_ROUTER_TOKEN", "")
    if not bearer or not router:
        raise ConfigError("LOCAL_DELEGATE_BEARER_TOKEN and LOCAL_DELEGATE_ROUTER_TOKEN are required")
    roots = env.get("LOCAL_DELEGATE_ALLOWED_ROOTS", "")
    return Config(
        bearer_token=bearer,
        router_token=router,
        host=env.get("LOCAL_DELEGATE_HOST", "127.0.0.1"),
        port=int(env.get("LOCAL_DELEGATE_PORT", "3006")),
        router_url=env.get("LOCAL_DELEGATE_ROUTER_URL", "http://192.168.6.153:8000/v1"),
        allowed_repo_roots=tuple(p for p in roots.split(os.pathsep) if p),
        jobs_dir=env.get("LOCAL_DELEGATE_JOBS_DIR", _default_local_appdata("jobs")),
        ledger_path=env.get("LOCAL_DELEGATE_LEDGER", _default_local_appdata("ledger.jsonl")),
        lease_path=env.get("LOCAL_DELEGATE_LEASE", _default_local_appdata("gpu.lock")),
        opencode_exe=env.get("LOCAL_DELEGATE_OPENCODE_EXE", ""),
        overlay_dir=env.get("LOCAL_DELEGATE_OVERLAY", ""),
        ask_timeout_s=int(env.get("LOCAL_DELEGATE_ASK_TIMEOUT_S", "600")),
        job_timeout_s=int(env.get("LOCAL_DELEGATE_JOB_TIMEOUT_S", "1800")),
        ask_max_input_bytes=int(env.get("LOCAL_DELEGATE_ASK_MAX_INPUT_BYTES", "400000")),
    )
