#!/usr/bin/env bash
# apply-prompts-en.sh — save English agent prompts as a new prompt version in ARTEX.
#
# Run inside LXC 158 (or anywhere `docker exec artex-global-postgres-1 psql` works):
#   bash apply-prompts-en.sh /root/artex-global/prompts-en
#
# Source: h4nt3r2409/hartex @ 8b60f82 (AGPL-3.0, English-only edition of the same
# upstream), prompt constants goalsDefaultTmpl … RetesterDefaultPrompt. Each was checked
# against our Chinese default before use: identical Go-template skeleton, same
# tool/identifier tokens, renders with the agent's real vars struct (missingkey=error),
# no CJK left, no new URLs/IPs, and read by hand for added or changed instructions.
#
# Mirrors db.SavePrompt (what "Save as new version" does): appends version N+1 and
# points agents.current_prompt_id at it. ARTEX reads the current prompt from the DB on
# every render, so no restart is needed; "Restore default" in the UI rolls back.
# Skips an agent whose current prompt is not a built-in default (user-edited) and is a
# no-op when the English text is already a version of that agent.
set -euo pipefail

DIR=${1:?usage: apply-prompts-en.sh <dir with <agent-key>.txt>}
PSQL=(docker exec -i artex-global-postgres-1 psql -U artex -d artex -v ON_ERROR_STOP=1 -At)
NOTE="English (hartex 8b60f82)"
KEYS=(goals planner mainagent worker auto pentest reporter retester)

for key in "${KEYS[@]}"; do
  f="$DIR/$key.txt"
  [[ -s "$f" ]] || { echo "missing or empty: $f" >&2; exit 1; }
done

for key in "${KEYS[@]}"; do
  txt=$(cat "$DIR/$key.txt"; printf x); txt=${txt%x}   # keep trailing newlines exactly
  result=$("${PSQL[@]}" -v key="$key" -v txt="$txt" -v note="$NOTE" <<'SQL'
WITH a AS (
  SELECT ag.id, p.note AS cur_note
  FROM agents ag LEFT JOIN agent_prompts p ON p.id = ag.current_prompt_id
  WHERE ag.key = :'key'
), ins AS (
  INSERT INTO agent_prompts (agent_id, version, template_text, note, updated_by)
  SELECT a.id,
         (SELECT COALESCE(max(version), 0) + 1 FROM agent_prompts WHERE agent_id = a.id),
         :'txt', :'note', 'system'
  FROM a
  WHERE a.cur_note IN ('Built-in default', '内置默认')
    AND NOT EXISTS (SELECT 1 FROM agent_prompts p WHERE p.agent_id = a.id AND p.template_text = :'txt')
  RETURNING id, agent_id, version
), upd AS (
  UPDATE agents SET current_prompt_id = ins.id, updated_at = now()
  FROM ins WHERE agents.id = ins.agent_id
  RETURNING ins.version
)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM a) THEN 'no such agent'
  WHEN EXISTS (SELECT 1 FROM upd) THEN 'saved as v' || (SELECT version FROM upd)
  WHEN (SELECT cur_note FROM a) IN ('Built-in default', '内置默认', :'note') THEN 'already applied'
  ELSE 'skipped: current prompt is user-edited (' || COALESCE((SELECT cur_note FROM a), 'none') || ')'
END;
SQL
)
  printf '%-10s %s\n' "$key" "$result"
done
