import pytest
from scripts.delegate.config import load_config, Config, ConfigError

BASE = {"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"}

def test_defaults_applied():
    c = load_config(BASE)
    assert isinstance(c, Config)
    assert c.host == "127.0.0.1" and c.port == 3006
    assert c.router_url.endswith("/v1")
    assert c.ask_max_input_bytes == 400_000

def test_missing_required_tokens_raise():
    with pytest.raises(ConfigError):
        load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b"})  # router token absent

def test_allowed_roots_parsed_from_pathsep_list():
    c = load_config({**BASE, "LOCAL_DELEGATE_ALLOWED_ROOTS": r"C:\a;C:\b"})
    assert c.allowed_repo_roots == (r"C:\a", r"C:\b")

def test_lease_path_defaults_to_gpu_lock():
    c = load_config(BASE)
    assert c.lease_path.endswith("gpu.lock")


def test_timeout_and_size_defaults():
    c = load_config(BASE)
    assert c.job_timeout_s == 1800 and c.ask_timeout_s == 600
    assert c.ask_max_input_bytes == 400_000


def test_timeout_and_size_env_overrides_take_effect():
    c = load_config({**BASE,
                     "LOCAL_DELEGATE_JOB_TIMEOUT_S": "90",
                     "LOCAL_DELEGATE_ASK_TIMEOUT_S": "45",
                     "LOCAL_DELEGATE_ASK_MAX_INPUT_BYTES": "12345"})
    assert c.job_timeout_s == 90 and c.ask_timeout_s == 45
    assert c.ask_max_input_bytes == 12345


def test_overlay_dir_defaults_to_repo_overlay():
    import os
    c = load_config(BASE)
    assert c.overlay_dir.replace("\\", "/").endswith("clients/opencode-delegate")
    # the default must actually point at the shipped overlay (agent def present)
    assert os.path.isfile(os.path.join(c.overlay_dir, "agent", "delegate.md"))
