# Offline depot publisher

Puts component media into the lab's VCF offline depot, using the depot's own
catalogue as the authority for what is needed and whether it arrived intact.

```bash
python vcfdepot.py audit                                   # whole-BOM gap report
python vcfdepot.py plan VRNI --types INSTALL PATCH --staging "~/Downloads/VCF Automation"
python vcfdepot.py publish VRLI --staging "..." --apply     # verify + copy
```

Options go **after** the subcommand — `plan VRNI --types ...`, not
`plan --types ... VRNI`, because `--types` takes a list and would swallow the
component name. `publish` is a dry run unless `--apply` is given, never
overwrites a file already present at the right size, and refuses any file whose
SHA-256 does not match the catalogue.

`python test_vcfdepot.py` exercises the failure paths (no network, no depot).

## What it refuses to do

Each of these was a way the tool could once have done or reported the wrong
thing quietly:

- **A file it cannot verify, it will not publish.** If a catalogue entry has no
  `size` or no `checksum`, that is reported as `CANNOT VERIFY` rather than as a
  missing file — the difference between "the catalogue is incomplete" and "go
  re-download 6 GiB from Broadcom".
- **An unreachable depot is not an empty one.** A HEAD that fails on transport
  is retried, then reported as `UNRESOLVED`, never counted as a gap. `audit`
  puts such components in their own section and exits 2. Without this, one
  reset connection during a 65-component sweep silently inflates the "still to
  download" figure with files that were there all along.
- **Disagreeing catalogue entries stop the run.** Dedup across bundle types is
  only sound while the duplicates agree, so that is checked. If two bundles
  name one file with different checksums, the tool exits rather than verifying
  against one and publishing for both.
- **Copies are atomic.** Files land on a sibling temp name and are renamed into
  place, so an interrupted copy cannot leave a truncated appliance image at the
  path the fleet fetches. A partial image is worse than an honest 404.
- **One bad file does not abort the batch.** An unreadable staging file or a
  failed copy is counted and reported; the remaining files still publish.

## Where the answers come from

Nothing about any product is hard-coded. Both the file list and every SHA-256
come from the depot's own metadata:

| Question | Answered by |
|---|---|
| Which components does release X need, at which version? | `PROD/metadata/manifest/v1/vcfManifest.json` |
| Which files make up that component version? | `PROD/metadata/productVersionCatalog/v1/productVersionCatalog.json` |
| Did the file arrive intact? | the `checksum` on each binary in that catalogue |

A component this tool has never seen works the same way as one it has.

## Things that will catch you out

**Read the catalogue, not the `depot-manifest-*.yaml`.** The per-component
depot manifest is *not* the full file list. VSP's manifest names four files;
its catalogue entry names six, including the `vmsp-cli-*.tar.gz` that the
Installer requests first. Trusting the manifest leaves you chasing a 404 for a
file you never knew existed.

**The HTTP path and the write path do not line up.** The web root is the
share's `PROD` directory, so HTTP `/PROD/COMP/VRLI/x` is written to
`<share>/PROD/PROD/COMP/VRLI/x`. `--depot-root` is the share root; the tool
handles the doubling.

**Bundles share files.** Logs 9.1.1 ships the same four binaries as both its
INSTALL and its PATCH bundle — same names, same checksums. Asking for
`--types INSTALL PATCH` fetches, verifies and copies each file once, and
publishing the install set satisfies the upgrade path for free. Networks is the
opposite: its PATCH bundle is three files that the INSTALL bundle never
mentions.

**Packaging generation changes between releases.** Log Insight was an appliance
OVA through 9.0.2 and became a four-file `operations-logs-*.tgz` set at 9.1.0.
An older OVA sitting in `PROD/COMP/VRLI/` is not a partial upgrade; it is a
different artifact entirely, and leaving it there is harmless.

**Size agreement is not integrity.** `plan` and `audit` compare sizes because
that is cheap over HEAD, but `publish` hashes every file before copying and
re-checks what is served afterwards. To hash what the depot actually serves,
read it back over HTTP rather than trusting the share.

## Scope

`audit` reports only against the catalogue; a component marked *not applicable*
is one the manifest names but the catalogue does not describe (`VMTOOLS`,
`GOSC`, the vSAN witnesses), or one with no bundle of the requested type
(`ESX_HOST` has no INSTALL bundle). Those are not gaps.

Most of a 65-component BOM is optional. In this lab only the deployed set needs
to be complete — the Supervisor services, VKS, DSM, HCX and NSX_ALB entries are
expected to be absent.
