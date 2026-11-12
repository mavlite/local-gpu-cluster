-- task-template-colo.sql — ARTEX task template "Colo pentest (IP-scoped)" with the
-- engagement scope and safeguards pre-filled (Tasks -> New task -> Task template).
--
-- A template carries description, goal, category and task-level intercept rules.
-- It can NOT carry task constraints or the asset-coverage switch, so:
--   * the constraint text is in the description (the planner reads it from step one);
--   * turn "asset coverage" OFF on the create form (it auto-grows the task scope and
--     exposes add_task_scope — how the mail host got into scope in task 2).
-- Intercept rules: 5 exact_ip ALLOW rows = whitelist (anything else is refused by
-- ARTEX's asset gate); BLOCK rows for the out-of-scope hostnames are checked first.
-- The egress lock (redteam-egress-lock.sh) stays the hard boundary.
--
-- Idempotent: re-running updates the template in place.
-- Apply: docker exec -i artex-global-postgres-1 psql -U artex -d artex -v ON_ERROR_STOP=1 < task-template-colo.sql
INSERT INTO task_templates(name, nkey, description, goal, intercept_rules)
VALUES (
  'Colo pentest (IP-scoped)',
  'colo pentest (ip-scoped)',
  $d$Authorised penetration test of the operator's own colo infrastructure.

Scope = these IPs ONLY: 38.107.232.37, 38.107.232.39, 38.107.232.51, 38.107.232.103, 38.107.232.210.

Rules of engagement:
- ALLOW: test only the IPs above and hostnames that currently resolve to them.
- DENY: any connection, scan or request to an IP outside that list, including the domain's MX/mail hosts (mail, webmail, pop) and CDN addresses (ftp, proxy - Cloudflare).
- DENY: adding any IP or domain outside that list to the task scope or assets.
- DENY: re-scanning or widening scans of a host that answered "filtered (admin-prohibited)" or "Couldn't connect" - that is the egress firewall, not the target.
- DENY: public lookup services (crt.sh, Shodan, search engines) - unreachable from this box.
- DENY: destructive actions (deleting data, dropping tables, denial of service).$d$,
  $g$Perform an authorised penetration test of the services on these IP addresses ONLY: 38.107.232.37, 38.107.232.39, 38.107.232.51, 38.107.232.103, 38.107.232.210. Hostnames under sigmadraconis.games are in scope only while they resolve to one of those IPs (currently the apex, www, wiki, panel and csp-report). mail/webmail/pop (third-party mail provider) and ftp/proxy (Cloudflare) are OUT OF SCOPE — do not scan, probe or connect to them. An egress firewall blocks every out-of-scope IP: "filtered (admin-prohibited)" or an instant "Couldn't connect" is the firewall, not the target — note nothing and move on. Locate and identify exploitable vulnerabilities and access beyond intended.$g$,
  '[
    {"enabled":true,"action":"allow","kind":"exact_ip","pattern":"38.107.232.37","note":"colo target"},
    {"enabled":true,"action":"allow","kind":"exact_ip","pattern":"38.107.232.39","note":"colo target"},
    {"enabled":true,"action":"allow","kind":"exact_ip","pattern":"38.107.232.51","note":"colo target"},
    {"enabled":true,"action":"allow","kind":"exact_ip","pattern":"38.107.232.103","note":"colo target"},
    {"enabled":true,"action":"allow","kind":"exact_ip","pattern":"38.107.232.210","note":"colo target"},
    {"enabled":true,"action":"block","kind":"fuzzy_domain","pattern":"mail.sigmadraconis.games","note":"third-party mail provider"},
    {"enabled":true,"action":"block","kind":"fuzzy_domain","pattern":"webmail.sigmadraconis.games","note":"third-party mail provider"},
    {"enabled":true,"action":"block","kind":"fuzzy_domain","pattern":"pop.sigmadraconis.games","note":"third-party mail provider"},
    {"enabled":true,"action":"block","kind":"fuzzy_domain","pattern":"ftp.sigmadraconis.games","note":"Cloudflare"},
    {"enabled":true,"action":"block","kind":"fuzzy_domain","pattern":"proxy.sigmadraconis.games","note":"Cloudflare"}
  ]'::jsonb
)
ON CONFLICT (nkey) DO UPDATE
  SET name = EXCLUDED.name, description = EXCLUDED.description,
      goal = EXCLUDED.goal, intercept_rules = EXCLUDED.intercept_rules;

SELECT id, name, jsonb_array_length(intercept_rules) AS rules FROM task_templates WHERE nkey = 'colo pentest (ip-scoped)';
