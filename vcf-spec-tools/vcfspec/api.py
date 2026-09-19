"""The orchestrator: one entry point per tool, already redacted.

This is the first code that composes every layer built so far (documents,
inventory schema, vendor schema, rules, render, probes), so it is the first
place their interactions can go wrong. Three things this module owns and
every other layer explicitly does not:

1. Redaction happens here, once, on the assembled output -- not scattered
   across rule modules. schema_layer.py already redacts its own messages
   (jsonschema echoes raw spec values into strings it builds itself), so
   redacting again here must be a no-op; redact() is idempotent (see
   tests/test_redact.py::test_redact_is_idempotent).
2. Every loader failure (DocumentTooLarge, DocumentTooDeep,
   DocumentTooManyAliases, a yaml.YAMLError, a non-mapping root) becomes a
   structured finding, and so does anything render() raises -- not just
   the documented InsecureCredentialError. render() is not defensive the
   way the rules modules are (e.g. a bad int() or a missing dict key on
   schema-invalid input), and that exception surface is not enumerable in
   advance, so render_document() catches broadly at that boundary rather
   than trying to keep the list complete. Nothing here ever lets an
   exception reach the caller.
3. In validate_document(), a schema finding blocks rules only for the
   subtree at its JSON pointer: "/networks/vsan" blocks "/networks/vsan/..."
   and nothing else. Gating compares pointer *segments*, not characters --
   a raw `pointer.startswith(blocked)` would wrongly block a sibling like
   "/networks/vsanWitness". render_document() does NOT gate its rule pass:
   render() does not respect gating either, so filtering only the finding
   and not the spec value it warns about would silently hand an operator a
   spec with a real problem and one fewer finding explaining it.
"""
from __future__ import annotations

from dataclasses import asdict

from .credentials import credential_findings
from .documents import DocumentKind, detect_kind, load_document
from .findings import Result, Severity
from .inventory import validate_inventory
from .redact import redact
from .render import InsecureCredentialError, render
from .rules import finding_for
from .rules.network import check_networks
from .rules.platform import check_platform
from .schema import (DEFAULT_VERSION, SchemaIntegrityError, known_versions,
                     load_schema)
from .validate.probes import ProbeConfig, run_probes
from .validate.schema_layer import (validate_against_schema,
                                    walk_declared_properties)

# Codes that mean "this JSON pointer's subtree failed schema validation".
_GATING_CODES = ("VCF-INV-SCHEMA", "VCF-SCHEMA")

# Both "/" (root) and "" are how a root-level error can be spelled; neither
# may ever gate, or every rule finding in the document is suppressed.
_ROOT_POINTERS = ("/", "")


def subtree_blocked(findings) -> set[str]:
    """Pointers whose subtree failed schema validation."""
    return {f.path for f in findings
            if f.code in _GATING_CODES and f.path not in _ROOT_POINTERS}


def _unescape_segment(segment: str) -> str:
    """RFC 6901 decoding: ~1 -> '/' must happen before ~0 -> '~'.

    Doing it in the other order would mis-decode a literal "~01": read
    left to right, that has to become "~1" (an escaped tilde followed by a
    literal '1'), not "/" (which is what decoding ~0 first would produce).
    """
    return segment.replace("~1", "/").replace("~0", "~")


def _segments(pointer: str) -> tuple[str, ...]:
    return tuple(_unescape_segment(part) for part in pointer.split("/") if part != "")


def _is_blocked(pointer: str, blocked: set[str]) -> bool:
    """True when pointer is the blocked pointer itself, or inside it.

    Compares JSON-pointer segments, not raw characters. Segment comparison
    is what keeps "/networks/vsan" from blocking "/networks/vsanWitness":
    they share a character prefix but not a segment prefix.

    A blocked entry with zero segments (root, spelled either "/" or "")
    is always skipped: an empty segment tuple is a prefix of every
    pointer's segments, so honouring it here would silently suppress
    every rule finding in the document. subtree_blocked() already keeps
    root pointers out of the set it builds, but that guard belongs on
    this function too -- it must hold regardless of which caller built
    `blocked`, not just for the one caller that remembers to filter first.
    """
    pointer_segments = _segments(pointer)
    for entry in blocked:
        entry_segments = _segments(entry)
        if entry_segments and pointer_segments[:len(entry_segments)] == entry_segments:
            return True
    return False


def _filter_blocked(result: Result, blocked: set[str]) -> Result:
    if not blocked:
        return result
    return Result(tuple(f for f in result.findings if not _is_blocked(f.path, blocked)))


def _unknown_version_finding():
    """The requested VCF version is not one this installation vendors.

    Takes no argument on purpose. Findings 1 and 2: the caller's version
    string never reaches the envelope, so every rejected version -- a
    traversal, an absolute path, an embedded NUL, a URL-encoded traversal,
    or a plain unknown-but-well-formed "9.9.9.9" -- yields a byte-identical
    result. That uniformity IS the fix for the existence oracle; the
    closed-set gate in schema.resolve_version() means no path was built to
    have an existence in the first place, and this keeps the envelope from
    reintroducing a difference the filesystem no longer has.

    The vendored set is reported instead, which is the thing a legitimate
    caller needs and the thing an attacker already knows.
    """
    return finding_for("VCF-SCHEMA-VERSION-UNKNOWN", "/",
                       known=", ".join(sorted(known_versions())) or "(none)")


def _integrity_finding(version: str):
    """The vendored schema no longer matches its recorded checksum.

    Finding 6: this used to share an `except` clause with "no such
    version" and so reached an MCP caller as VCF-MCP-BAD-ARGS -- a
    retryable usage error -- for what is a supply-chain event. It gets its
    own critical code, and mcp_server._reclassify_unknown_version
    deliberately does not translate it.

    `version` IS quoted here, unlike the unknown-version finding above,
    and that is safe rather than inconsistent: reaching an integrity check
    at all means the version already passed schema.resolve_version(), so
    it is a member of the closed vendored set -- a string this package
    read off its own disk, never caller text.
    """
    return finding_for("VCF-SCHEMA-INTEGRITY", "/", version=version)


def _unreadable_envelope(exc: Exception, skipped: dict[str, str]) -> dict:
    # str(exc) is never used here: it is text from documents.py's parser
    # and depth/size checks, built from operator-supplied YAML, and
    # redact() only masks shapes it recognises -- it cannot promise
    # anything about text it has never seen. The exception's class name
    # is safe (it names a Python type, never spec content) and is enough
    # to tell DocumentTooLarge apart from a plain yaml.YAMLError.
    finding = finding_for("VCF-INPUT-UNREADABLE", "/", exc_type=type(exc).__name__)
    return _envelope(Result((finding,)), ["detect"], skipped)


def _rules_for_inventory(doc: dict, blocked: set[str]) -> Result:
    rules = check_networks(doc).merge(check_platform(doc))
    return _filter_blocked(rules, blocked)


def validate_document(text: str, input_kind: str | None = None,
                      probe_config: ProbeConfig | None = None,
                      version: str = DEFAULT_VERSION) -> dict:
    layers_run: list[str] = ["detect"]
    skipped: dict[str, str] = {}

    try:
        doc = load_document(text)
    except Exception as exc:
        return _unreadable_envelope(exc, {
            "schema": "document unreadable", "rules": "document unreadable",
            "probes": "document unreadable"})

    kind, result = detect_kind(doc, input_kind)
    if kind is DocumentKind.UNKNOWN:
        for layer in ("schema", "rules", "probes"):
            skipped[layer] = "document kind unknown"
        return _envelope(result, layers_run, skipped)

    if kind is DocumentKind.INVENTORY:
        schema_result = validate_inventory(doc)
        result = result.merge(schema_result)
        layers_run.append("schema")

        # Finding 11: an inventory document has one fixed schema regardless
        # of `version` -- nothing below this branch ever reads it. Before
        # this, `vcf_version: "0.0.0"` on an inventory silently had zero
        # effect and the result still said `valid: true`, which a caller
        # could easily read as "checked and fine for VCF 0.0.0" when
        # nothing checked that at all. This must not *reject* the call --
        # an inventory-kind call carrying an irrelevant vcf_version is a
        # legitimate shape (fix A already established that rejecting it
        # outright is wrong) -- it only has to stop the result being
        # silently misleading about what was consulted. An info finding
        # does that without changing `valid`, and is skipped when `version`
        # is the default: a caller who never mentioned a version, or who
        # asked for the one already in effect, has nothing to be told.
        #
        # The note does not quote `version` back. That is the same rule
        # the rejection path follows (_unknown_version_finding), applied
        # here too: this path performs no filesystem access and is not an
        # oracle, but the value is still arbitrary caller text landing in
        # a message an AI agent reads, and naming the vendored set tells
        # the operator strictly more than repeating what they just sent.
        if version != DEFAULT_VERSION:
            result = result.merge(Result((finding_for(
                "VCF-VERSION-NOT-CONSULTED", "/",
                known=", ".join(sorted(known_versions())) or "(none)"),)))

        blocked = subtree_blocked(schema_result.findings)
        result = result.merge(_rules_for_inventory(doc, blocked))
        layers_run.append("rules")
        if blocked:
            skipped["rules"] = f"suppressed under {sorted(blocked)}"
    else:
        # validate_against_schema() loads the vendored schema for `version`
        # unconditionally; if nothing was ever vendored for it (or its
        # checksum was tampered), that raises rather than returning a
        # Result. Left uncaught, that would break this module's own
        # promise that nothing here ever lets an exception reach the
        # caller -- so it becomes a finding here, the same as every other
        # documented-but-not-guaranteed input problem, instead of
        # propagating as a raw FileNotFoundError.
        try:
            schema_result = validate_against_schema(doc, version)
        except SchemaIntegrityError:
            schema_result = Result((_integrity_finding(version),))
        except FileNotFoundError:
            schema_result = Result((_unknown_version_finding(),))
        # The vendored VMware schema accepts a real password -- real
        # passwords are what it is for -- so the schema pass alone cannot
        # enforce "credentials are ${reference} strings, never secrets".
        # Without this, vcf_validate_spec(input_kind="sddc_spec") returned
        # valid=True and zero findings on a document holding two real root
        # passwords, and the operator was told their spec was clean.
        schema_result = schema_result.merge(credential_findings(doc))
        result = result.merge(schema_result)
        layers_run.append("schema")
        skipped["rules"] = "rules operate on inventories; render first"

    if kind is not DocumentKind.INVENTORY:
        # run_probes reads the inventory shape: inventory["dns"]["subdomain"],
        # inventory["hosts"][i]["name"], ["mgmtIp"]. An SddcSpec spells those
        # dnsSpec and hostSpecs[].hostname, and has no per-host management
        # address at all -- so there is nothing for the IP allowlist to gate
        # a probe against, and probing it correctly is not a matter of
        # renaming keys. This block used to sit outside the kind branch, so
        # it ran anyway: inventory.get("hosts") returned None, the loop body
        # never executed, and the layer reported success by saying nothing.
        # layers_run listed "probes" and layers_skipped stayed silent, so
        # both halves of the envelope's own honesty mechanism agreed that
        # the probe layer had run and was content, having inspected nothing.
        # An explicit skip, exactly like the "rules" line above, is the only
        # acceptable answer: telling an operator something was checked when
        # it was not is the failure mode this envelope exists to prevent.
        skipped["probes"] = ("probes read the inventory shape; an SddcSpec "
                             "has no per-host management IP to allowlist")
    elif probe_config is None:
        skipped["probes"] = "no probe configuration supplied"
    elif any(f.severity is Severity.CRITICAL for f in result.findings):
        skipped["probes"] = "critical findings present"
    else:
        result = result.merge(run_probes(doc, probe_config))
        layers_run.append("probes")

    return _envelope(result, layers_run, skipped)


def render_document(text: str, version: str = DEFAULT_VERSION,
                    input_kind: str | None = None) -> dict:
    try:
        doc = load_document(text)
    except Exception as exc:
        return _unreadable_envelope(exc, {"render": "document unreadable"})

    kind, result = detect_kind(doc, input_kind)
    if kind is not DocumentKind.INVENTORY:
        # detect_kind() attaches no finding for a document it correctly
        # recognised as SDDC_SPEC -- sniffing a real one is not itself an
        # error. But render() only ever accepts a LabInventory, so without
        # this, a legitimately-detected SddcSpec document reached here
        # with an *empty* result: `valid: true`, zero findings, on a
        # document nothing about was actually checked (render never runs,
        # schema/rules never run). Say so explicitly instead of reporting
        # a clean pass over an uninspected document. (UNKNOWN already
        # carries its own finding from detect_kind -- VCF-INPUT-UNRECOGNISED
        # -- so this does not double up on that branch.)
        if kind is not DocumentKind.UNKNOWN:
            result = result.merge(Result((finding_for(
                "VCF-RENDER-WRONG-KIND", "/", kind=str(kind)),)))
        return _envelope(result, ["detect"], {"render": "input is not an inventory"})

    # No subtree gating here, unlike validate_document: render's rule pass
    # is never filtered. Gating a rule finding without also gating the
    # spec value it warns about would let an operator receive a rendered
    # spec with a real problem in it and one fewer finding explaining why.
    schema_result = validate_inventory(doc)
    rules_result = check_networks(doc).merge(check_platform(doc))
    combined = result.merge(schema_result).merge(rules_result)
    layers_run = ["detect", "schema", "rules"]

    # render() is documented as not trusting that validate_inventory() ran
    # first: it re-checks every credential and raises rather than emitting
    # a literal secret. That guard, and any other exception render() can
    # raise on malformed input (a bad int(), a missing key -- the set of
    # possible failures is not enumerable in advance), must never
    # propagate out of the orchestrator -- convert it to a finding instead.
    #
    # Neither branch below puts str(exc) in the finding: redact() only
    # masks shapes it recognises (jsonschema-echo and key=/secret=-shaped
    # text), and an exception raised by arbitrary code operating on
    # operator data is not a recognised shape -- it could read anything,
    # including a spec value verbatim. Only the exception's class name
    # (never spec content) reaches the message. The detail is not lost:
    # validate_document() on the same input reports the real, structured
    # finding, which is where an operator should be looking anyway.
    try:
        spec, render_result = render(doc, version)
    except InsecureCredentialError:
        finding = finding_for("VCF-RENDER-INSECURE-CREDENTIAL", "/credentials")
        combined = combined.merge(Result((finding,)))
        return _envelope(combined, layers_run, {"render": "refused an insecure credential"})
    except SchemaIntegrityError:
        combined = combined.merge(Result((_integrity_finding(version),)))
        return _envelope(combined, layers_run,
                         {"render": "the vendored schema failed its integrity check"})
    except FileNotFoundError:
        # render() loads vcfspec/defaults/<version>.yaml unconditionally;
        # an unvendored `version` raises here. That used to be caught by
        # the broad `except Exception` below and reported as
        # VCF-RENDER-FAILED -- true, but indistinguishable from "render()
        # has a bug", when this is really "you asked for a VCF release
        # nothing is vendored for". Same code as validate_document's
        # equivalent guard, for the same underlying condition.
        combined = combined.merge(Result((_unknown_version_finding(),)))
        return _envelope(combined, layers_run,
                         {"render": "no vendored data for this VCF version"})
    except Exception as exc:
        finding = finding_for("VCF-RENDER-FAILED", "/", exc_type=type(exc).__name__)
        combined = combined.merge(Result((finding,)))
        return _envelope(combined, layers_run,
                         {"render": "render() raised an unexpected exception"})

    combined = combined.merge(render_result)
    layers_run.append("render")

    # The output is checked, not just the input. Without this,
    # `valid: true` from render did not mean the rendered spec was valid:
    # an inventory gateway of "nope" passed the inventory schema (which
    # types it as a bare string), skipped the rule layer (_address()
    # returns None for an unparseable address), and was copied straight
    # into networkSpecs[0].gateway -- where the vendored VMware schema
    # rejects it. Exit 0, valid: true, layers_run ending in "render", and
    # a spec the Installer would refuse five hours into a bare-metal
    # build, which is the exact failure this package exists to prevent.
    verify_result, verify_ok = _verify_rendered(spec, version)
    combined = combined.merge(verify_result)
    layers_run.append("verify")

    if not verify_ok:
        # No spec key. The whole point is preventing a bad spec reaching
        # VCF, so handing one back with a warning attached would defeat
        # it -- an operator (or an agent) pipes `.spec` into a file and
        # never reads the findings. This mirrors what the insecure-
        # credential path above already does: emit the findings, withhold
        # the output.
        #
        # Note the line this draws: only the *verify* layer withholds the
        # spec. A rule finding about the inventory (a gateway outside its
        # subnet, an undersized TEP pool) still returns one, because those
        # describe the input and an operator fixes them by iterating on
        # the render. A spec that the vendored schema rejects, or that
        # carries a field the schema does not declare, is different in
        # kind: it is not a spec, and there is nothing to iterate on.
        return _envelope(combined, layers_run,
                         {"spec": "the rendered spec failed schema validation"})

    envelope = _envelope(combined, layers_run, {})
    envelope["spec"] = redact(spec)
    return envelope


def _verify_rendered(spec: dict, version: str) -> tuple[Result, bool]:
    """Check the *rendered* spec against the vendored schema. Returns
    (findings, ok) where ok is False when the spec must be withheld.

    Two independent checks, because neither subsumes the other:

    1. validate_against_schema() -- types, required fields, minLength,
       patterns. This is what catches a value copied verbatim out of the
       inventory that the inventory's own looser schema permitted.
    2. walk_declared_properties() -- an invented key name at any depth.
       jsonschema cannot catch that here: no $def in the vendored schema
       sets additionalProperties: false, so an undeclared key validates
       cleanly at every level. Before this, that walk existed, worked, was
       well tested, and had zero production call sites -- it ran against
       one bundled fixture in tests/test_render.py and nothing else.
       Wiring it here makes it a runtime guard over every render branch,
       including the ones no example exercises (storage.type != VSAN_ESA,
       nsx absent, vsp absent).

    Nothing here may raise: this module promises no exception reaches the
    caller. load_schema() raises for a version nothing was vendored for,
    and _resolve_ref() raises on a malformed schema; both become findings.
    """
    try:
        findings = list(validate_against_schema(spec, version).findings)
        walk = walk_declared_properties(load_schema(version), "SddcSpec", spec)
    except SchemaIntegrityError:
        return Result((_integrity_finding(version),)), False
    except FileNotFoundError:
        return Result((_unknown_version_finding(),)), False
    except Exception as exc:
        return Result((finding_for("VCF-RENDER-FAILED", "/",
                                   exc_type=type(exc).__name__),)), False

    for pointer in sorted(walk.undeclared):
        findings.append(finding_for("VCF-RENDER-UNDECLARED-FIELD", pointer,
                                    pointer=pointer))
    result = Result(tuple(findings))
    return result, result.valid


def _envelope(result: Result, layers_run: list[str], skipped: dict[str, str]) -> dict:
    result = _refuse_unvalidated_success(result, layers_run)
    return redact({
        "valid": result.valid,
        "findings": [asdict(f) for f in result.findings],
        "layers_run": layers_run,
        "layers_skipped": skipped,
    })


def _refuse_unvalidated_success(result: Result, layers_run: list[str]) -> Result:
    """Defence in depth, independent of any one caller's own skip logic:
    a result must never report valid=True when no substantive layer --
    anything beyond the unconditional 'detect' step -- actually ran.

    Every current path that skips every layer already attaches its own
    explanatory finding (VCF-INPUT-UNRECOGNISED, VCF-INPUT-BAD-KIND,
    VCF-INPUT-UNREADABLE, VCF-RENDER-WRONG-KIND...), so in today's code
    this should never actually fire -- it is not the primary signal for
    any of those cases, on purpose; each of them says precisely *why*
    nothing ran. It exists for the path neither this fix nor the one
    before it has thought of: a future kind-detection branch, a new
    input_kind value, a new document shape, anything that reaches
    _envelope() with layers_run == ["detect"] and forgets to attach a
    finding of its own. That path fails safe here -- reported invalid,
    with an explanation that at least says nothing was checked -- instead
    of silently reporting a clean pass on a document nothing inspected.
    """
    if result.valid and layers_run == ["detect"]:
        return result.merge(Result((finding_for("VCF-NO-VALIDATION-RAN", "/"),)))
    return result
