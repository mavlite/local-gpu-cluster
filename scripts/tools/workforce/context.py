"""Packet context for the reviewer (round 2 §3.2): computed from the pristine snapshot and git blobs
of in-scope files, never from a tree agent code has run in. The reviewer used two thirds of its
steps re-reading files and grepping for tests and callers; this hands it those up front."""
import re
import tarfile

_DEF = re.compile(r"^([ +-])(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")   # top level: no indent
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@.*$")
MAX_SITES = 40


def changed_names(diff_text):
    """Top-level def/class names a hunk adds or changes, in order of first appearance: every added
    def/class line, plus the def/class a hunk opens on (with -W context, the function whose body
    changed). Nested defs are indented and never match."""
    out, first_in_hunk = [], True
    for line in diff_text.split("\n"):
        if line.startswith("@@"):
            first_in_hunk = True
            continue
        if line.startswith("diff --git"):
            first_in_hunk = False
            continue
        m = _DEF.match(line)
        if m and (m.group(1) == "+" or first_in_hunk) and m.group(2) not in out:
            out.append(m.group(2))
        if m:
            first_in_hunk = False
    return out


def _snapshot_files(snapshot_tar, suffixes):
    with tarfile.open(snapshot_tar) as t:
        for m in t.getmembers():
            if m.isfile() and m.name.endswith(suffixes):
                yield m.name, t.extractfile(m).read()


def call_sites(names, snapshot_tar, patched, allowed_suffixes=(".py",)):
    """`path:line: <source>` for every word-boundary use of each name across the snapshot's source
    files, with `patched` (path -> bytes, the in-scope files after the change) replacing their
    snapshot copies. Definitions themselves are excluded; at most MAX_SITES lines per name."""
    files = dict(_snapshot_files(snapshot_tar, allowed_suffixes))
    files.update({p: b for p, b in patched.items() if p.endswith(allowed_suffixes)})
    out = []
    for name in names:
        use = re.compile(r"\b" + re.escape(name) + r"\b")
        is_def = re.compile(r"^\s*(def|class)\s+" + re.escape(name) + r"\b")
        n = 0
        for path in sorted(files):
            for i, line in enumerate(files[path].decode("utf-8", "replace").splitlines(), 1):
                if use.search(line) and not is_def.match(line):
                    out.append(f"{path}:{i}: {line.strip()}")
                    n += 1
                    if n >= MAX_SITES:
                        break
            if n >= MAX_SITES:
                break
    return out


def touching_tests(changed_paths, snapshot_tar):
    """Pristine test files whose text names a changed file's stem or basename. The repo's tests load
    modules by sys.path inserts and spec_from_file_location, so an import match would miss them."""
    keys = set()
    for p in changed_paths:
        base = p.rsplit("/", 1)[-1]
        keys.add(base)
        keys.add(base.rsplit(".", 1)[0])
    out = []
    for name, data in _snapshot_files(snapshot_tar, (".py",)):
        base = name.rsplit("/", 1)[-1]
        if "/tests/" not in f"/{name}" and not base.startswith("test_"):
            continue
        text = data.decode("utf-8", "replace")
        if any(k in text for k in keys):
            out.append(name)
    return sorted(out)


def shrink_blocks(diff_text, max_lines=60):
    """Trim each hunk's context to ±max_lines around its first/last changed line; unchanged hunks
    pass through byte for byte. A trimmed hunk ends with `[context trimmed: file:start-end]`."""
    lines = diff_text.split("\n")                      # never splitlines(): \x0c etc. are not line ends
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()
    out, file, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git"):
            file = line.split(" b/", 1)[-1]
        m = _HUNK.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not (lines[j].startswith("@@") or lines[j].startswith("diff --git")):
            j += 1
        body = lines[i + 1:j]
        changed = [k for k, l in enumerate(body) if l[:1] in ("+", "-")]
        if not changed or (changed[0] <= max_lines and len(body) - 1 - changed[-1] <= max_lines):
            out.append(line)
            out.extend(body)
        else:
            lo, hi = max(0, changed[0] - max_lines), min(len(body), changed[-1] + max_lines + 1)
            start = int(m.group(2))
            out.append(line)
            out.extend(body[lo:hi])
            out.append(f"[context trimmed: {file}:{start}-{start + len(body)}]")
        i = j
    return "\n".join(out) + ("\n" if trailing else "")
