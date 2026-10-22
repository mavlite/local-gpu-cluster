"""scripts/lib/chat-flags.sh: the chat unit's speculative-decoding and multimodal-projector flag lines.

Every rendered line is a systemd ExecStart continuation: four-space indent, ends with ' \\' and a newline, so the
block can sit inside the unit heredoc between two other flags without ever producing a blank (terminating) line."""
import os
import subprocess

import pytest

LIB = os.path.join(os.path.dirname(__file__), "..", "..", "lib", "chat-flags.sh")


def render(fn, **env):
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **{k: str(v) for k, v in env.items()}}
    # printf x keeps the trailing newline that $( ) would strip -- exactly how 51-lxc-amd.sh captures it.
    # A missing library or function must fail the test, not render an empty (vacuously "correct") block.
    script = (f'source "{LIB}" || exit 97; declare -F {fn} >/dev/null || exit 98; '
              f'out="$({fn}; rc=$?; printf x; exit $rc)" || exit $?; printf "%s" "${{out%x}}"')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full)


def lines_ok(text):
    return all(l.startswith("    --") and l.endswith(" \\") for l in text.splitlines())


def test_mtp_lines_match_the_current_unit():
    r = render("chat_spec_lines", LLAMA_SPEC_TYPE="draft-mtp", LLAMA_SPEC_NMAX=3, LLAMA_SPEC_PMIN=0)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "    --spec-type draft-mtp \\\n    --spec-draft-n-max 3 \\\n"


def test_spec_sampling_is_added_only_when_set():
    r = render("chat_spec_lines", LLAMA_SPEC_TYPE="draft-mtp", LLAMA_SPEC_NMAX=3, LLAMA_SPEC_PMIN=0,
               LLAMA_SPEC_SAMPLING="probabilistic")
    assert r.returncode == 0, r.stderr
    assert "    --spec-draft-sampling probabilistic \\\n" in r.stdout and lines_ok(r.stdout)
    assert r.stdout.endswith("\\\n")


def test_spec_sampling_rejects_unknown_values():
    r = render("chat_spec_lines", LLAMA_SPEC_TYPE="draft-mtp", LLAMA_SPEC_NMAX=3, LLAMA_SPEC_SAMPLING="fast")
    assert r.returncode != 0 and "LLAMA_SPEC_SAMPLING" in r.stderr


def test_no_spec_lines_when_spec_type_none():
    r = render("chat_spec_lines", LLAMA_SPEC_TYPE="none", LLAMA_SPEC_NMAX=3, LLAMA_SPEC_SAMPLING="probabilistic")
    assert r.returncode == 0 and r.stdout == ""


def test_pmin_line_only_when_nonzero():
    r = render("chat_spec_lines", LLAMA_SPEC_TYPE="draft-mtp", LLAMA_SPEC_NMAX=3, LLAMA_SPEC_PMIN="0.5")
    assert "    --spec-draft-p-min 0.5 \\\n" in r.stdout and lines_ok(r.stdout)


def test_external_draft_model_path_is_unchanged():
    r = render("chat_spec_lines", LLAMA_DRAFT_REPO="Qwen/Qwen3.5-0.8B-GGUF", LLAMA_DRAFT_QUANT="Q4_K_M",
               LLAMA_SPEC_NMAX=16, LLAMA_SPEC_NMIN=0, LLAMA_SPEC_TYPE="draft-mtp")
    assert r.stdout.startswith("    --hf-repo-draft Qwen/Qwen3.5-0.8B-GGUF:Q4_K_M \\\n") and lines_ok(r.stdout)


def test_no_projector_renders_no_mmproj():
    r = render("chat_mmproj_lines")
    assert r.returncode == 0 and r.stdout == "    --no-mmproj \\\n"


def test_projector_with_device_and_token_cap():
    r = render("chat_mmproj_lines", LLAMA_MMPROJ="/opt/models/qwen38-mmproj/mmproj-F16.gguf",
               LLAMA_MMPROJ_DEVICE="ROCm1", LLAMA_IMAGE_MAX_TOKENS=1280)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ("    --mmproj /opt/models/qwen38-mmproj/mmproj-F16.gguf \\\n"
                        "    --mmproj-device ROCm1 \\\n    --image-max-tokens 1280 \\\n")


def test_projector_without_optional_settings():
    r = render("chat_mmproj_lines", LLAMA_MMPROJ="/m/p.gguf")
    assert r.stdout == "    --mmproj /m/p.gguf \\\n"


@pytest.mark.parametrize("bad", ["many", "-5", "12.5"])
def test_image_token_cap_must_be_a_positive_integer(bad):
    r = render("chat_mmproj_lines", LLAMA_MMPROJ="/m/p.gguf", LLAMA_IMAGE_MAX_TOKENS=bad)
    assert r.returncode != 0 and "LLAMA_IMAGE_MAX_TOKENS" in r.stderr


def test_no_template_lines_when_unset():
    r = render("chat_template_lines")
    assert r.returncode == 0 and r.stdout == ""


def test_template_line_renders_the_file_flag():
    r = render("chat_template_lines", LLAMA_CHAT_TEMPLATE="/opt/models/chat-templates/qwen3.8.jinja")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "    --chat-template-file /opt/models/chat-templates/qwen3.8.jinja \\\n"


# ---- the real unit template in 51-lxc-amd.sh, rendered with the library's blocks -----------------------------

SCRIPT51 = os.path.join(os.path.dirname(__file__), "..", "..", "51-lxc-amd.sh")


def render_unit(**env):
    text = open(SCRIPT51, encoding="utf-8").read()
    start = text.index("cat > /etc/systemd/system/llamacpp-chat.service <<EOF\n") + len("cat > /etc/systemd/system/llamacpp-chat.service <<EOF\n")
    body = text[start:text.index("\nEOF\n", start)]
    base = {"LLAMA_HF_REPO": "r", "LLAMA_HF_QUANT": "q", "LLAMA_ALIAS": "a", "LLAMA_CTX": "393216", "LLAMA_KV_TYPE": "q8_0",
            "LLAMA_PARALLEL": "3", "LLAMA_CACHE_REUSE": "0", "LLAMA_CACHE_RAM_MB": "16384", "LLAMA_TENSOR_SPLIT": "1,1",
            "LLAMA_SPLIT_MODE": "tensor", "LLAMA_THREADS": "8", "LLAMA_FLASH_ATTN": "auto",
            "LLAMA_SPEC_TYPE": "draft-mtp", "LLAMA_SPEC_NMAX": "3", "LLAMA_SPEC_PMIN": "0", "CHAT_CACHE_LIST": ""}
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **base, **{k: str(v) for k, v in env.items()}}
    script = (f'source "{LIB}" || exit 97; '
              'DRAFT_LINES="$(chat_spec_lines; printf x)"; DRAFT_LINES="${DRAFT_LINES%x}"; '
              'MMPROJ_LINES="$(chat_mmproj_lines; printf x)"; MMPROJ_LINES="${MMPROJ_LINES%x}"; '
              'TEMPLATE_LINES="$(chat_template_lines; printf x)"; TEMPLATE_LINES="${TEMPLATE_LINES%x}"; '
              'OFFLINE_LINES="$(chat_offline_lines "$LLAMA_HF_REPO" "$LLAMA_HF_QUANT" "$CHAT_CACHE_LIST"; printf x)"; OFFLINE_LINES="${OFFLINE_LINES%x}"; '
              f'cat <<EOF\n{body}\nEOF\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full)
    assert r.returncode == 0, r.stderr
    return r.stdout


def exec_start(unit):
    lines = unit.splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("ExecStart=/"))
    out = [lines[i]]
    while out[-1].endswith("\\"):
        i += 1
        out.append(lines[i])
    return out


def test_default_unit_is_text_only_with_one_contiguous_execstart():
    es = exec_start(render_unit())
    assert all(l.strip() for l in es), "blank line inside ExecStart ends the command early"
    joined = " ".join(es)
    assert "--no-mmproj" in joined and "--mmproj " not in joined
    assert "--spec-type draft-mtp" in joined and "--flash-attn" in joined and "--metrics" in joined


def test_vision_unit_carries_the_projector_flags_and_stays_contiguous():
    es = exec_start(render_unit(LLAMA_MMPROJ="/opt/models/qwen38-mmproj/mmproj-F16.gguf", LLAMA_MMPROJ_DEVICE="ROCm1",
                                LLAMA_IMAGE_MAX_TOKENS="1280", LLAMA_SPEC_SAMPLING="probabilistic"))
    assert all(l.strip() for l in es)
    joined = " ".join(es)
    for flag in ("--mmproj /opt/models/qwen38-mmproj/mmproj-F16.gguf", "--mmproj-device ROCm1",
                 "--image-max-tokens 1280", "--spec-draft-sampling probabilistic", "--flash-attn", "--metrics"):
        assert flag in joined, flag
    assert "--no-mmproj" not in joined


def test_template_override_unit_carries_the_file_flag_and_stays_contiguous():
    es = exec_start(render_unit(LLAMA_CHAT_TEMPLATE="/opt/models/chat-templates/qwen3.8.jinja"))
    assert all(l.strip() for l in es)
    joined = " ".join(es)
    assert "--chat-template-file /opt/models/chat-templates/qwen3.8.jinja" in joined
    assert "--flash-attn" in joined and "--metrics" in joined


def test_default_unit_has_no_template_flag():
    joined = " ".join(exec_start(render_unit()))
    assert "--chat-template-file" not in joined


# ---- chat_offline_lines: force llama.cpp offline when the model is already cached -------------------------------
# Incident 2026-10-19: swapping to hemmingway hung llama-server ~150s in futex on a stalled
# HuggingFace :443 socket during the --hf-repo revalidation, despite a complete 23.9 GB cached
# blob; the router's swap timeout then 503'd. --offline (b11578+) forces cache use and skips the
# network. We only add it when `llama-server --cache-list` already lists the target, so a
# first-ever swap to an uncached model still downloads normally.

# Real `llama-server --cache-list` output shape: "  N. <repo>:<quant>", one per line.
# Note llama.cpp strips the unsloth "UD-" prefix from the cache key: profile UD-Q6_K_XL is
# listed as :Q6_K_XL, UD-IQ4_XS as :IQ4_XS, etc. Non-UD quants (Q6_K, Q8_0) list verbatim.
CACHE_LIST = (
    "number of models in cache: 4\n"
    "   1. unsloth/Qwen3.8-27B-GGUF:Q6_K_XL\n"
    "   2. bartowski/Altworld_Hemmingway-1-GGUF:Q6_K\n"
    "   3. unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M\n"
    "   4. unsloth/Qwen3-Coder-Next-GGUF:IQ4_XS\n"
)


def render_offline(repo, quant, cache_list):
    script = (f'source "{LIB}" || exit 97; declare -F chat_offline_lines >/dev/null || exit 98; '
              'out="$(chat_offline_lines "$1" "$2" "$3"; rc=$?; printf x; exit $rc)" || exit $?; printf "%s" "${out%x}"')
    return subprocess.run(["bash", "-c", script, "bash", repo, quant, cache_list],
                          capture_output=True, text=True,
                          env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})


def test_offline_line_when_exact_quant_cached():
    r = render_offline("bartowski/Altworld_Hemmingway-1-GGUF", "Q6_K", CACHE_LIST)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "    --offline \\\n"


def test_offline_line_when_ud_prefixed_quant_cached():
    # Profile quant carries the UD- prefix; the cache key does not. Must still match.
    r = render_offline("unsloth/Qwen3.8-27B-GGUF", "UD-Q6_K_XL", CACHE_LIST)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "    --offline \\\n"
    r2 = render_offline("unsloth/Qwen3-Coder-Next-GGUF", "UD-IQ4_XS", CACHE_LIST)
    assert r2.stdout == "    --offline \\\n"


def test_no_offline_when_that_quant_not_cached():
    # qwen3.6 repo is cached, but only at Q4_K_M -- a swap to Q6_K_XL must still hit the network.
    r = render_offline("unsloth/Qwen3.6-35B-A3B-GGUF", "UD-Q6_K_XL", CACHE_LIST)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_no_offline_on_quant_prefix_partial_match():
    # Profile wants Q6_K; cache has only :Q6_K_XL for that repo. The :Q6_K token must NOT
    # partial-match :Q6_K_XL, or we would --offline a model that is not actually cached.
    r = render_offline("unsloth/Qwen3.8-27B-GGUF", "Q6_K", CACHE_LIST)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_no_offline_on_repo_mismatch():
    r = render_offline("someone/Other-Model-GGUF", "Q6_K", CACHE_LIST)
    assert r.returncode == 0 and r.stdout == ""


def test_no_offline_on_empty_cache_list():
    r = render_offline("bartowski/Altworld_Hemmingway-1-GGUF", "Q6_K", "")
    assert r.returncode == 0 and r.stdout == ""


def test_cached_model_unit_carries_offline_and_stays_contiguous():
    es = exec_start(render_unit(LLAMA_HF_REPO="r", LLAMA_HF_QUANT="q", CHAT_CACHE_LIST="   1. r:q\n"))
    assert all(l.strip() for l in es), "blank line inside ExecStart ends the command early"
    joined = " ".join(es)
    assert "--offline" in joined and "--flash-attn" in joined and "--metrics" in joined


def test_default_unit_has_no_offline_flag():
    # Empty cache-list (the render_unit default) -> model treated as not cached -> network path.
    assert "--offline" not in " ".join(exec_start(render_unit()))
