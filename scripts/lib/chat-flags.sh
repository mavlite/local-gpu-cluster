#!/usr/bin/env bash
# chat-flags.sh -- renders the llamacpp-chat ExecStart lines that depend on configuration: speculative decoding and
# the multimodal projector. Each line is a systemd continuation ("    --flag value \" + newline), so a block can sit
# between two other flags in the unit heredoc without ever producing a blank (terminating) line.
# Sourced by 51-lxc-amd.sh; tested by scripts/files/tests/test_chat_flags.py.

# chat_spec_lines -- speculative decoding.
#   LLAMA_DRAFT_REPO / LLAMA_DRAFT_QUANT  external draft model (path A; disabled by default)
#   LLAMA_SPEC_TYPE                        in-GGUF MTP (path B) unless "none"
#   LLAMA_SPEC_NMAX / LLAMA_SPEC_NMIN / LLAMA_SPEC_PMIN
#   LLAMA_SPEC_SAMPLING                    optional: greedy | probabilistic (--spec-draft-sampling, llama.cpp >= b11368)
chat_spec_lines() {
  local sampling="${LLAMA_SPEC_SAMPLING:-}"
  if [[ -n "$sampling" && "$sampling" != "greedy" && "$sampling" != "probabilistic" ]]; then
    echo "chat-flags: LLAMA_SPEC_SAMPLING must be greedy or probabilistic, got '$sampling'" >&2
    return 1
  fi
  if [[ -n "${LLAMA_DRAFT_REPO:-}" ]]; then
    printf '    --hf-repo-draft %s:%s \\\n' "$LLAMA_DRAFT_REPO" "${LLAMA_DRAFT_QUANT:-}"
    printf '    --n-gpu-layers-draft all \\\n'
    printf '    --spec-draft-n-max %s \\\n' "${LLAMA_SPEC_NMAX:-3}"
    printf '    --spec-draft-n-min %s \\\n' "${LLAMA_SPEC_NMIN:-0}"
  elif [[ "${LLAMA_SPEC_TYPE:-none}" != "none" ]]; then
    printf '    --spec-type %s \\\n' "$LLAMA_SPEC_TYPE"
    printf '    --spec-draft-n-max %s \\\n' "${LLAMA_SPEC_NMAX:-3}"
    if [[ "${LLAMA_SPEC_PMIN:-0}" != "0" ]]; then
      printf '    --spec-draft-p-min %s \\\n' "$LLAMA_SPEC_PMIN"
    fi
  else
    return 0
  fi
  if [[ -n "$sampling" ]]; then
    printf '    --spec-draft-sampling %s \\\n' "$sampling"
  fi
}

# chat_mmproj_lines -- vision projector.
#   LLAMA_MMPROJ             path to the projector GGUF; unset = --no-mmproj (text only, the old behaviour)
#   LLAMA_MMPROJ_DEVICE      optional backend device for the projector, e.g. ROCm1
#   LLAMA_IMAGE_MAX_TOKENS   optional positive integer cap on tokens per image
chat_mmproj_lines() {
  if [[ -z "${LLAMA_MMPROJ:-}" ]]; then
    printf '    --no-mmproj \\\n'
    return 0
  fi
  local cap="${LLAMA_IMAGE_MAX_TOKENS:-}"
  if [[ -n "$cap" && ! "$cap" =~ ^[1-9][0-9]*$ ]]; then
    echo "chat-flags: LLAMA_IMAGE_MAX_TOKENS must be a positive integer, got '$cap'" >&2
    return 1
  fi
  printf '    --mmproj %s \\\n' "$LLAMA_MMPROJ"
  if [[ -n "${LLAMA_MMPROJ_DEVICE:-}" ]]; then
    printf '    --mmproj-device %s \\\n' "$LLAMA_MMPROJ_DEVICE"
  fi
  if [[ -n "$cap" ]]; then
    printf '    --image-max-tokens %s \\\n' "$cap"
  fi
}

# chat_template_lines -- override the chat template embedded in the GGUF.
#   LLAMA_CHAT_TEMPLATE   path to a jinja template file; empty/unset = use the GGUF's embedded template (old behaviour)
# Needed for qwen3.8-redteam: the OBLITERATED GGUF ships a stripped template with no tools section, so
# llama-server silently drops the request's tools array and the model improvises tool calls in prose.
# The stock Qwen3.8's full official template (byte copy, same tokenizer/architecture) restores them.
chat_template_lines() {
  if [[ -n "${LLAMA_CHAT_TEMPLATE:-}" ]]; then
    printf '    --chat-template-file %s \\\n' "$LLAMA_CHAT_TEMPLATE"
  fi
}

# chat_check_slot_ctx -- refuse a layout that gives one slot more context than the model's native window.
#   LLAMA_CTX / LLAMA_PARALLEL   total context and slot count (llama.cpp gives each slot CTX / PARALLEL)
#   LLAMA_MAX_SLOT_CTX           native window, default 262144 (Qwen3.6/3.8); above it needs YaRN, which we do not set
chat_check_slot_ctx() {
  local ctx="${LLAMA_CTX:-}" par="${LLAMA_PARALLEL:-1}" max="${LLAMA_MAX_SLOT_CTX:-262144}"
  if [[ ! "$ctx" =~ ^[1-9][0-9]*$ || ! "$par" =~ ^[1-9][0-9]*$ || ! "$max" =~ ^[1-9][0-9]*$ ]]; then
    echo "chat-flags: LLAMA_CTX, LLAMA_PARALLEL and LLAMA_MAX_SLOT_CTX must be positive integers (got '$ctx', '$par', '$max')" >&2
    return 1
  fi
  if (( ctx / par > max )); then
    echo "chat-flags: LLAMA_CTX/LLAMA_PARALLEL = $ctx/$par gives $(( ctx / par )) per slot, above LLAMA_MAX_SLOT_CTX $max" >&2
    return 1
  fi
}
