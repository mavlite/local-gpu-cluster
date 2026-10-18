"""swap-chat-model.sh profiles carry their own slot count (follow-up A of coding-engine phase 1).

Every profile's per-slot context (CTX / PARALLEL) must stay within the model's native window, a profile swap must
write LLAMA_PARALLEL alongside LLAMA_CTX (so swapping away from qwen3.8's 3 x 128K never leaves another model at
3 x 87K), and lib/chat-flags.sh refuses a layout that over-allocates a slot."""
import os
import subprocess

import pytest

HERE = os.path.dirname(__file__)
SWAP = os.path.join(HERE, "..", "..", "swap-chat-model.sh")
LIB = os.path.join(HERE, "..", "..", "lib", "chat-flags.sh")
PROFILES = ["qwen3.6", "qwen3.6-fast", "coder", "devstral", "devstral-large", "qwen3.8", "qwen3.8-redteam"]
FIELDS = "REPO QUANT ALIAS SPLIT CTX CACHE_REUSE KV SPLIT_MODE SPEC_TYPE SPEC_NMAX PARALLEL".split()


def bash(script, **env):
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **{k: str(v) for k, v in env.items()}}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full)


def profile(name):
    r = bash(f'SWAP_CHAT_MODEL_LIB_ONLY=1 source "{SWAP}" || exit 97; declare -F get_profile >/dev/null || exit 98; '
             f'get_profile "{name}"')
    assert r.returncode == 0, (r.returncode, r.stderr)
    parts = r.stdout.split()
    assert len(parts) in (10, 11), parts
    d = dict(zip(FIELDS, parts))
    d.setdefault("PARALLEL", "1")
    return d


@pytest.mark.parametrize("name", PROFILES)
def test_every_profile_keeps_one_slot_within_the_native_window(name):
    p = profile(name)
    assert int(p["PARALLEL"]) >= 1
    assert int(p["CTX"]) // int(p["PARALLEL"]) <= 262144


@pytest.mark.parametrize("name", ["qwen3.8", "qwen3.8-redteam"])
def test_qwen38_profiles_are_three_slots_of_128k(name):
    p = profile(name)
    assert (p["CTX"], p["PARALLEL"], p["CACHE_REUSE"]) == ("393216", "3", "0")


def test_other_profiles_stay_single_slot():
    for name in PROFILES[:5]:
        assert profile(name)["PARALLEL"] == "1", name


def test_write_config_env_writes_parallel_and_ctx_together(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("LLAMA_CTX=262144\nLLAMA_PARALLEL=1\nOTHER=x\n")
    r = bash(f'SWAP_CHAT_MODEL_LIB_ONLY=1 source "{SWAP}" || exit 97; CONFIG_ENV="{cfg.as_posix()}"; '
             'write_config_env r q a 1,1 393216 0 q8_0 tensor draft-mtp 3 3')
    assert r.returncode == 0, r.stderr
    text = cfg.read_text()
    assert "LLAMA_CTX=393216" in text and "LLAMA_PARALLEL=3" in text and "OTHER=x" in text
    assert "LLAMA_PARALLEL=1" not in text


def test_write_config_env_defaults_parallel_to_one_for_ten_field_profiles(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("LLAMA_PARALLEL=3\n")
    r = bash(f'SWAP_CHAT_MODEL_LIB_ONLY=1 source "{SWAP}" || exit 97; CONFIG_ENV="{cfg.as_posix()}"; '
             'write_config_env r q a 1,1 262144 0 q8_0 layer none 0')
    assert r.returncode == 0, r.stderr
    assert "LLAMA_PARALLEL=1" in cfg.read_text()


def test_swap_to_qwen38_sets_the_vision_projector(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("LLAMA_MMPROJ=\nROUTER_VISION_ENABLED=0\n")
    r = bash(f'SWAP_CHAT_MODEL_LIB_ONLY=1 source "{SWAP}" || exit 97; CONFIG_ENV="{cfg.as_posix()}"; '
             'write_config_env unsloth/Qwen3.8-27B-GGUF UD-Q6_K_XL qwen3.8 1,1 393216 0 q8_0 tensor draft-mtp 3 3')
    assert r.returncode == 0, r.stderr
    text = cfg.read_text()
    # suffix match: Git Bash on Windows rewrites a leading /opt/... arg (MSYS path
    # conversion); the Linux host keeps it absolute. The suffix is stable on both.
    assert "qwen38-mmproj/mmproj-F16.gguf" in text
    assert "LLAMA_MMPROJ_DEVICE=ROCm1" in text
    assert "ROUTER_VISION_ENABLED=1" in text


def test_swap_away_from_qwen38_clears_the_vision_projector(tmp_path):
    # Regression: swapping qwen3.8 -> qwen3.6 left the qwen3.8 mmproj attached,
    # so qwen3.6 aborted on load (n_embd mismatch) and crash-looped.
    cfg = tmp_path / "config.env"
    cfg.write_text("LLAMA_MMPROJ=/opt/models/qwen38-mmproj/mmproj-F16.gguf\n"
                   "LLAMA_MMPROJ_DEVICE=ROCm1\nROUTER_VISION_ENABLED=1\n")
    r = bash(f'SWAP_CHAT_MODEL_LIB_ONLY=1 source "{SWAP}" || exit 97; CONFIG_ENV="{cfg.as_posix()}"; '
             'write_config_env unsloth/Qwen3.6-35B-A3B-GGUF UD-Q6_K rag-qwen3.6 1,1.5 262144 1024 q8_0 layer none 0')
    assert r.returncode == 0, r.stderr
    text = cfg.read_text()
    assert "LLAMA_MMPROJ=\n" in text and "mmproj-F16.gguf" not in text
    assert "LLAMA_MMPROJ_DEVICE=\n" in text
    assert "ROUTER_VISION_ENABLED=0" in text


@pytest.mark.parametrize("ctx,par,ok", [("393216", "3", True), ("262144", "1", True), ("393216", "1", False),
                                        ("262144", "0", False), ("abc", "1", False), ("262144", "x", False)])
def test_slot_guard(ctx, par, ok):
    r = bash(f'source "{LIB}" || exit 97; declare -F chat_check_slot_ctx >/dev/null || exit 98; chat_check_slot_ctx',
             LLAMA_CTX=ctx, LLAMA_PARALLEL=par)
    assert (r.returncode == 0) == ok, (r.returncode, r.stderr)
    if not ok:
        assert r.returncode not in (97, 98) and "LLAMA_" in r.stderr


def test_slot_guard_limit_is_configurable():
    r = bash(f'source "{LIB}" || exit 97; declare -F chat_check_slot_ctx >/dev/null || exit 98; chat_check_slot_ctx',
             LLAMA_CTX="131072", LLAMA_PARALLEL="1", LLAMA_MAX_SLOT_CTX="65536")
    assert r.returncode not in (0, 97, 98) and "LLAMA_" in r.stderr
