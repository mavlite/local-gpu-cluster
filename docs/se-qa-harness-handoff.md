# Handoff: the Expanse2.Fixes test harness moves to this repository

Ownership of the Space Engineers QA **test harness** — building it, running it, keeping it green —
transfers from `SE-DX2.0Code` to this repository, to run on the `se-qa` guest (VMID 170) already
built here. This document is the transfer package. It assumes no knowledge of the conversation that
produced it.

Written 2026-09-08.

---

## 0. This changes a scope boundary — read this first

`docs/se-qa-vm-requirements.md` §1 currently states:

> **Scope boundary.** This repository owns the *machine*: the guest, its network, its storage, and
> anything touching the Proxmox host. It does not own what runs inside. The plugins, test scripts and
> result logs live in `SE-DX2.0Code` and are somebody else's job.

That is now **partly superseded**. This repository takes ownership of the *harness* — the standalone
check runner described below. It does **not** take ownership of the plugin itself, the Torch server
configuration, or the in-game test plan; those stay in `SE-DX2.0Code`.

§7 of that document also says *"Do not install the third-party QA plugin here."* That prohibition is
unaffected. The harness is first-party code from `SE-DX2.0Code`, not the third-party QA plugin, and
it needs neither Torch nor the game at runtime.

**Action for whoever picks this up:** amend §1 of `se-qa-vm-requirements.md` to carve out the harness,
so the two documents do not contradict each other. Do not simply delete the boundary — the rest of it
still holds.

---

## 1. What the harness is

A single self-contained console executable that verifies two pieces of `Expanse2.Fixes` v2.3.0
behaviour without a game, without Torch, and without a server. It prints one `[PASS]`/`[FAIL]` line
per check and **exits non-zero if any check fails**, so CI can gate on it directly.

Current state: **37 checks, 0 failures.**

| Group | Checks | What it proves |
|---|---|---|
| PatchGuard code-header reader | 32 | Reads the header of freshly JIT'd 1-, 8- and 49-byte methods and confirms via `ntdll!RtlLookupFunctionEntry` that a body under 11 bytes has no function entry at offset `+0xA`. This is the crash geometry behind the Torch 12-byte jump-stub bug. |
| Off-thread `RefreshConstraint` guard | 5 | Drives the guard's thread decision on real OS threads: vanilla runs on the update thread, the Havok constraint rebuild is skipped off it, it fails open with no session, it skips when the update thread is not yet set, and it trusts thread **identity** rather than name. |

### Why the second group exists

`Expanse2.Fixes` v2.3.0 guards a crash that killed eleven production servers in one week: Nexus
serialises medical-room grids on its NetMQ poller thread, which reaches Keen's
`MyMechanicalConnectionBlockBase.RefreshConstraint` and disposes a Havok constraint underneath the
running simulation.

Section 1 of the plugin's test plan asks for proof that the guard intercepts that off-thread call,
observed in game. **That proof cannot be taken on the Asylum testbed.** Nexus is not serving
cross-sector respawn requests there: on 2026-09-08, 147 respawn-screen browses on Asylum2 returned
only that sector's own 9 spawns (Asylum1 holds 197), every one logging `has no cross-sector spawn
data cached`, with zero broadcasts. Asylum1 saw no respawn traffic at all, so the dangerous branch is
unreachable and the guard's silence proves nothing.

These five checks are the substitute. They prove the decision logic deterministically. They do **not**
prove that Harmony binds the prefix to the real Keen method — see §7.

---

## 2. Requirements

### 2.1 The hard constraint: this is Windows-only

**The harness cannot run in an LXC.** This repository's default deployment model does not apply here.

- It targets **.NET Framework 4.8** (`net48`), which exists only on Windows.
- It P/Invokes `ntdll.dll!RtlLookupFunctionEntry` and reads CLR code headers through raw pointers.
  Both depend on Windows x64 unwind metadata and the .NET Framework CLR's in-memory layout.
- It must run **x64** (`PlatformTarget x64`, `Prefer32Bit false`). On x86 the unwind checks are
  meaningless.

Do not attempt a Mono or Wine port of the PatchGuard group; it inspects the real JIT's output and
there is nothing to port it to. The five thread-guard checks are pure managed threading and *could*
be retargeted to `net8.0` for Linux, but they live in the same executable and depend on a `net48`
library, so splitting them is work with little payoff. See §7.

### 2.2 Target host — already satisfied

VMID 170 `se-qa`, built 2026-09-07, meets every requirement as-is:

| Requirement | VM 170 status |
|---|---|
| Windows x64 | Windows Server 2022 Standard, Desktop Experience |
| .NET Framework 4.8 runtime | Ships in Server 2022 — present, no install needed |
| Internet for NuGet restore | Yes; firewall default-allows outbound, `1.1.1.1:443` verified reachable |
| Disk | 200 GB on `local-lvm` |
| CPU / RAM | 6 cores / 16 GB — ample; the harness runs in seconds |

**No GPU is required.** The harness never launches the game client. This is the key reason it is a
good fit for VM 170 as currently configured: the unresolved null-renderer question in
`se-qa-vm-requirements.md` §5 does not block it.

### 2.3 What must be installed on VM 170

Only one thing is missing:

- **.NET SDK** (8.0 or newer). Needed to *build*; the 4.8 targeting pack is **not** required because
  the project pins `Microsoft.NETFramework.ReferenceAssemblies`, which supplies it via NuGet.

If you would rather not install an SDK on the guest, build on a workstation and copy the two output
files (`Expanse2.Fixes.TestHarness.exe`, `Expanse2.Fixes.dll`) — they are the entire runtime
footprint. Building on the guest is preferred, because a green build there is itself evidence the
environment is sound.

### 2.4 NuGet feeds

Restore needs **both**:

- `https://api.nuget.org/v3/index.json` (default)
- `https://nuget.storage.yandexcloud.net/index.json` — carries the Torch and Space Engineers
  reference assemblies, already declared per-project via `RestoreAdditionalProjectSources`

The VM 170 firewall default-allows outbound, so both are reachable. It **cannot** reach
`192.168.6.0/24` or `10.60.0.0/16`, which is intentional and does not affect the harness.

### 2.5 Package dependencies

Resolved automatically by restore; listed so an air-gapped mirror is possible later.

| Package | Version | Notes |
|---|---|---|
| `Torch.Server.ReferenceAssemblies` | `1.3.*-master*` | `ExcludeAssets=runtime` in the plugin |
| `SpaceEngineersDedicated.ReferenceAssemblies` | `1.210.14` | Must match the fleet's game version |
| `Lib.Harmony` | `2.3.*` | `ExcludeAssets=runtime`; resolved at runtime on the fleet from another plugin |
| `Microsoft.NETFramework.ReferenceAssemblies` | `1.0.3` | Lets a locked restore work without the 4.8 targeting pack |
| `PolySharp` | `1.*` | Language-feature polyfills |

---

## 3. The one real obstacle: a cross-repo project reference

The harness is **not** free-standing today. Two couplings must be resolved before it can build from
this repository.

**(a) Project reference.** `Expanse2.Fixes.TestHarness.csproj` contains:

```xml
<ProjectReference Include="..\Expanse2.Fixes\Expanse2.Fixes.csproj" />
```

**(b) `internal` access.** The harness consumes four members that are not public:

| Member | Declared in |
|---|---|
| `NativeCodeInfo.TryRead` | `plugins/Expanse2.Fixes/PatchGuard/NativeCodeInfo.cs` |
| `NativeCodeInfo.DescribeEntry` | same |
| `TinyMethodPatchGuard.ExposedBelow` | `plugins/Expanse2.Fixes/PatchGuard/TinyMethodPatchGuard.cs` |
| `MyMechanicalConnectionOffThreadRefreshPatch.ShouldRunOriginal` | `plugins/Expanse2.Fixes/Patches/MyMechanicalConnectionOffThreadRefreshPatch.cs` |

Access is granted by `<InternalsVisibleTo Include="Expanse2.Fixes.TestHarness" />` in
`Expanse2.Fixes.csproj`. **`Expanse2.Fixes` is not strong-named**, so this works by simple assembly
name — which means the harness assembly must keep the name `Expanse2.Fixes.TestHarness` verbatim, or
all four members become inaccessible and the build fails.

### Options, worst to best

1. **Copy the plugin source into this repo.** Rejected. It forks a shipping plugin and guarantees drift.
2. **Consume a prebuilt `Expanse2.Fixes.dll`.** Works — `InternalsVisibleTo` is name-based, not
   strong-named — but the DLL becomes an opaque binary blob with no provenance, and the harness would
   silently test a stale plugin.
3. **Git submodule of `SE-DX2.0Code`, pinned to a commit.** ✅ **Recommended.** Keeps the
   `ProjectReference` and `InternalsVisibleTo` working unchanged, records exactly which plugin commit
   was tested, and makes an upgrade an explicit submodule bump that shows in review. Costs one
   `git submodule update --init` in the runbook.

Note the repository is private (`DraconisCluster/SE-DX2.0Code`); the guest needs a credential for the
submodule fetch, or you clone once from a workstation and copy.

---

## 4. Items to transfer

### 4.1 Source files

Everything below is on branch `fixes/v2.3.0-hardening` at commit `54e7dca4`:

| File | Lines | Role |
|---|---|---|
| [`plugins/Expanse2.Fixes.TestHarness/Program.cs`](https://github.com/DraconisCluster/SE-DX2.0Code/blob/54e7dca411c0174ce15b37b82f4afac9916d2aeb/plugins/Expanse2.Fixes.TestHarness/Program.cs) | 101 | The check runner |
| [`plugins/Expanse2.Fixes.TestHarness/Expanse2.Fixes.TestHarness.csproj`](https://github.com/DraconisCluster/SE-DX2.0Code/blob/54e7dca411c0174ce15b37b82f4afac9916d2aeb/plugins/Expanse2.Fixes.TestHarness/Expanse2.Fixes.TestHarness.csproj) | 34 | Project file |

Referenced but **not** transferred — they stay in the plugin and arrive via the submodule:

- [`plugins/Expanse2.Fixes/PatchGuard/NativeCodeInfo.cs`](https://github.com/DraconisCluster/SE-DX2.0Code/blob/54e7dca411c0174ce15b37b82f4afac9916d2aeb/plugins/Expanse2.Fixes/PatchGuard/NativeCodeInfo.cs)
- [`plugins/Expanse2.Fixes/PatchGuard/TinyMethodPatchGuard.cs`](https://github.com/DraconisCluster/SE-DX2.0Code/blob/54e7dca411c0174ce15b37b82f4afac9916d2aeb/plugins/Expanse2.Fixes/PatchGuard/TinyMethodPatchGuard.cs)
- [`plugins/Expanse2.Fixes/Patches/MyMechanicalConnectionOffThreadRefreshPatch.cs`](https://github.com/DraconisCluster/SE-DX2.0Code/blob/54e7dca411c0174ce15b37b82f4afac9916d2aeb/plugins/Expanse2.Fixes/Patches/MyMechanicalConnectionOffThreadRefreshPatch.cs)

### 4.2 ⚠️ The links above are missing a day's work

Commit `54e7dca4` predates 2026-09-08. **The five off-thread guard checks and the `ShouldRunOriginal`
seam they test are not in it.** The linked `Program.cs` is 101 lines; the current one is 173.

That work is uncommitted on branch `qa/headless-client-spike` in the working tree at
`C:\Users\willi\Documents\GitHub\SE-DX2.0Code`. Until it is pushed there is no permalink for it.
It is exported as a patch — 3 files, +89/−2:

```
harness-2026-09-08.patch     # apply with: git apply harness-2026-09-08.patch
```

**Recommended:** push that work to a branch on `SE-DX2.0Code` first, then pin the submodule to it and
replace the links in §4.1. Transferring via a loose patch file invites exactly the drift the submodule
is meant to prevent.

### 4.3 Background reading (not required, but it is the "why")

In `DraconisCluster/sdg-fleetmanager`, branch `docs/ex-crash-hardening-2026-09-06`:

- `docs/handoffs/2026-09-06-ex-crash-hardening.md` — the two crash clusters and the four commits
- `docs/incidents/2026-09-05-refreshconstraint-guard-test-plan.md` — the test plan whose section 1 this harness substitutes for
- `docs/incidents/2026-09-05-12h-crash-review.md` — §3 is the root cause of the guarded crash

---

## 5. Build and run

```bash
# on VM 170, from the repo root, after the submodule is initialised
dotnet build plugins/Expanse2.Fixes.TestHarness -c Release -p:SolutionDir=<repo-root>\

plugins/Expanse2.Fixes.TestHarness/bin/Release/net48/Expanse2.Fixes.TestHarness.exe
echo $?    # 0 = all checks passed, non-zero = failure count
```

`SolutionDir` **must** be passed with a trailing backslash; the plugin's `Zip` target writes to
`$(SolutionDir)\plugins\.build` and fails without it.

Expected tail:

```
Off-thread RefreshConstraint guard
  [PASS] on the update thread: vanilla RefreshConstraint runs
  [PASS] off the update thread: the Havok constraint rebuild is skipped
  [PASS] no game: vanilla runs even off the update thread
  [PASS] game up, update thread not yet set: skips
  [PASS] a worker sharing the update thread's name ('NetMQPollerThread') still skips

ALL CHECKS PASSED
```

Four `MSB3277`/`MSB3246` warnings about `Newtonsoft.Json` version unification and a reference with no
metadata are **pre-existing and expected**. They are not regressions; do not chase them.

### Driving it from the host

Per `AGENTS.md` and `se-qa-vm-requirements.md` §4, control VM 170 through the guest agent, not SSH,
and pass payloads base64-encoded rather than as inline heredocs:

```bash
qm guest exec 170 -- powershell -EncodedCommand <base64-utf16le>
```

---

## 6. Work completed to date (2026-09-08)

- Harness builds clean and runs green on Windows x64 / .NET Framework 4.8: **37 checks, 0 failures, exit 0.**
- Five off-thread `RefreshConstraint` checks added test-first. The failing state was observed before
  implementing: against a `=> true` stub — which is exactly "no guard at all" — three checks failed,
  and precisely the three *skip* assertions, confirming they discriminate rather than merely passing.
- Production change to support it is 11 lines: extracting `ShouldRunOriginal` from the Harmony prefix.
  Reviewed and confirmed **exactly behaviour-preserving**, including that `game?.UpdateThread` does not
  invoke the getter on a null receiver, so no extra property read is introduced on a hot path.
- Review verdict **APPROVE**, no findings above LOW. One LOW acted on (a reflexive check that also
  passed against the stub was documented and moved onto a dedicated thread); one LOW accepted as-is
  (a timed-out worker is left orphaned — background thread, 10-second budget, process exits anyway).

Nothing is committed. Nothing has been pushed.

---

## 7. Known gaps — inherited, not introduced

1. **Harmony binding is unverified by the harness.** The five checks prove the guard's *decision*, not
   that the prefix is actually attached to Keen's method. Field evidence covers it for now — both
   Asylum instances logged `patched MyMechanicalConnectionBlockBase.RefreshConstraint` on 2026-09-08 —
   but a Keen rename would surface as a silently missing patch on the fleet rather than a build
   failure. The fix is a binding-surface test using `AccessTools` against the real assemblies. A test
   project doing exactly this (`Expanse2.Fixes.Tests`) was dropped on 2026-09-02 and is recoverable
   from git history at `88f497f0^`; it has no entry for this patch because the patch was written after.
2. **No CI.** The harness exits non-zero on failure and is ready to gate, but nothing runs it
   automatically. A Windows runner is required.
3. **The in-game rows are still `SE-DX2.0Code`'s.** Test-plan section 2 (rotor detach/reattach, grid
   split and merge, blueprint paste, pistons, hangar, wheels) needs a real client and does not move here.
4. **VM 170 cannot reach the fleet**, by design (`10.60.0.0/16` is dropped). The harness does not care.
   Anything that later needs to talk to Asylum does, and that constraint must not be quietly relaxed.

---

## 8. Definition of done for this transfer

1. `SE-DX2.0Code` is available to the build as a pinned submodule, and the day's uncommitted work is
   pushed and pinned rather than carried as a patch file.
2. The harness builds and runs green **on VM 170**, and the real command output is recorded in §9.
3. `docs/se-qa-vm-requirements.md` §1 is amended so the scope boundary matches reality.
4. A runbook entry exists for driving the build and run through `qm guest exec`.
5. The .NET SDK version installed on VM 170 is recorded in §9, so the build is reproducible.

---

## 9. Build log

| Date | Who | What happened |
|---|---|---|
| 2026-09-08 | — | This handoff written. Nothing installed on VM 170 yet; harness still builds and runs only on the `SE-DX2.0Code` workstation. |
