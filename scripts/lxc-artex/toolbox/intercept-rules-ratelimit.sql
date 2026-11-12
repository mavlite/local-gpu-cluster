-- intercept-rules-ratelimit.sql — keep agents on the rate-limited tool wrappers.
--
-- The caps themselves are enforced structurally by /usr/local/bin/<tool>
-- (toolbox-ratelimit.sh), NOT by these rules: ARTEX matches intercept patterns with Go's
-- RE2, which has no lookahead, so no regex can detect a MISSING --rate flag. What a regex
-- CAN match is the two ways to go around the wrapper, which is all these rules do:
--   1. calling the real binary by its /opt/toolbox/real/ path
--   2. setting TOOLBOX_NO_CLAMP=1 (the operator-console bypass)
--
-- Idempotent by deleting its own '[lab] ' rows first and re-inserting: intercept_rules has
-- no unique index on name, and adding one would be a schema change to upstream's table.
-- Only rows this file owns are touched. Takes effect on the next tool call; no restart.
-- Delete the rows (or toggle enabled=false in Settings > Intercept rules) to lift.
--
-- Apply: docker exec -i artex-global-postgres-1 psql -U artex -d artex \
--          -v ON_ERROR_STOP=1 < intercept-rules-ratelimit.sql

BEGIN;

DELETE FROM intercept_rules WHERE name IN (
  '[lab] rate-limited tools: no direct real-binary call',
  '[lab] rate-limited tools: no clamp bypass'
);

INSERT INTO intercept_rules
  (name, enabled, priority, match_target, match_type, pattern, action, message,
   timeout_enabled, timeout_seconds, timeout_action)
VALUES
  ('[lab] rate-limited tools: no direct real-binary call', true, 95, 'tool_input', 'regex',
   '/opt/toolbox/real/', 'deny',
   'Blocked: /opt/toolbox/real/ holds the unwrapped binaries. Call the tool by its plain '
   || 'name (hydra, masscan, ffuf, feroxbuster, nuclei) so the lab rate caps apply — these '
   || 'targets are live production services. Caps: hydra -t 4, masscan --rate 1000, '
   || 'ffuf -rate 50 -t 10, feroxbuster --rate-limit 50 --threads 10, nuclei -rl 50 -c 10.',
   false, 60, 'deny'),

  ('[lab] rate-limited tools: no clamp bypass', true, 95, 'tool_input', 'regex',
   '(?i)TOOLBOX_NO_CLAMP', 'deny',
   'Blocked: TOOLBOX_NO_CLAMP disables the lab rate caps and is for an operator on the '
   || 'console, not for agents. Run the tool normally; if a cap genuinely blocks the test, '
   || 'record that as a fact and let the planner raise it with the operator.',
   false, 60, 'deny');

COMMIT;

SELECT id, enabled, priority, action, name FROM intercept_rules
WHERE name LIKE '[lab]%' ORDER BY id;
