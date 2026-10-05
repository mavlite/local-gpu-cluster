#!/usr/bin/env bash
# Phase-1 gate: pack every gate-* Polyglot run's per-exercise results on LXC 158 into one tarball.
#   quality_pack.sh <out.tgz>
# Only .aider.results.json files are packed (no chat histories, no keys). Paths inside the
# tarball are <timestamp>--gate-<label>-NN/python/exercises/practice/<exercise>/.aider.results.json.
set -Eeuo pipefail
OUT="${1:?out.tgz}"
cd /root/aider/tmp.benchmarks
mapfile -t files < <(find . -path './*--gate-*' -name .aider.results.json | sort)
[ "${#files[@]}" -gt 0 ] || { echo "no gate-* results under $(pwd)" >&2; exit 1; }
printf '%s\n' "${files[@]}" | tar -czf "$OUT" -T -
echo "packed ${#files[@]} result files from $(printf '%s\n' "${files[@]}" | cut -d/ -f2 | sort -u | wc -l) runs into $OUT"
