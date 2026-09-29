#!/usr/bin/env python3
"""
Offline-depot publisher for VCF 9.x components.

The depot's own metadata is the authority for everything here. Both the file
list and the SHA-256 of every file come from
``PROD/metadata/productVersionCatalog/v1/productVersionCatalog.json``, and the
component/version pairing comes from
``PROD/metadata/manifest/v1/vcfManifest.json``. Nothing is hard-coded per
product, so a component this tool has never seen works the same way.

Commands
--------
audit    Compare a release's whole BOM against what the depot actually serves.
plan     Show exactly which files a component needs and where each one stands.
publish  Verify staged files against the catalogue and copy them into the depot.

Publishing is a dry run unless ``--apply`` is given, never overwrites a file
that is already present at the right size, and refuses any file whose SHA-256
does not match the catalogue.

Paths
-----
The depot is served over HTTP but written over a filesystem path, and the two
do not line up: the web root is the share's ``PROD`` directory, so a file at
HTTP ``/PROD/COMP/VRLI/x`` is written to ``<share>/PROD/PROD/COMP/VRLI/x``.
``--depot-root`` is the share root; the doubling is handled here.

Stdlib only, to match the other operational tooling in this repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://172.16.10.50:8080"
DEFAULT_ROOT = r"\\truenas.knowledgeondemand.net\VCF-Offline-Depot"
DEFAULT_RELEASE = "9.1.1.0"

MANIFEST = "/PROD/metadata/manifest/v1/vcfManifest.json"
CATALOG = "/PROD/metadata/productVersionCatalog/v1/productVersionCatalog.json"


# --------------------------------------------------------------------- http --

def fetch_json(base: str, path: str):
    """
    Fetch one of the depot's metadata documents.

    Every command starts here, so a wrong --base or a depot that is down should
    say so in one line rather than unwinding a urllib traceback over the
    operator.
    """
    url = base + path
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"depot returned HTTP {e.code} for {url}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach the depot at {url}: {e.reason}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{url} is not valid JSON: {e}")


class Probe:
    """
    Outcome of one HEAD against the depot.

    Three states, kept distinct on purpose. An earlier version returned a
    single int -- size, or a negative HTTP status, or -1 for anything else --
    which made a transient blip indistinguishable from a real 404. In a
    65-component audit that is hundreds of HEADs, and one reset connection
    would report an already-complete component as missing, inflating the
    "still to download" figure with files that were there all along.

    state == "ok"      size is the served byte count
    state == "absent"  the depot answered, and said no: status is the code
    state == "error"   we could not get an answer: detail says why
    """
    __slots__ = ("state", "size", "status", "detail")

    def __init__(self, state, size=None, status=None, detail=""):
        self.state, self.size, self.status, self.detail = state, size, status, detail

    def matches(self, want) -> bool:
        """True only on a definite answer of the expected size."""
        return self.state == "ok" and want is not None and self.size == want

    def describe(self) -> str:
        if self.state == "ok":
            return f"served {self.size} bytes"
        if self.state == "absent":
            return f"absent (HTTP {self.status})"
        return f"unreachable ({self.detail})"


def head(url: str, attempts: int = 3) -> Probe:
    """HEAD a depot object, retrying transport failures but never a 4xx/5xx."""
    last = ""
    for attempt in range(attempts):
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return Probe("ok", size=int(r.headers.get("Content-Length") or 0))
        except urllib.error.HTTPError as e:
            # A status is an answer. Retrying will not change it.
            return Probe("absent", status=e.code)
        except Exception as e:  # transport: DNS, reset, TLS, timeout
            last = f"{type(e).__name__}: {e}"
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    return Probe("error", detail=last)


def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- catalogue --

def bom_for(manifest: dict, release: str) -> dict[str, str]:
    for r in manifest["releases"]:
        if r["version"] == release:
            return {b["name"]: b["version"] for b in r["bom"]}
    known = ", ".join(r["version"] for r in manifest["releases"])
    raise SystemExit(f"release {release} not in manifest. Known: {known}")


def bundles_for(catalog: dict, component: str, version: str) -> list[dict]:
    """Every bundle the catalogue defines for one component version."""
    for rec in catalog["patches"].get(component, []):
        if rec["productVersion"] == version:
            return rec.get("artifacts", {}).get("bundles", [])
    return []


def required_files(catalog: dict, component: str, version: str,
                   types: tuple[str, ...]) -> list[dict]:
    """
    Deduplicated binaries across the requested bundle types.

    Bundles overlap: Logs ships the same four files as both INSTALL and PATCH.
    Keyed by filename so such a file is fetched, verified and copied once.

    Dedup is only safe while the duplicates agree, and that is an assumption
    about someone else's data, so it is checked rather than trusted. If two
    bundles name the same file with a different size or checksum, keeping
    whichever the JSON happened to list first would verify against one and
    publish for both, silently. That is a stop, not a warning.
    """
    seen: dict[str, dict] = {}
    for b in bundles_for(catalog, component, version):
        if b["type"] not in types:
            continue
        for binary in b.get("binaries", []):
            name = binary["fileName"]
            entry = seen.get(name)
            if entry is None:
                seen[name] = dict(binary, bundleTypes=[b["type"]])
                continue
            for field in ("size", "checksum"):
                if entry.get(field) != binary.get(field):
                    raise SystemExit(
                        f"catalogue conflict for {component} {version}: "
                        f"{name} has {field}={entry.get(field)!r} in bundle "
                        f"{entry['bundleTypes'][0]} but {field}={binary.get(field)!r} "
                        f"in bundle {b['type']}. Publish one bundle type at a time.")
            if b["type"] not in entry["bundleTypes"]:
                entry["bundleTypes"].append(b["type"])
    return list(seen.values())


# -------------------------------------------------------------------- audit --

def cmd_audit(args) -> int:
    manifest = fetch_json(args.base, MANIFEST)
    catalog = fetch_json(args.base, CATALOG)
    bom = bom_for(manifest, args.release)
    print(f"VCF {args.release} BOM: {len(bom)} components")
    print(f"depot: {args.base}\n")

    complete, partial, indeterminate, uncatalogued = [], [], [], []
    for comp, ver in sorted(bom.items()):
        entries = catalog["patches"].get(comp)
        if not entries:
            uncatalogued.append((comp, ver, "no catalogue entry"))
            continue
        if not any(p["productVersion"] == ver for p in entries):
            uncatalogued.append((comp, ver, "version not in catalogue"))
            continue
        need = required_files(catalog, comp, ver, tuple(args.types))
        if not need:
            uncatalogued.append((comp, ver, f"no {'/'.join(args.types)} bundle"))
            continue
        missing, unknown = [], []
        for f in need:
            p = head(f"{args.base}/PROD/COMP/{comp}/{f['fileName']}")
            if p.matches(f.get("size")):
                continue
            # A file we could not reach, or cannot judge, is not evidence of a
            # gap. Counting it as missing would overstate what must be
            # downloaded -- the one number an operator acts on.
            (unknown if p.state == "error" or f.get("size") is None
             else missing).append((f, p))
        if unknown:
            indeterminate.append((comp, ver, need, missing, unknown))
        elif missing:
            partial.append((comp, ver, need, missing))
        else:
            complete.append((comp, ver, need))

    print(f"-- complete ({len(complete)}) " + "-" * 40)
    for comp, ver, need in complete:
        print(f"   {comp:<34}{ver:<24}{len(need)} file(s)")

    print(f"\n-- incomplete ({len(partial)}) " + "-" * 38)
    for comp, ver, need, missing in partial:
        short = sum((f.get("size") or 0) for f, _ in missing)
        print(f"   {comp:<34}{ver:<24}{len(missing)}/{len(need)} missing"
              f"  ({short/2**30:.1f} GiB)")
        if args.verbose:
            for f, p in missing:
                print(f"        {f['fileName']}  ({f.get('size')} bytes) -- {p.describe()}")

    if indeterminate:
        print(f"\n-- INDETERMINATE ({len(indeterminate)}) " + "-" * 34)
        print("   the depot did not give a usable answer for these; re-run before"
              " treating them as gaps")
        for comp, ver, need, missing, unknown in indeterminate:
            print(f"   {comp:<34}{ver:<24}{len(unknown)} unresolved,"
                  f" {len(missing)} missing, of {len(need)}")
            for f, p in unknown:
                why = ("catalogue has no size for this file" if f.get("size") is None
                       else p.describe())
                print(f"        {f['fileName']} -- {why}")

    print(f"\n-- not applicable ({len(uncatalogued)}) " + "-" * 34)
    for comp, ver, why in uncatalogued:
        print(f"   {comp:<34}{ver:<24}{why}")
    return 2 if indeterminate else 0


# --------------------------------------------------------------------- plan --

def resolve_version(manifest, catalog, component, release, version):
    if version:
        return version
    bom = bom_for(manifest, release)
    if component not in bom:
        raise SystemExit(
            f"{component} is not in the {release} BOM. "
            f"Pass --version to publish it anyway.")
    return bom[component]


def survey(args):
    """Per-file state: what the catalogue wants, what the depot has, what is staged."""
    manifest = fetch_json(args.base, MANIFEST)
    catalog = fetch_json(args.base, CATALOG)
    version = resolve_version(manifest, catalog, args.component, args.release,
                              args.version)
    need = required_files(catalog, args.component, version, tuple(args.types))
    if not need:
        raise SystemExit(
            f"catalogue defines no {'/'.join(args.types)} bundle for "
            f"{args.component} {version}")

    staging = Path(args.staging) if args.staging else None
    rows = []
    for f in need:
        url = f"{args.base}/PROD/COMP/{args.component}/{f['fileName']}"
        probe = head(url)
        size, checksum = f.get("size"), f.get("checksum")
        src = staging / f["fileName"] if staging else None

        # Without a size and a checksum from the catalogue there is nothing to
        # verify against, and a file we cannot verify is one we will not
        # publish. Say that plainly: reporting it as "missing" would send an
        # operator off to re-download a file that is sitting right there.
        unverifiable = "" if (size is not None and checksum) else (
            "catalogue gives no size" if size is None else "catalogue gives no checksum")

        staged = False
        stage_note = ""
        if src is not None and not unverifiable:
            try:
                staged = src.is_file() and src.stat().st_size == size
                if src.is_file() and not staged:
                    stage_note = f"staged copy is {src.stat().st_size} bytes, want {size}"
            except OSError as e:
                stage_note = f"cannot read staged file: {e}"

        rows.append({
            "file": f["fileName"],
            "size": size,
            "checksum": checksum,
            "bundleTypes": f["bundleTypes"],
            "probe": probe,
            "published": probe.matches(size),
            "staged": staged,
            "stage_note": stage_note,
            "unverifiable": unverifiable,
            "src": src,
        })
    return version, rows


def cmd_plan(args) -> int:
    version, rows = survey(args)
    print(f"{args.component} {version}   bundles: {'/'.join(args.types)}\n")
    for r in rows:
        p = r["probe"]
        if r["unverifiable"]:
            state = "CANNOT VERIFY"
        elif r["published"]:
            state = "in depot"
        elif r["staged"]:
            state = "staged, not published"
        elif p.state == "error":
            state = "UNRESOLVED"
        elif p.state == "absent":
            state = f"missing (HTTP {p.status})"
        else:
            state = f"WRONG SIZE in depot ({p.size})"
        size = f"{(r['size'] or 0)/2**20:.1f} MiB"
        print(f"   {state:<24}{size:>12}   {r['file']}")
        if r["unverifiable"]:
            print(f"        {r['unverifiable']} -- this tool will not publish it")
        if p.state == "error":
            print(f"        {p.describe()} -- re-run before treating this as a gap")
        if r["stage_note"]:
            print(f"        {r['stage_note']}")
        if args.verbose:
            print(f"        bundles={','.join(r['bundleTypes'])}  sha256={r['checksum']}")

    todo = [r for r in rows if not r["published"] and not r["unverifiable"]]
    blocked = [r for r in todo if not r["staged"] and r["probe"].state != "error"]
    print(f"\n   {len(rows) - len(todo)}/{len(rows)} already published")
    if blocked:
        need = sum(r["size"] or 0 for r in blocked)
        print(f"   {len(blocked)} file(s) not staged either -- "
              f"{need/2**30:.2f} GiB must come from the Broadcom portal:")
        for r in blocked:
            print(f"        {r['file']}")
    return 0


# ------------------------------------------------------------------ publish --

def cmd_publish(args) -> int:
    if not args.staging:
        raise SystemExit("--staging is required for publish")
    version, rows = survey(args)
    dest_dir = Path(args.depot_root) / "PROD" / "PROD" / "COMP" / args.component
    print(f"{args.component} {version}")
    print(f"   from {args.staging}")
    print(f"   to   {dest_dir}")
    print(f"   {'APPLY' if args.apply else 'DRY RUN (pass --apply to write)'}\n")

    fail = 0
    for r in rows:
        name = r["file"]
        if r["unverifiable"]:
            print(f"   CANNOT VERIFY {name} -- {r['unverifiable']}; not publishing")
            fail += 1
            continue
        if r["published"]:
            print(f"   SKIP      {name} (already in depot at the right size)")
            continue
        if r["probe"].state == "error":
            print(f"   UNRESOLVED {name} -- {r['probe'].describe()}; not publishing")
            fail += 1
            continue
        if not r["staged"]:
            note = r["stage_note"] or "not in staging -- download it first"
            print(f"   MISSING   {name} ({note})")
            fail += 1
            continue

        print(f"   VERIFY    {name} ...", end="", flush=True)
        try:
            got = sha256_file(r["src"])
        except OSError as e:
            print(f" UNREADABLE: {e}")
            fail += 1
            continue
        if got != r["checksum"].lower():
            print(f" CHECKSUM MISMATCH\n               staged {got}\n"
                  f"               catalogue {r['checksum']}")
            fail += 1
            continue
        print(" ok")

        if not args.apply:
            print(f"   WOULD COPY {name}  ({r['size']/2**20:.1f} MiB)")
            continue

        # Copy to a sibling temp name and rename into place. A copy straight to
        # the served path can leave a truncated file exactly where the fleet
        # will fetch it, and a partial appliance image is worse than an honest
        # 404: os.replace is atomic, so a reader sees the old file or the new
        # one and never a half-written one.
        print(f"   COPY      {name}  ({r['size']/2**20:.1f} MiB) ...",
              end="", flush=True)
        dest = dest_dir / name
        tmp = dest_dir / f".{name}.partial-{os.getpid()}"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(r["src"], tmp)
            written = tmp.stat().st_size
            if written != r["size"]:
                print(f" SIZE MISMATCH after copy: {written}, want {r['size']}")
                fail += 1
                continue
            os.replace(tmp, dest)
            print(" done")
        except (OSError, KeyboardInterrupt) as e:
            print(f" FAILED: {type(e).__name__}: {e}")
            fail += 1
            if isinstance(e, KeyboardInterrupt):
                print("   interrupted -- stopping")
                break
        finally:
            # Never leave scratch behind in a directory the fleet reads.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                print(f"   WARNING: could not remove {tmp}; delete it by hand")

    if args.apply and not fail:
        print("\n   confirming over HTTP (what the fleet will actually read):")
        for r in rows:
            url = f"{args.base}/PROD/COMP/{args.component}/{r['file']}"
            p = head(url)
            if p.matches(r["size"]):
                print(f"      OK   {r['file']}")
            else:
                print(f"      FAIL {r['file']}: {p.describe()}, want {r['size']} bytes")
                fail += 1

    print()
    print(f"   {fail} problem(s)" if fail else "   depot is consistent with the catalogue")
    return 1 if fail else 0


# --------------------------------------------------------------------- main --

def main(argv=None) -> int:
    # Shared options live on a parent parser attached to each subcommand, so
    # they are written AFTER the subcommand ("plan VRLI --types INSTALL").
    # Defining them on the top-level parser as well would look friendlier but
    # is a trap: the subparser's own default silently overwrites whatever was
    # given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default=DEFAULT_BASE, help="depot HTTP base URL")
    common.add_argument("--depot-root", default=DEFAULT_ROOT,
                        help="depot share root (the PROD/PROD doubling is handled)")
    common.add_argument("--release", default=DEFAULT_RELEASE,
                        help="VCF release whose BOM to use")
    common.add_argument("--types", nargs="+", default=["INSTALL"],
                        metavar="TYPE", help="bundle types: INSTALL, PATCH, ...")
    common.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("audit", parents=[common],
                   help="compare the whole BOM against the depot")

    for name, help_ in (("plan", "show what one component needs"),
                        ("publish", "verify and copy a component into the depot")):
        s = sub.add_parser(name, parents=[common], help=help_)
        s.add_argument("component", help="BOM component name, e.g. VRLI")
        s.add_argument("--version", help="override the BOM version")
        s.add_argument("--staging", help="directory holding the downloaded files")
        if name == "publish":
            s.add_argument("--apply", action="store_true",
                           help="actually write; without it this is a dry run")

    args = p.parse_args(argv)
    args.component = getattr(args, "component", None)
    if args.component:
        args.component = args.component.upper()
    # Both of these key into the catalogue, so both are folded. Leaving --types
    # alone made "--types install" match no bundle at all and report the
    # component as having none, which reads like a catalogue problem.
    args.types = [t.upper() for t in args.types]
    return {"audit": cmd_audit, "plan": cmd_plan, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
