#!/usr/bin/env bash
# toolbox-ratelimit — clamp aggressive tools to lab-safe rates.
#
# Installed as /usr/local/bin/{hydra,masscan,ffuf,feroxbuster,nuclei}; the real binaries
# live in /opt/toolbox/real/. Dispatches on $0, rewrites the rate/concurrency flag to the
# cap when it is too high, appends the cap when the flag is absent, then execs the real
# tool. Clamping happens here rather than in an ARTEX intercept rule because those match
# with RE2, which has no lookahead and so cannot detect a MISSING flag.
#
# The targets are live production game and wiki servers; tool defaults (hydra 16 tasks,
# masscan unlimited) are far more aggressive than a careful operator. Set TOOLBOX_NO_CLAMP=1
# to bypass — intended for an operator on the console, not for agents.
set -u

tool=$(basename "$0")
real=/opt/toolbox/real/$tool
[ -x "$real" ] || { echo "toolbox-ratelimit: no real binary at $real" >&2; exit 127; }

if [ "${TOOLBOX_NO_CLAMP:-0}" = "1" ]; then exec "$real" "$@"; fi

# Informational invocations send no traffic: pass them through untouched, so --version
# and --help keep working (and the build-time manifest reads real version strings).
for a in "$@"; do
  case $a in
    -h|--help|-V|--version|-version|version) exec "$real" "$@" ;;
  esac
done

case $tool in
  hydra)       flags="-t";                  cap=4    ;;
  masscan)     flags="--rate";              cap=1000 ;;
  ffuf)        flags="-rate:50 -t:10"                ;;
  feroxbuster) flags="--rate-limit:50 --threads:10"  ;;
  nuclei)      flags="-rl:50 -c:10"                  ;;
  *)           exec "$real" "$@"                     ;;
esac

# Normalise to "flag:cap" pairs so single- and multi-flag tools share one code path.
specs=""
for f in $flags; do
  case $f in
    *:*) specs="$specs $f" ;;
    *)   specs="$specs $f:$cap" ;;
  esac
done

args=("$@")

for spec in $specs; do
  flag=${spec%%:*}
  limit=${spec##*:}
  found=0
  for i in "${!args[@]}"; do
    # "--rate 1000" (value in the next argument)
    if [ "${args[$i]}" = "$flag" ] && [ $((i + 1)) -lt ${#args[@]} ]; then
      found=1
      val=${args[$((i + 1))]}
      if [[ $val =~ ^[0-9]+$ ]] && [ "$val" -gt "$limit" ]; then
        echo "[toolbox] $tool: clamped $flag $val -> $limit (lab rate policy)" >&2
        args[i + 1]=$limit
      fi
    # "--rate=1000" (value joined with =)
    elif [[ ${args[$i]} == "$flag"=* ]]; then
      found=1
      val=${args[$i]#*=}
      if [[ $val =~ ^[0-9]+$ ]] && [ "$val" -gt "$limit" ]; then
        echo "[toolbox] $tool: clamped $flag=$val -> $limit (lab rate policy)" >&2
        args[i]="$flag=$limit"
      fi
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "[toolbox] $tool: no $flag given; applying lab default $flag $limit" >&2
    args+=("$flag" "$limit")
  fi
done

exec "$real" "${args[@]}"
