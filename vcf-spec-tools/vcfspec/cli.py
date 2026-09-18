"""Command line: vcfspec validate|render <file>.

stdout carries the JSON result only, and nothing else -- so
`vcfspec validate x.yaml | jq` works. Everything human-oriented (usage
errors, internal-error detail) goes to stderr instead.

Exit codes are the scripting contract:
  0 -- the document is valid.
  1 -- the document is invalid (a structured finding explains why), OR an
       exception escaped the layers below this CLI. api.py promises that
       never happens, but this is the last line of defence, and it must
       never let a traceback reach the operator.
  2 -- usage error: bad arguments, or a path that cannot be read as a file
       (missing, a directory, unreadable). This is deliberately distinct
       from "1": a file that exists and parses but is malformed VCF input
       is not a usage error -- api.py already turns that into a finding,
       so it exits 1 like any other invalid document.

Probe containment is enforced here, not just in validate.probes: --probe
with no --allowlist is refused as a usage error rather than silently
running zero probes. An empty ProbeConfig.allowlist permits nothing, so
that combination would otherwise look like a clean pass while checking
nothing -- an operator who asked for probes and got none deserves an
error, not misleading output.

Note what that check can and cannot do. Non-emptiness is an argument
property; "this allowlist matches something in this document" is not,
because it depends on the document. A non-empty allowlist matching none of
the hosts (`--allowlist 203.0.113.0/24` against a 10.50.10.0/24 lab) used
to produce the identical misleading pass. That case is caught where it is
actually knowable, in run_probes, as the blocking finding
VCF-PROBE-NOTHING-PERMITTED -- which also covers every caller of
run_probes, not only the one with argv.

--allowlist-domain is the name-side equivalent of --allowlist, and it is
not optional in effect: probes fail closed without it, issuing no forward
DNS lookups at all. That is deliberate rather than a usage error, because
the reverse lookup and the TCP check still run and are still useful, and
the skipped forward lookups are each reported as VCF-PROBE-NAME-BLOCKED
rather than silently omitted.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path

from .api import render_document, validate_document
from .schema import DEFAULT_VERSION
from .validate.probes import ProbeConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vcfspec")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate an inventory or spec document.")
    validate.add_argument("path", type=Path)
    validate.add_argument("--version", default=DEFAULT_VERSION,
                          help="VCF schema version to validate SDDC specs against.")
    validate.add_argument(
        "--probe", action="store_true",
        help="Run live network probes against hosts. Requires --allowlist.")
    validate.add_argument(
        "--allowlist", action="append", default=None, metavar="CIDR",
        help="CIDR probes are permitted to touch; repeatable. Required "
             "with --probe -- an empty allowlist permits nothing.")
    validate.add_argument(
        "--allowlist-domain", action="append", default=None, metavar="SUFFIX",
        help="DNS suffix a forward lookup is permitted to resolve under; "
             "repeatable. Without it no forward lookup is issued at all -- "
             "an IP allowlist cannot gate a name, because the query is "
             "itself the exfiltration channel.")
    validate.add_argument("--probe-timeout", type=float, default=2.0,
                          help="Per-probe socket timeout in seconds.")

    render = sub.add_parser("render", help="Render an inventory into a VCF SDDC spec.")
    render.add_argument("path", type=Path)
    render.add_argument("--version", default=DEFAULT_VERSION,
                        help="VCF schema version to render for.")

    return parser


def _invalid_cidrs(cidrs: list[str]) -> list[str]:
    """Entries that ProbeConfig.permits() could never match -- including ""
    and whitespace, which ipaddress.ip_network() already rejects."""
    invalid = []
    for cidr in cidrs:
        try:
            ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError:
            invalid.append(cidr)
    return invalid


def _read_document(path: Path) -> tuple[str | None, int | None]:
    """Read `path` as UTF-8 text. Returns (text, None) or (None, exit_code).

    Every way a path can fail to be a readable file -- missing, a
    directory, permission-denied -- is a usage error (2), never treated as
    an invalid document (1). That distinction only starts once we have
    text to hand to validate_document/render_document.
    """
    if not path.exists():
        print(f"cannot read {path}: no such file", file=sys.stderr)
        return None, 2
    if path.is_dir():
        print(f"cannot read {path}: is a directory", file=sys.stderr)
        return None, 2
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return None, 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # main(argv) -> int is a stated contract for programmatic callers
        # (e.g. an MCP server driving this CLI in-process); argparse's own
        # usage-error path must not surface as an uncaught exception to
        # them. Shell/CI behaviour is unchanged either way: SystemExit(2)
        # still yields process exit 2 when __main__ re-raises it below.
        return exc.code if isinstance(exc.code, int) else 2

    if args.command == "validate" and args.probe:
        if not args.allowlist:
            print("vcfspec: --probe requires at least one --allowlist CIDR "
                  "(an empty allowlist would silently probe nothing)",
                  file=sys.stderr)
            return 2
        invalid = _invalid_cidrs(args.allowlist)
        if invalid:
            # A partially-bad allowlist (some valid CIDRs, one typo) is
            # refused wholesale rather than silently probing only the
            # entries that happened to parse: an operator who typed 5
            # ranges and gets checks against 4 has been told less than
            # they asked for without being told anything went wrong.
            print("vcfspec: --allowlist has invalid CIDR(s): "
                  f"{', '.join(invalid)}", file=sys.stderr)
            return 2

    text, error_code = _read_document(args.path)
    if error_code is not None:
        return error_code

    try:
        if args.command == "validate":
            probe_config = None
            if args.probe:
                probe_config = ProbeConfig(
                    allowlist=tuple(args.allowlist),
                    timeout_s=args.probe_timeout,
                    domain_allowlist=tuple(args.allowlist_domain or ()))
            result = validate_document(text, probe_config=probe_config,
                                       version=args.version)
        else:
            result = render_document(text, version=args.version)
    except Exception as exc:
        # api.py promises no exception ever reaches this point; this is
        # the last line of defence if that promise is ever broken. Only
        # the exception's type name goes to stderr/stdout -- never
        # str(exc), which could echo arbitrary spec content (including a
        # secret) that no layer below has had a chance to redact.
        print(f"vcfspec: internal error ({type(exc).__name__}) while "
              f"running {args.command}", file=sys.stderr)
        json.dump({"valid": False, "findings": [], "layers_run": [],
                  "layers_skipped": {},
                  "error": f"internal error: {type(exc).__name__}"},
                 sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
