"""github_repo handler.

Clones (or pulls) a git repo, walks files matching file_glob, and yields
a Document for each — with the citation URL computed from the file path
according to the source's URL transform rules. Mirrors the behavior of
scripts/tools/ingest-github-repo.sh.

Config keys consumed:
  repo                   git URL to clone
  file_glob              glob pattern, e.g. "*.rst" or "*.adoc"
  path_strip             leading path components to strip when forming URL,
                         e.g. "source/" for sphinx, "content/" for hugo
  rendered_base          base URL of the rendered site
  url_ext_from           file extension to strip when building URL ("*.rst")
  url_ext_to             URL extension to append (".html", "/")
  url_keep_depth         when set to N, keep only first N path components
                         after path_strip (for "many .adoc → one HTML page"
                         patterns like Keycloak)
  url_lowercase          true to lowercase URL path (Hugo)
  url_encode_spaces      true to %20-encode spaces in paths (OpenZFS)
  file_exclude_regex     regex matched against path-stripped path; exclude
                         matches (filter list before yielding)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

from .base import Document, Handler, HandlerContext


class GitHubRepoHandler(Handler):
    name = "github_repo"

    def collect(
        self,
        config: dict[str, Any],
        context: HandlerContext,
    ) -> Iterator[Document]:
        repo_url: str = config["repo"]
        file_glob: str = config.get("file_glob", "*.rst")
        path_strip: str = config.get("path_strip", "").lstrip("/")
        rendered_base: str = config["rendered_base"].rstrip("/")
        url_ext_from: str = config.get("url_ext_from", ".rst")
        url_ext_to: str = config.get("url_ext_to", ".html")
        url_keep_depth: int | None = config.get("url_keep_depth")
        url_lowercase: bool = bool(config.get("url_lowercase", False))
        url_encode_spaces: bool = bool(config.get("url_encode_spaces", False))
        file_exclude_re: re.Pattern | None = (
            re.compile(config["file_exclude_regex"])
            if config.get("file_exclude_regex")
            else None
        )

        clone_dir = self._clone_or_pull(repo_url, context.cache_dir)

        # Two-pass: gather per file, then emit one Document per unique citation URL.
        # Why dedupe: when url_keep_depth collapses many source files to one
        # rendered page (Keycloak: ~399 .adoc partials → ~7 guide URLs), the
        # plan layer treats each collision as a separate ADD and AnythingLLM
        # ends up with N copies per URL, but state can only remember the last
        # one's docpath — so future refreshes can't delete the orphans.
        # Use git ls-files so .gitignore is respected and order is stable.
        by_url: dict[str, dict] = {}
        for rel_path in self._list_files(clone_dir, file_glob):
            stripped = self._strip_prefix(rel_path, path_strip)
            if stripped is None:
                continue
            if file_exclude_re and file_exclude_re.search(rel_path):
                continue

            abs_path = clone_dir / rel_path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                # Skip unreadable files; let refresh.py log the URL as an error.
                continue
            if not content.strip():
                continue

            citation_url = self._build_url(
                stripped,
                rendered_base,
                url_ext_from,
                url_ext_to,
                url_keep_depth,
                url_lowercase,
                url_encode_spaces,
            )

            last_modified = self._git_file_last_modified(clone_dir, rel_path)

            entry = by_url.setdefault(
                citation_url,
                {"rel_paths": [], "contents": [], "last_modifieds": [], "first_stripped": stripped},
            )
            entry["rel_paths"].append(rel_path)
            entry["contents"].append(content)
            entry["last_modifieds"].append(last_modified)

        for citation_url, entry in by_url.items():
            last_modified = max(
                (lm for lm in entry["last_modifieds"] if lm), default=""
            )
            rel_paths = entry["rel_paths"]

            if len(entry["contents"]) == 1:
                body = entry["contents"][0]
            else:
                # Multiple source files collapsed to one URL. Concatenate in
                # git ls-files order (lexicographic, deterministic) so the
                # content hash is stable across refreshes.
                body = "\n\n---\n\n".join(entry["contents"])

            # Prepend a small provenance header so the URL/date survive
            # AnythingLLM's metadata stripping at chunk write.
            text = (
                f"Source: {citation_url}\n"
                f"URL: {citation_url}\n"
                f"Last-modified: {last_modified}\n\n"
                f"{body}"
            )

            title = entry["first_stripped"].rstrip("/").replace("/", " / ")

            metadata: dict[str, Any] = {
                "last_modified": last_modified,
                "repo": repo_url,
                "repo_path": rel_paths[0] if len(rel_paths) == 1 else rel_paths,
            }
            if len(rel_paths) > 1:
                metadata["merged_count"] = len(rel_paths)

            yield Document(
                url=citation_url,
                content=text,
                title=title,
                metadata=metadata,
            )

    # ─── helpers ──────────────────────────────────────────────────────────
    def _clone_or_pull(self, repo_url: str, cache_dir: Path) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Derive a stable dir name from the repo URL last segment.
        name = repo_url.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[: -len(".git")]
        clone_dir = cache_dir / name

        if (clone_dir / ".git").exists():
            # Existing clone: fetch + reset to origin/HEAD. This handles
            # force-pushes and branch renames without manual intervention.
            subprocess.run(
                ["git", "-C", str(clone_dir), "fetch", "--prune", "--quiet"],
                check=True,
            )
            # Determine default branch from the remote HEAD ref.
            result = subprocess.run(
                ["git", "-C", str(clone_dir), "symbolic-ref",
                 "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                default_ref = result.stdout.strip()  # e.g., refs/remotes/origin/main
                subprocess.run(
                    ["git", "-C", str(clone_dir), "reset", "--hard",
                     default_ref, "--quiet"],
                    check=True,
                )
            else:
                # Fallback: just pull on the current branch.
                subprocess.run(
                    ["git", "-C", str(clone_dir), "pull", "--ff-only", "--quiet"],
                    check=True,
                )
        else:
            # Fresh clone. Shallow is fine since we only ever want HEAD;
            # depth=1 saves bandwidth on large repos like keycloak.
            if clone_dir.exists():
                shutil.rmtree(clone_dir)
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", repo_url, str(clone_dir)],
                check=True,
            )

        return clone_dir

    def _list_files(self, clone_dir: Path, glob: str) -> list[str]:
        """Use git ls-files for tracked-only listing with -- pathspec."""
        result = subprocess.run(
            ["git", "-C", str(clone_dir), "ls-files", "--", f"**/{glob}", glob],
            capture_output=True, text=True, check=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def _strip_prefix(self, rel_path: str, prefix: str) -> str | None:
        if not prefix:
            return rel_path
        if rel_path.startswith(prefix):
            return rel_path[len(prefix):]
        return None  # path doesn't fall under path_strip → skip

    def _build_url(
        self,
        stripped: str,
        base: str,
        ext_from: str,
        ext_to: str,
        keep_depth: int | None,
        lowercase: bool,
        encode_spaces: bool,
    ) -> str:
        # Strip the source extension first.
        if ext_from and stripped.endswith(ext_from):
            stripped = stripped[: -len(ext_from)]

        # Keycloak-style collapse: many .adoc files → one /guide/ URL.
        if keep_depth is not None and keep_depth > 0:
            parts = stripped.split("/")
            stripped = "/".join(parts[:keep_depth])

        # Append the target URL extension.
        if ext_to:
            if ext_to == "/" and not stripped.endswith("/"):
                stripped = stripped + "/"
            elif ext_to != "/":
                stripped = stripped + ext_to

        if lowercase:
            stripped = stripped.lower()

        if encode_spaces:
            # urllib.parse.quote with safe="/" preserves path separators while
            # encoding spaces (and other unsafe characters that should be).
            stripped = urllib.parse.quote(stripped, safe="/")

        return f"{base}/{stripped.lstrip('/')}"

    def _git_file_last_modified(self, clone_dir: Path, rel_path: str) -> str:
        """ISO date of the most recent commit touching rel_path."""
        result = subprocess.run(
            ["git", "-C", str(clone_dir), "log", "-1",
             "--format=%cI", "--", rel_path],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() or ""
