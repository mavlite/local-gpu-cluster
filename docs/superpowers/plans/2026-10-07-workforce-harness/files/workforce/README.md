# Workforce harness

Implements the harness of spec `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md`
(§5.1 workspaces, §5.5 write-back and grading, §6 review protocol, §7 loop metric and W1 rule,
§9 W3 rule). Plan: `docs/superpowers/plans/2026-10-07-workforce-harness.md` (Plan C of 4). The
sandbox VM (Plan B) runs `cli.py run`; the workstation builds bundles and imports patches.

## Pieces
| Runs on | File | Purpose |
|---|---|---|
| workstation | `bundle.py` | task bundle from a commit: stripped parent snapshot + visible tests, answer-leak check, reference patch (kept off the VM), validation, manifest |
| VM | `workspace.py` | unpack a snapshot; baseline git dir **outside** the workspace; raw changes; binary patch |
| VM | `paths.py` | which changed paths may be exported (declared, not protected, regular files) |
| VM | `profiles.py` | read-only opencode config (agents `impl-N`, `reviewer`, `fixer`) + lock-down env |
| VM | `oc.py` | `opencode run` headless: argv, session id, final text, whole-tree kill on timeout, export |
| VM | `review.py` | messages, review packet (kept under opencode's 2000-line attachment cap), verdict parse |
| VM | `pipeline.py` | scheduling, review rounds, rework affinity, lead fix, export, grade, run record; worker health monitor |
| VM | `grade.py` | grading tree from the pristine snapshot + filtered patch + restored visible tests + hidden tests; local or network-less Docker runner |
| both | `loops.py` | repeat/cycle loop episodes and steps per turn from `opencode export` |
| workstation | `analysis.py` | W1 choice (one-sided Clopper-Pearson) and W3 decision (paired bootstrap, Welch CI, user probe) |
| both | `cli.py` | `bundle-build`, `bundle-validate`, `manifest`, `run`, `import`, `w1`, `w3` |

## Footguns (each found by running, opencode 1.18.34)
- Without `OPENCODE_DISABLE_PROJECT_CONFIG=1`, a workspace `.opencode/agent/<name>.md` **replaces
  the agent's prompt** and a workspace `opencode.json` grants tools (webfetch was offered). Every
  review round starts a new opencode process, so an agent could rewrite its own permissions. The
  end-to-end test plants both and fails if either loads.
- A reviewer with `edit: deny` but bash allowed **can still change files** (`echo > x`): reviews
  run on a disposable copy that is deleted afterwards.
- `--file` is an array option and swallows anything after it: the message goes first.
- Attached files reach the model capped at **2000 lines**; packets are cut at 1900 with a pointer to
  the full packet inside the reviewer's copy.
- `opencode run` waits for EOF on a non-TTY stdin: always `stdin=DEVNULL`. A timeout must kill the
  whole process tree.
- Sessions live in `opencode.db` (SQLite); `opencode export <id>` prints a status line, then JSON.
- A grader that cannot find pytest reports "failed" too: a test asserts the failure summary is a
  real test failure (`2 failed`), not `No module named pytest`.

## Tests
`python3 -m pytest scripts/tools/workforce/tests -q` (99 tests: 98 run on Windows, where the POSIX permission test skips; 98 on Linux, where the opencode end-to-end test skips; also green on Python 3.12.3).
`test_e2e_opencode.py` drives the real opencode binary against a scripted model server and skips
when opencode is not installed (`WF_OPENCODE` forces a path). Docker grading is only argv-tested
here; Plan B verifies it on the VM.
