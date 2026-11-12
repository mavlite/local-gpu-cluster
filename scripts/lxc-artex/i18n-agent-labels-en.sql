-- i18n-agent-labels-en.sql — English names/descriptions for ARTEX's system-seeded agents
-- and their prompt-variable labels (artex-global 133f3cf).
--
-- The web UI translates these only where it goes through useAgentLabels()
-- (web/src/lib/agent-i18n.ts); other screens show the raw DB value, which is the
-- Chinese seed (and Korean for the reporter). Writing the UI's own English strings
-- (web/messages/en.json → builtinAgents) into the DB makes every screen English.
--
-- Each UPDATE matches the exact seed text, so a value the user has edited is never
-- overwritten and re-running is a no-op. Safe while a task is running (labels only;
-- no restart). The agent prompt bodies (agent_prompts.template_text) are NOT touched:
-- they are the benchmarked Chinese "brain" (agent/outputlang.go).
--
-- Apply:  docker exec -i artex-global-postgres-1 psql -U artex -d artex -v ON_ERROR_STOP=1 < i18n-agent-labels-en.sql

BEGIN;

UPDATE agents AS a SET name = t.name_en, updated_at = now()
FROM (VALUES
  ('goals',     '目标拆解',   'Goal Decomposition'),
  ('planner',   '规划',       'Planning'),
  ('mainagent', '主',         'Main'),
  ('worker',    '执行',       'Execution'),
  ('pentest',   '渗透测试',   'Penetration Test'),
  ('reporter',  '보고서 작성', 'Report Writing'),
  ('retester',  '漏洞复测',   'Vulnerability Retest')
) AS t(key, name_seed, name_en)
WHERE a.key = t.key AND a.name = t.name_seed;

UPDATE agents AS a SET description = t.desc_en, updated_at = now()
FROM (VALUES
  ('goals', '把渗透任务目标拆解成若干独立、可验证的子目标。',
   'Breaks the pentest objective down into several independent, verifiable sub-goals.'),
  ('planner', '读取态势、判定目标，只在确有未覆盖的新方向时补充探索意图（每任务一个规划循环）。',
   'Reads the situation and decides on goals, adding exploration intents only when there is a genuinely uncovered new direction (one planning loop per task).'),
  ('mainagent', '人机接口：观察进展，把人的意图落成 hint 或高优先级意图。',
   'Human-machine interface: watches progress and turns human intent into hints or high-priority intents.'),
  ('worker', '领取一条意图执行，把发现的事实/漏洞写回知识图谱后停止。',
   'Picks up a single intent and executes it, writing discovered facts / vulnerabilities back to the knowledge graph before stopping.'),
  ('auto', '平台操作助手：用工具管理任务(建/看/暂停/给提示)与资产，并可创建/修改 skill、自定义工具、MCP。',
   'Platform operations assistant: uses tools to manage tasks (create / view / pause / hint) and assets, and can create or modify skills, custom tools, and MCP.'),
  ('pentest', '独立渗透 agent：一人从侦察→找攻击面→深入利用→验证→收尾走完整条链，自己规划、自己执行、自己对抗式验证。',
   'Standalone pentest agent: single-handedly runs the full chain from recon → attack-surface discovery → deep exploitation → verification → wrap-up, planning, executing, and adversarially verifying on its own.'),
  ('reporter', '취약점 상세 보고서 작성: 취약점을 발견하면 자동으로 트리거되어, 증거와 실행 과정을 조회한 뒤 Markdown 보고서를 작성해 해당 취약점에 기록합니다.',
   'Writes a detailed vulnerability report: triggered automatically when a finding is recorded, it reviews the evidence and execution trace, then writes a Markdown report to that finding.'),
  ('retester', '从漏洞详情手动启动，读取原证据并保存独立复测结论。',
   'Launched manually from a finding''s detail view; reads the original evidence and saves an independent retest conclusion.')
) AS t(key, desc_seed, desc_en)
WHERE a.key = t.key AND a.description = t.desc_seed;

-- Prompt-variable labels (System > Agents > prompt editor). var_name is what the
-- template references and is left alone; only the human label changes.
UPDATE agent_prompt_vars AS v SET description = t.desc_en
FROM agents AS a, (VALUES
  ('goals',     'EngagementDescription', '任务描述（测试对象/背景）',     'Engagement description (target / background)'),
  ('planner',   'Goal',                  '任务总目标',                   'Overall task goal'),
  ('planner',   'AssetSummary',          '资产计数/类型分布摘要(可选)',    'Asset count / type-distribution summary (optional)'),
  ('mainagent', 'Goal',                  '当前任务目标',                 'Current task goal'),
  ('mainagent', 'AssetSummary',          '开局态势摘要(可选)',            'Opening-situation summary (optional)'),
  ('mainagent', 'FindingsSummary',       '已确认漏洞摘要(可选)',          'Confirmed-findings summary (optional)'),
  ('worker',    'ProxyAddr',             '记录代理地址(驱动 if 双文案)',   'Recording proxy address (drives the if-branch wording)'),
  ('worker',    'WorkerName',            'worker 自我标识(可选)',         'Worker self-identifier (optional)')
) AS t(key, var_name, desc_seed, desc_en)
WHERE v.agent_id = a.id AND a.key = t.key AND v.var_name = t.var_name AND v.description = t.desc_seed;

-- Example values shown next to the labels.
UPDATE agent_prompt_vars SET example = 'Test the example.com site'  WHERE example = '测试 example.com 站点';
UPDATE agent_prompt_vars SET example = 'Gain admin access to example.com' WHERE example = '拿下 example.com 的管理员权限';

COMMIT;
