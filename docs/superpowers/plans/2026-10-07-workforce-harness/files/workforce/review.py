"""Review protocol: messages to the agents and the reviewer's verdict (workforce spec §6)."""
import re

MAX_ATTACH_LINES = 1900          # opencode passes at most 2000 lines of an attached file
MAX_FEEDBACK = 8000
FULL_PACKET = ".workforce_review/packet.md"
_VERDICT = re.compile(r"^[\s*_#>`]*(ACCEPT|REVISE)(?![A-Za-z])[\s*_`]*:?\s*(.*)$")


def test_command(task):
    return "python3 -m pytest -q " + " ".join(task.get("pytest_args") or task["tests"])


def parse_verdict(text):
    """("ACCEPT", "") | ("REVISE", feedback) | ("NONE", text) from the reviewer's final text."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        m = _VERDICT.match(line)
        if m:
            if m.group(1) == "ACCEPT":
                return "ACCEPT", ""
            rest = "\n".join([m.group(2)] + lines[i + 1:]).strip()
            return "REVISE", rest
    return "NONE", (text or "").strip()


def implement_message(task, request_text):
    return (f"{request_text.strip()}\n\n"
            f"Files in scope (only changes to these are kept): {', '.join(task['files'])}\n"
            f"Acceptance tests: {test_command(task)}\n")


def packet(task, request_text, diff, test_output, history=()):
    """(text, truncated). Kept under MAX_ATTACH_LINES; when cut, the last line points the reader at
    the full packet, which the caller writes to FULL_PACKET in the reviewer's directory."""
    parts = ["# Request", request_text.strip(), "",
             f"Files in scope: {', '.join(task['files'])}", f"Acceptance tests: {test_command(task)}", ""]
    if history:
        parts += ["# Review feedback so far", *history, ""]
    parts += ["# Test output", test_output.strip(), "", "# Diff", diff.rstrip()]
    lines = "\n".join(parts).splitlines()
    if len(lines) <= MAX_ATTACH_LINES:
        return "\n".join(lines) + "\n", False
    return "\n".join(lines[:MAX_ATTACH_LINES] + [
        f"[packet truncated at {MAX_ATTACH_LINES} of {len(lines)} lines; the full packet is at "
        f"{FULL_PACKET}]"]) + "\n", True


def revise_message(feedback):
    fb = feedback.strip()
    if len(fb) > MAX_FEEDBACK:
        fb = fb[:MAX_FEEDBACK] + "\n[feedback truncated]"
    return f"REVISE: {fb}\n\nAddress this review, then re-run the acceptance tests."
