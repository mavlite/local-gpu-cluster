#!/usr/bin/env bash
# Phase-1 gate quality sub-gate (spec §5.1, §5.6): one Aider Polyglot run on LXC 158.
#   polyglot_run.sh <alias> <run-name> <api-base> <key-file>
# Like /root/run-polyglot.sh but the endpoint is a parameter, so the same ruler can point at the
# router (coordinator alias) or straight at one CPU worker (http://172.16.10.20x:8090/v1).
# Frozen: python subset, all 34 exercises, --threads 1, edit-format whole.
set -Eeuo pipefail
ALIAS="${1:?alias}"; NAME="${2:?run-name}"; BASE="${3:?api-base}"; KEYFILE="${4:?key-file}"
[ -r "$KEYFILE" ] || { echo "key file $KEYFILE not readable" >&2; exit 1; }
cd /root/aider
export PATH="/root/aider-venv/bin:/usr/local/bin:$PATH"
export OPENAI_API_BASE="$BASE"
OPENAI_API_KEY="$(cat "$KEYFILE")"
export OPENAI_API_KEY
export AIDER_DOCKER=1 AIDER_CHECK_UPDATE=false
exec /root/aider-venv/bin/python benchmark/benchmark.py "$NAME" \
  --model "openai/$ALIAS" --edit-format whole \
  --threads 1 --num-tests -1 --languages python \
  --exercises-dir polyglot-benchmark --new
