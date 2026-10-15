---
name: escalate
description: How and when to ask Claude for help through the budgeted escalation broker - the brief template, the sufficiency check, and the auto-escalation rules.
---

# Escalating to Claude

Claude is reached ONLY through the subagents `claude` (Sonnet, user-asked), `claude-hard` (Opus, user-asked) and
`claude-auto` (Sonnet, your own decision). Every call costs real money from a capped budget. The broker on the
router enforces the caps; never try to work around a refusal.

You may call `claude-auto` on your own, under the rules below. NEVER call `claude` or `claude-hard` unless the
user invoked /ask-claude or explicitly asked you to ask Claude in this turn.

## The brief: sufficient first, small second

Claude sees ONLY the brief: no repo, no transcript, no tools. A brief that leaves Claude guessing wastes the call.
Leave out irrelevant material, never relevant context. The cap is ~8K tokens for Sonnet, ~16K for Opus.

Use exactly these headings, copied from the template below. All are required; a section that truly does not
apply says `n/a: <why>`. Goal, Exact error and Question can never be n/a.

```
## Goal
1-3 sentences: what we are trying to achieve and why.
## Context
Repo purpose in one line; language, framework and the versions that matter; OS/runtime; constraints on the fix.
## Reproduce
The exact command(s) that show the problem; expected vs actual behaviour.
## Exact error
Verbatim: the full relevant traceback / compiler / test output, from the first cause to the failing line.
## Code
Every excerpt needed to reason about the failure, each headed `path:line-range`: the failing function, its
callers/callees on the failing path, the relevant types/config, and the failing test.
## What was tried
Bullets "attempt -> what happened", including hypotheses ruled out and why.
## Question
One specific question, and what a useful answer looks like (e.g. "root cause and a minimal diff").
```

Never paste the transcript or unrelated files. A whole file only when it is short and wholly relevant. Redact
anything that looks like a key, token or password.

## Sufficiency check (answer each; fix every "no" before sending)

1. Could a senior engineer who has never seen this repo reproduce or reason about the problem from this brief alone?
2. Is the full causal chain of the error present (the code on the failing path, not just the line that throws)?
3. Are versions and constraints stated wherever the fix could depend on them?
4. Is every attempted fix listed with its result?
5. Is the question answerable, and does it say what form of answer we need?

## When YOU may escalate on your own (subagent `claude-auto`)

Only if one of these holds:
- the same error survived 3 genuinely different fix attempts;
- you are looping (the same edit -> test cycle repeating);
- you need knowledge you could not find after trying the repo, context7 and searxng.

Never for something a search or reading the code would answer, never on a first failure, never for the same
problem twice, except ONE follow-up when Claude's answer named exactly what was missing, carrying exactly that.
If the sufficiency check fails and you cannot fix it within the cap, ask the user instead of sending.

## Sending and applying

Call the task tool with the subagent and the brief as the whole prompt. Read the answer, apply the fix yourself,
and run the verification it names. The answer ends with a budget footer; mention it to the user.

## When the broker refuses

422/413: fix the brief (missing section / too large) and resend; that costs nothing. 429 (budget or daily calls):
stop escalating. Continue without Claude where you can, otherwise tell the user you are stuck, what you tried,
and that the escalation budget is used for now.
