"""install_opencode.py -- add the Claude escalation pieces to the user's opencode setup (spec 2026-10-15 sec 3.2):
router models claude-ask/-auto/-hard, tool-less subagents claude/claude-hard/claude-auto, the /ask-claude and
/claude-budget commands, the `escalate` skill, the budget printer and two RULES.md lines. Backs up config.json
first; idempotent; never touches auth.json or any key."""
import copy
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_MARKER = "<!-- claude-escalation -->"
RULES_LINES = (
    f"\n{RULES_MARKER}\n"
    "- Stuck? Follow the `escalate` skill. Claude is reached only through the `claude`, `claude-hard` and "
    "`claude-auto` subagents, never any other way.\n"
    "- If the escalation broker refuses (budget or daily calls), do not retry or work around it: continue "
    "without Claude, or tell the user you are stuck and what you tried.\n"
)
RELAY_PROMPT = "Pass the brief through unchanged. Return the answer verbatim."
NO_TOOLS = {"*": False}
ASK_TEMPLATE = (
    "The user wants to ask Claude for help with what we are stuck on. Arguments: $ARGUMENTS\n"
    "1. Load the `escalate` skill and write the brief from this session using its template exactly. If the "
    "arguments contain a note, include it where it belongs.\n"
    "2. Run the sufficiency check and fix every 'no'.\n"
    "3. Show the user the brief, its estimated size (bytes/3 tokens) and your checklist answers, then STOP and "
    "wait. Send only after the user replies 'send' (they may add context first).\n"
    "4. On 'send': call the task tool with subagent `claude-hard` if the arguments start with 'hard', otherwise "
    "`claude`, with the brief as the whole prompt. Then apply the answer and run the verification it names."
)
BUDGET_TEMPLATE = ("Run this command with the bash tool and show its output verbatim, nothing else: "
                   "python ~/.config/opencode/bin/claude_budget.py")


def merge_config(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    models = out.setdefault("provider", {}).setdefault("router", {}).setdefault("models", {})
    for alias, cap in (("claude-ask", 2048), ("claude-auto", 2048), ("claude-hard", 4096)):
        models[alias] = {"name": f"Claude via escalation broker ({alias})", "tools": False,
                         "limit": {"context": 16384, "output": cap}}
    agents = out.setdefault("agent", {})
    for name, alias, desc in (("claude", "claude-ask", "Claude Sonnet (user-asked); send a brief from the escalate skill"),
                              ("claude-hard", "claude-hard", "Claude Opus (user-asked, hard problems only)"),
                              ("claude-auto", "claude-auto", "Claude Sonnet for self-escalation under the escalate skill's rules")):
        agents[name] = {"mode": "subagent", "model": f"router/{alias}", "description": desc,
                        "prompt": RELAY_PROMPT, "tools": dict(NO_TOOLS)}
    commands = out.setdefault("command", {})
    commands["ask-claude"] = {"description": "Ask Claude for help (Sonnet; 'hard' for Opus)", "template": ASK_TEMPLATE}
    commands["claude-budget"] = {"description": "Show the Claude escalation budget", "template": BUDGET_TEMPLATE}
    return out


def ensure_rules(text: str) -> str:
    return text if RULES_MARKER in text else text.rstrip("\n") + "\n" + RULES_LINES


def main(home: str) -> int:
    oc = os.path.join(home, ".config", "opencode")
    cfg_path = os.path.join(oc, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    shutil.copy2(cfg_path, f"{cfg_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-escalation")
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merge_config(cfg), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.makedirs(os.path.join(oc, "skills", "escalate"), exist_ok=True)
    shutil.copy2(os.path.join(HERE, "skill", "SKILL.md"), os.path.join(oc, "skills", "escalate", "SKILL.md"))
    os.makedirs(os.path.join(oc, "bin"), exist_ok=True)
    shutil.copy2(os.path.join(HERE, "claude_budget.py"), os.path.join(oc, "bin", "claude_budget.py"))
    rules = os.path.join(oc, "RULES.md")
    text = open(rules, encoding="utf-8").read() if os.path.exists(rules) else ""
    with open(rules, "w", encoding="utf-8", newline="\n") as f:
        f.write(ensure_rules(text))
    print(f"installed into {oc} (config backed up)")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.expanduser("~")))
