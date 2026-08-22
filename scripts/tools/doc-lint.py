#!/usr/bin/env python3
"""doc-lint.py — catch documentation drift that manual review keeps missing.

Three checks, each derived from a real defect found during the 2026-08-22
live-state reconciliation:

  1. CITATIONS   Every `path/to/file.sh:NN` reference in the docs is resolved
                 against the real file. Five of seven pre-existing citations
                 had rotted (LLAMA_CTX cited at :43 when it lives at :49,
                 ALIAS_MAP cited at :336-340 when it lives at :409-473, ...).
                 A citation that points at an unrelated line is worse than no
                 citation: it sends a reader to the wrong place while looking
                 authoritative.

  2. STALE       A blacklist of phrases that were true once and are not now.
                 Three rounds of manual sweeping still left "MCP-over-SSE" in
                 two scripts and a systemd unit Description, and
                 ALLM_LLM_TOKEN_LIMIT=131072 in bootstrap.sh.

  3. INVENTORY   Every scripts/NN-*.sh must appear in the scripts/README.md
                 phase table. 58-mcp-sdg.sh and 59-llamacpp-restart-timer.sh
                 were both absent -- 59 appeared in no document at all.

Usage:
    python3 scripts/tools/doc-lint.py [--repo PATH] [--quiet]

Exit status: 0 clean, 1 findings. Safe to gate CI on.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Phrases that must not reappear. Keep the reason attached -- a bare blacklist
# rots into cargo cult once nobody remembers why an entry is there.
STALE_STRINGS: list[tuple[str, str]] = [
    ("MCP-over-SSE", "the Memory Vault bridge is Streamable HTTP at /mcp"),
    ("Eight client-facing", "there are fourteen aliases across six profiles"),
    ("eight chat aliases", "there are fourteen aliases across six profiles"),
    ("three pre-defined profiles", "there are six profiles"),
    ("192.168.6.156:3005", "the Memory Vault bridge is at 192.168.6.223"),
    ("SearXNG (optional)", "no SearXNG LXC exists; .156 is an unrelated LAN device"),
    ("ALLM_LLM_TOKEN_LIMIT=131072", "the live value is 200000 (router cap)"),
    ("'mcp>=1.2'", "must be pinned 'mcp>=1.2,<2'; mcp 2.0.0 removed mcp.server.fastmcp"),
]

# Paths whose contents are historical records or vendored, not current-state
# claims, and so are exempt from the stale-string check.
STALE_EXEMPT_DIRS = ("docs/superpowers/", ".superpowers/", "docs/archive/", "LESSONS.md")

# An author can mark a line as a deliberate historical quote:
#     ... installed 'mcp>=1.2' with no upper bound   <!-- doc-lint: allow -->
ALLOW_RE = re.compile(r"doc-lint:\s*allow")

CITATION_RE = re.compile(r"([0-9A-Za-z_./-]+\.(?:sh|py)):(\d+)")
# Identifier-ish tokens we can look for in the citing paragraph.
TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_]{3,}|[a-z_][a-z0-9_]{4,}")
CONTEXT_CHARS = 300


def docs_under(repo: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(repo.rglob("*.md")):
        rel = p.relative_to(repo).as_posix()
        if any(seg in rel for seg in ("node_modules/", "__pycache__/")):
            continue
        out.append(p)
    return out


def resolve(repo: Path, cited: str) -> Path | None:
    """Map a citation path to a real file, trying the documented shorthands."""
    name = Path(cited).name
    for cand in (repo / cited, repo / "scripts" / name, repo / "scripts" / "files" / name,
                 repo / "scripts" / "rag" / name, repo / "scripts" / "tools" / name):
        if cand.is_file():
            return cand
    return None


def check_citations(repo: Path) -> list[str]:
    findings: list[str] = []
    for doc in docs_under(repo):
        rel = doc.relative_to(repo).as_posix()
        if any(rel.startswith(d) or rel == d for d in STALE_EXEMPT_DIRS):
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for m in CITATION_RE.finditer(text):
            cited, lineno = m.group(1), int(m.group(2))
            target = resolve(repo, cited)
            if target is None:
                continue  # not a repo file (URLs, external refs)
            lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
            if lineno > len(lines):
                findings.append(
                    f"{rel}: cites {cited}:{lineno} but that file has only "
                    f"{len(lines)} lines")
                continue
            line = lines[lineno - 1]
            if not line.strip():
                findings.append(
                    f"{rel}: cites {cited}:{lineno}, which is a blank line")
                continue
            # Look both ways: docs cite before the identifier ("see X.sh:49
            # LLAMA_CTX") as often as after.
            ctx = text[max(0, m.start() - CONTEXT_CHARS):
                       min(len(text), m.end() + CONTEXT_CHARS)]
            tokens = set(TOKEN_RE.findall(line))
            if tokens and not any(t in ctx for t in tokens):
                findings.append(
                    f"{rel}: cites {cited}:{lineno} -> {line.strip()[:70]!r}\n"
                    f"    nothing in the citing text matches that line; "
                    f"the reference may have rotted")
    return findings


def check_stale(repo: Path) -> list[str]:
    findings: list[str] = []
    targets = list(docs_under(repo))
    for pat in ("scripts/**/*.sh", "scripts/**/*.py", "scripts/**/*.yaml",
                "scripts/**/*.example"):
        targets.extend(sorted(repo.glob(pat)))
    for f in targets:
        rel = f.relative_to(repo).as_posix()
        if any(rel.startswith(d) or rel == d for d in STALE_EXEMPT_DIRS):
            continue
        if "__pycache__" in rel or "/tests/" in rel:
            continue
        if rel == "scripts/tools/doc-lint.py":
            continue  # this file necessarily contains every blacklisted string
        text = f.read_text(encoding="utf-8", errors="replace")
        for needle, why in STALE_STRINGS:
            if needle not in text:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                if needle in line and ALLOW_RE.search(line) is None:
                    findings.append(f"{rel}:{i}: stale {needle!r} - {why}")
    return findings


def check_inventory(repo: Path) -> list[str]:
    readme = repo / "scripts" / "README.md"
    if not readme.is_file():
        return ["scripts/README.md is missing; cannot check phase inventory"]
    text = readme.read_text(encoding="utf-8", errors="replace")
    findings = []
    for script in sorted((repo / "scripts").glob("[0-9][0-9]-*.sh")):
        if script.name not in text:
            findings.append(
                f"scripts/{script.name} is not listed in the "
                f"scripts/README.md phase table")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--quiet", action="store_true",
                    help="only print findings, not the per-check headers")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    total = 0
    for title, fn in (("citations", check_citations),
                      ("stale strings", check_stale),
                      ("script inventory", check_inventory)):
        found = fn(repo)
        total += len(found)
        if not args.quiet:
            status = "OK" if not found else f"{len(found)} finding(s)"
            print(f"== {title}: {status}")
        for f in found:
            print(f"  {f}")

    if not args.quiet:
        print()
        print("clean" if total == 0 else f"{total} finding(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
