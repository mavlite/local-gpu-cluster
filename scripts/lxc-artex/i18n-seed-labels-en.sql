-- i18n-seed-labels-en.sql — translate ARTEX's Chinese seed text that the web UI
-- does NOT translate on its own (artex-global 133f3cf).
--
-- Already translated by the UI (web/src/lib/{agent,builtin}-i18n.ts) and therefore
-- left alone here: built-in agent names/descriptions, intercept rule NAMES, asset
-- rule notes, tool descriptions. Rule names must stay as seeded — the UI looks
-- them up verbatim and later seed versions de-duplicate by name.
--
-- Each UPDATE matches the exact Chinese seed text, so a value the user has edited
-- is never overwritten and re-running is a no-op. Safe while a task is running
-- (no restart needed). Text the Go server generates at runtime (conversation
-- titles "工具触发 · …", the 【任务 …】 / 【本次由工具调用触发】 framing, canned
-- tool_result strings) is not in the DB and needs a source patch + rebuild.
--
-- The agent prompts (agent_prompts.template_text) are intentionally NOT touched:
-- upstream keeps them in Chinese as the benchmarked "brain" (agent/outputlang.go).
--
-- Apply:  docker exec -i artex-global-postgres-1 psql -U artex -d artex -v ON_ERROR_STOP=1 < i18n-seed-labels-en.sql
BEGIN;

-- Intercept rule messages (shown under the rule on Intercept rules / Approvals,
-- and returned to the model when a rule denies a tool call).
UPDATE intercept_rules AS r SET message = t.en
FROM (VALUES
  ('禁止执行递归强制删除（rm -rf / rm --recursive），可能永久损坏系统或靶机环境',
   'Recursive force-delete (rm -rf / rm --recursive) is blocked: it can permanently damage the system or the target environment'),
  ('禁止删除系统关键路径',
   'Deleting critical system paths is blocked'),
  ('禁止格式化磁盘（mkfs）',
   'Formatting disks (mkfs) is blocked'),
  ('禁止使用 dd 覆写磁盘设备',
   'Overwriting disk devices with dd is blocked'),
  ('禁止执行 Fork 炸弹',
   'Fork bombs are blocked'),
  ('禁止执行关机或重启命令',
   'Shutdown and reboot commands are blocked'),
  ('禁止 kill -9 -1 或 killall -9（杀死所有进程）',
   'kill -9 -1 and killall -9 (killing all processes) are blocked'),
  ('禁止对磁盘设备执行 shred/wipe 擦除',
   'Erasing disk devices with shred/wipe is blocked'),
  ('禁止清空防火墙规则（iptables -F / nft flush）',
   'Flushing firewall rules (iptables -F / nft flush) is blocked'),
  ('禁止执行 DROP 操作，可能不可逆地销毁数据库对象',
   'DROP is blocked: it can irreversibly destroy database objects'),
  ('禁止执行 TRUNCATE，可能清空数据表所有数据',
   'TRUNCATE is blocked: it can wipe all data in a table'),
  ('禁止执行 MongoDB drop 操作',
   'MongoDB drop operations are blocked'),
  ('禁止执行 Redis FLUSHALL / FLUSHDB，可能清空全部缓存数据',
   'Redis FLUSHALL / FLUSHDB is blocked: it can wipe all cached data'),
  ('禁止通过 curl/wget 发送 HTTP DELETE 请求，可能删除目标系统数据',
   'Sending HTTP DELETE requests with curl/wget is blocked: it can delete data on the target system'),
  ('禁止使用 Python HTTP 客户端发送 DELETE 请求',
   'Sending DELETE requests with a Python HTTP client is blocked'),
  ('禁止在脚本中声明并发送 HTTP DELETE 请求',
   'Declaring and sending HTTP DELETE requests in scripts is blocked'),
  ('禁止调用批量清空或销毁类接口（/clear /wipe /flush /purge 等）',
   'Calling bulk-clear or destroy endpoints (/clear /wipe /flush /purge, etc.) is blocked'),
  ('破坏性命令被拒绝（rm -rf / / mkfs / dd / fork bomb / 关机重启 / 覆写磁盘设备）',
   'Destructive command denied (rm -rf / / mkfs / dd / fork bomb / shutdown-reboot / disk-device overwrite)'),
  ('疑似数据外泄管道被拒绝（命令输出经 curl/wget/nc 外传）',
   'Suspected data-exfiltration pipe denied (command output piped out via curl/wget/nc)'),
  ('禁止调用删除类接口（/delete /remove /unlink /erase 等），不论使用哪种 HTTP 方法——多数应用的删除接口用 GET/POST 就能触发，同样会真实删除目标数据',
   'Calling delete-type endpoints (/delete /remove /unlink /erase, etc.) is blocked regardless of HTTP method: most apps trigger deletes via GET/POST too, and they really delete target data')
) AS t(zh, en)
WHERE r.message = t.zh;

-- Historical approval records snapshot the rule message at decision time.
UPDATE intercept_pending AS p SET reason = r.message
FROM intercept_rules AS r
WHERE p.rule_id = r.id
  AND p.reason = '禁止调用删除类接口（/delete /remove /unlink /erase 等），不论使用哪种 HTTP 方法——多数应用的删除接口用 GET/POST 就能触发，同样会真实删除目标数据';

-- Prompt version note on System > Agents > prompt history.
UPDATE agent_prompts SET note = 'Built-in default' WHERE note = '内置默认';

-- Reporter trigger message: shown as the opening "user" message of every
-- "Report Writing" conversation, and it is the reporter's instruction. Every
-- tool name, argument name and the finding_id=finding_node_id mapping is kept
-- verbatim — only the prose is translated.
UPDATE agent_triggers SET tool_call_message =
  'A finding was just recorded via report_finding above. Read finding_id (the standalone finding record ID) and finding_node_id (the exploration node ID) from the returned JSON. First call get_finding_traffic(finding_id) to read the current evidence list and its version (an empty list is normal — write the report as usual); if the run guidance enables auto-binding, verify and bind this finding''s traffic before reading it. Use finding_node_id for node details. Finally call update_finding_report(finding_id=finding_node_id, report, evidence_version=<the version you actually read>) to save. evidence_version is mandatory, otherwise the report is permanently marked as pending update. Do not mix up the two IDs.'
WHERE agent_key = 'reporter'
  AND tool_call_message = '上面刚有一个漏洞被 report_finding 登记。请读取返回 JSON 的 finding_id（独立漏洞记录 ID）与 finding_node_id（探索节点 ID），先用 get_finding_traffic(finding_id) 读取当前证据清单及其 version（空清单是正常情况，照常写报告）；若运行指引启用自动绑定，在读取前先核实并关联本次漏洞的流量。节点详情使用 finding_node_id。最后调用 update_finding_report(finding_id=finding_node_id, report, evidence_version=实际读取版本) 保存，evidence_version 必须传，否则报告会被永久标记为待更新。不要混用两种编号。';

COMMIT;
