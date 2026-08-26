"""Rewrite the chat unit's speculative-decoding argument block.

Line-based on purpose. An earlier version matched a two-line string containing
backslash-newline continuations; that literal cannot survive being typed through
nested heredocs over ssh (one backslash was silently dropped, so the pattern
became a literal 'n' and never matched). Here no pattern contains a backslash:
lines are identified by flag name and the trailing continuation is re-emitted.

Usage: apply_spec.py <unit> "<arg>" ["<arg>" ...]
Removes every existing --spec-* line and inserts the given args before
--no-mmproj, preserving the unit's 4-space continuation indent.
"""
import io
import sys

BS = chr(92)
DROP = ("--spec-type", "--spec-draft-n-max", "--spec-draft-n-min",
        "--spec-draft-p-min", "--spec-ngram")
ANCHOR = "--no-mmproj"


def main():
    unit, new_args = sys.argv[1], sys.argv[2:]
    lines = io.open(unit, encoding="utf-8").read().split("\n")
    out, inserted = [], False
    for ln in lines:
        if any(k in ln for k in DROP):
            continue
        if ANCHOR in ln and not inserted:
            out.extend("    " + a + " " + BS for a in new_args)
            inserted = True
        out.append(ln)
    if not inserted:
        sys.exit("apply_spec: anchor %s not found in %s" % (ANCHOR, unit))
    io.open(unit, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    print("      applied %d spec arg(s)" % len(new_args))


if __name__ == "__main__":
    main()
