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
