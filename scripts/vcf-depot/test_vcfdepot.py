"""
Exercises vcfdepot.py's failure paths with synthetic catalogue data.

These are the cases a live depot cannot produce on demand: a catalogue entry
with no size or checksum, two bundles disagreeing about the same filename, an
unreadable staging file, and an interrupted copy. Each one was a way the tool
could report or do the wrong thing quietly.
"""
import hashlib
import os
import shutil
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vcfdepot  # noqa: E402

TMP = Path(os.environ["TEMP"]) / "vcfdepot-test"
STAGE = TMP / "staging"
DEPOT = TMP / "depot"
fails = 0


def check(label, cond, detail=""):
    global fails
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        fails += 1


def reset():
    if TMP.exists():
        shutil.rmtree(TMP)
    STAGE.mkdir(parents=True)
    DEPOT.mkdir(parents=True)


def make_catalog(binaries_by_type):
    return {"patches": {"TEST": [{"productVersion": "1.0",
                                  "artifacts": {"bundles": [
                                      {"id": "b", "type": t, "binaries": bins}
                                      for t, bins in binaries_by_type.items()]}}]}}


def args_for(**kw):
    a = types.SimpleNamespace(
        base="http://depot.invalid", depot_root=str(DEPOT), release="1.0",
        types=["INSTALL"], verbose=False, component="TEST", version="1.0",
        staging=str(STAGE), apply=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def patch(catalog, probe_state="absent"):
    """Point vcfdepot at synthetic metadata and a depot that has nothing."""
    vcfdepot.fetch_json = lambda base, path: (
        catalog if "productVersionCatalog" in path
        else {"releases": [{"version": "1.0", "bom": [{"name": "TEST", "version": "1.0"}]}]})
    vcfdepot.head = lambda url, attempts=3: vcfdepot.Probe(probe_state, status=404,
                                                           detail="synthetic")


# --- 1. a catalogue entry with no checksum must not read as a mismatch -------
print("\n1. catalogue missing a checksum")
reset()
(STAGE / "f.bin").write_bytes(b"x" * 10)
patch(make_catalog({"INSTALL": [{"fileName": "f.bin", "size": 10}]}))
_, rows = vcfdepot.survey(args_for())
check("flagged unverifiable", bool(rows[0]["unverifiable"]), rows[0])
check("not reported as staged", rows[0]["staged"] is False)
rc = vcfdepot.cmd_publish(args_for(apply=True))
check("publish refuses (rc!=0)", rc != 0, f"rc={rc}")
check("nothing was written", not any(DEPOT.rglob("f.bin")))

# --- 2. a catalogue entry with no size ---------------------------------------
print("\n2. catalogue missing a size")
reset()
(STAGE / "f.bin").write_bytes(b"x" * 10)
patch(make_catalog({"INSTALL": [{"fileName": "f.bin", "checksum": "ab" * 32}]}))
_, rows = vcfdepot.survey(args_for())
check("flagged unverifiable", "no size" in rows[0]["unverifiable"], rows[0]["unverifiable"])
check("published is False, not a crash", rows[0]["published"] is False)

# --- 3. two bundles disagreeing about one filename ---------------------------
print("\n3. bundles disagree about the same file")
reset()
patch(make_catalog({
    "INSTALL": [{"fileName": "f.bin", "size": 10, "checksum": "aa" * 32}],
    "PATCH":   [{"fileName": "f.bin", "size": 99, "checksum": "bb" * 32}]}))
try:
    vcfdepot.survey(args_for(types=["INSTALL", "PATCH"]))
    check("refuses to dedup conflicting entries", False, "no SystemExit raised")
except SystemExit as e:
    check("refuses to dedup conflicting entries", "catalogue conflict" in str(e), str(e))

# agreeing entries must still dedup to one row
patch(make_catalog({
    "INSTALL": [{"fileName": "f.bin", "size": 10, "checksum": "aa" * 32}],
    "PATCH":   [{"fileName": "f.bin", "size": 10, "checksum": "aa" * 32}]}))
_, rows = vcfdepot.survey(args_for(types=["INSTALL", "PATCH"]))
check("identical entries dedup to one row", len(rows) == 1, f"{len(rows)} rows")
check("both bundle types recorded",
      sorted(rows[0]["bundleTypes"]) == ["INSTALL", "PATCH"], rows[0]["bundleTypes"])

# --- 4. checksum mismatch must not publish -----------------------------------
print("\n4. staged file fails its checksum")
reset()
(STAGE / "f.bin").write_bytes(b"x" * 10)
patch(make_catalog({"INSTALL": [{"fileName": "f.bin", "size": 10, "checksum": "cc" * 32}]}))
rc = vcfdepot.cmd_publish(args_for(apply=True))
check("publish refuses", rc != 0, f"rc={rc}")
check("nothing written", not (DEPOT / "PROD/PROD/COMP/TEST/f.bin").exists())
check("no scratch left behind", not list((DEPOT).rglob(".*partial*")))

# --- 5. a good file publishes atomically, leaving no scratch ------------------
print("\n5. a good file publishes")
import hashlib
data = b"y" * 4096
digest = hashlib.sha256(data).hexdigest()
reset()
(STAGE / "f.bin").write_bytes(data)
patch(make_catalog({"INSTALL": [{"fileName": "f.bin", "size": 4096, "checksum": digest}]}))
# the post-copy HTTP confirmation cannot work against a synthetic depot
rc = vcfdepot.cmd_publish(args_for(apply=True))
dest = DEPOT / "PROD" / "PROD" / "COMP" / "TEST" / "f.bin"
check("file landed at the served path", dest.exists() and dest.read_bytes() == data)
check("no scratch left behind", not list(DEPOT.rglob(".*partial*")))

# --- 6. an unreadable staged file must not abort the batch -------------------
print("\n6. staged file disappears between survey and copy")
reset()
(STAGE / "f.bin").write_bytes(data)
patch(make_catalog({"INSTALL": [{"fileName": "f.bin", "size": 4096, "checksum": digest}]}))
a = args_for(apply=True)
_, rows = vcfdepot.survey(a)
(STAGE / "f.bin").unlink()          # vanishes after survey saw it
try:
    rc = vcfdepot.cmd_publish(a)
    check("reports cleanly instead of a traceback", rc != 0, f"rc={rc}")
except Exception as e:
    check("reports cleanly instead of a traceback", False, f"{type(e).__name__}: {e}")

print(f"\n{'FAILURES: %d' % fails if fails else 'all failure paths behave'}")
sys.exit(1 if fails else 0)
