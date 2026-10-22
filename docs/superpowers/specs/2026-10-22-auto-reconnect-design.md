# Automatic Reconnect And Resume After Storage Loss

Status: approved by owner (2026-10-22)

## Why

A technician rescued data from a badly failing HDD that needed external cooling
and kept dropping off the bus. Copying is slow, so such jobs routinely run
unattended overnight. Today `copy`/`resume` exits with code 3 when storage
disappears (`StoragePaused`), the active file returns to `pending` without
consuming a retry, and the operator must reconnect the disk by hand and run
`resume`. Manual resume already works well; the overnight gap is that one
disconnect at 1 a.m. idles the job until morning.

Goal: when the source (or destination) disappears, the tool waits, lets the disk
cool down, brings the same volume back online when the OS can do that without
physical intervention, verifies it is the same volume, and resumes, with hard
limits so a dying disk is not hammered all night.

## Goals

- Opt-in automatic wait → cooldown → remount → identity check → resume loop
  inside a single `copy`/`resume` invocation.
- Remount only the recorded volume, identified by APFS/HFS Volume UUID, and mount
  the source **read-only**.
- Hard stop criteria that turn repeated disconnects into an explicit escalation
  ("image the disk with ddrescue / hand to a lab") instead of endless retries.
- Every pause/reconnect/resume event is recorded in the job directory and shown
  by `status`, `status --watch`, `next`, `report`, and the GUI.
- Deterministic behavior: no LLM, no heuristics beyond the documented rules.

## Non-goals

- Power-cycling USB ports, hubs, docks or smart plugs. macOS has no supported
  API for built-in ports; per-port switching needs special hubs (uhubctl) and
  every power cycle stresses a failing drive. Possible later extension: see
  "Open decisions".
- Unlocking FileVault/encrypted volumes, entering SMB credentials, or any
  interactive prompt during the night.
- Disk imaging (`ddrescue`) itself; this design only recommends it.
- Rewriting the job's recorded source/destination paths to a new mount point.

## Anti-goals (failure even if it "works")

- Any write to the source volume, including a filesystem repair triggered by
  our mount request.
- Resuming against a different volume that happens to have the same name/path.
- Silent infinite retry overnight on a disk that is getting worse.
- Holding the job in a state where `status`/GUI shows "running" or "idle" while
  it is actually waiting for a disk.

## Behavior

### Identity recorded per job

At `init` (and lazily on the first `copy` of older jobs, while storage is
available, the same way `storage_anchors` is upgraded today) record for source
and destination roots, from `diskutil info -plist <mount point>`:

- `VolumeUUID`, `MountPoint`, `FilesystemType`, `DeviceIdentifier` (informational
  only; device identifiers change across reconnects), whether it is a network
  filesystem (`smbfs`, `afpfs`, `nfs`, `webdav`), and whether it was mounted
  read-only.

Missing UUID (e.g. some network/FAT volumes) is allowed: such a volume is only
ever waited for, never remounted by us.

### Loop (only with `--auto-reconnect`)

When `copy_job` catches `StoragePaused`:

1. Classify the pause. Destination full / quota (`ENOSPC`, `EDQUOT`) is **not**
   reconnectable → exit 3 as today. Lost source or destination → continue.
2. Activity state `waiting` with reason, the attempt number, and the time the
   cooldown ends. `status --watch` and the GUI display it.
3. Cooldown (`--reconnect-cooldown`, default 300 s). Nothing touches the disk.
4. Wait phase, polling every 15 s for up to `--reconnect-wait` (default 1800 s):
   - Volume mounted at the recorded mount point with the recorded UUID → go to 5.
   - Volume present (UUID resolves via `diskutil info`) but not mounted, local
     filesystem → `diskutil mount readOnly <VolumeUUID>` for the source,
     `diskutil mount <VolumeUUID>` for the destination; then re-check.
   - Source volume mounted writable by macOS itself (normal auto-mount after it
     reappears), or mounted at a different mount point (e.g. `"/Volumes/Data 1"`
     because a stale mount point lingered): plain `diskutil unmount <UUID>`
     (never `force`), then `diskutil mount readOnly <UUID>` and re-check that it
     landed on the recorded mount point. If the plain unmount fails or the
     volume still lands elsewhere → stop with an explicit message; the operator
     decides. Paths are never auto-rerooted. (Owner decision 2026-10-22.)
   - Destination volume is never unmounted by the tool; only mounted if present
     and unmounted.
   - Recorded mount point still listed with the recorded UUID but I/O probes
     time out (stale mount): treat as "not available yet" and keep waiting; at
     the end of the wait phase stop with a "stale mount, reboot or replug" note.
   - Network filesystem or no UUID → only wait for the recorded path to come
     back.
   - Every `diskutil` call runs as a subprocess with a timeout (default 60 s);
     a hung call counts as "not available yet", never blocks the job forever.
5. Verify identity: UUID matches and the existing `storage_anchors` inode probe
   passes in the bounded worker. Mismatch → stop (never resume on a different
   volume).
6. Resume the same selection in-process (same phase, timeout, limit budget)
   holding the existing exclusive job lock throughout.

### File active during the outage

Today the active file returns to `pending` without consuming a retry. That stays
for the first loss. If storage is lost again while the **same** file is being
read, the file is marked `failed` with reason "storage lost twice while reading
this file" (that read attempt stays consumed), so the queue moves on and a bad region cannot
pin the whole night to one file. It stays retryable later. (Builder call.)

### Stop criteria (any one ends the run with exit 3 and an escalation note)

Limits are computed from the persisted `storage_events`, not per process, so a
restarted `resume --auto-reconnect` cannot reset them.

- `--max-reconnects` successful reconnects since the last operator
  acknowledgement (default 6).
- More than 3 disconnects within any rolling 60 minutes.
- No forward progress: two consecutive reconnect cycles each copied zero bytes
  of new data before the next disconnect.
- Wait phase expired without the volume coming back.
- Identity mismatch, different mount point, or volume needs unlocking.

After such a stop (`gave-up` event), `--auto-reconnect` refuses to loop again
until the operator acknowledges it (`--acknowledge-storage-loss` on the next
`copy`/`resume`, or a confirmation dialog in the GUI); without it the run behaves
as plain `copy`. (Builder call.)

On these stops `next` recommends: stop file-level copying and image the disk
(`ddrescue` with a mapfile) or escalate, consistent with
`docs/agent-runbook.md` "Stop Or Escalate".

### Records

New manifest table `storage_events` (timestamp, role source/dest, kind:
`lost` / `cooldown` / `remount-attempt` / `remounted` / `resumed` / `gave-up` / `stopped` /
`acknowledged`, detail). `stopped` (operator stop while waiting) is not a
give-up and never blocks a later auto-reconnect. Reported in `report` (technician) and summarized in `status`.
Customer report: one neutral line ("the source disk disconnected N times; the
rescue resumed automatically") only if N > 0.

### Interfaces

- CLI: `copy`/`resume` gain `--auto-reconnect`, `--reconnect-cooldown SECONDS`,
  `--reconnect-wait SECONDS`, `--max-reconnects N`. Default off, so existing
  scripts and the runbook behave exactly as today.
- GUI: "Automatically reconnect after disk loss" checkbox, default **on** in the
  GUI, plus a "Waiting for disk" state with countdown and attempt counter and a
  "Stop waiting" action that ends the run cleanly (exit 3, `stopped` recorded,
  no give-up block). The CLI maps SIGTERM to the same handling as SIGINT: while
  waiting it records `stopped` and exits 3; during an active copy pass it keeps
  today's `interrupted` path (exit 130). Implemented after the GUI
  console PR is merged, on top of its state model. (Builder call.)
- Docs: runbook section for overnight runs (cooling, power settings, what the
  loop can and cannot do), rescue-operations update.

## Constraints

- Python stdlib only; `diskutil` and `mount` are the only external tools.
- Source must never be written; mounting is read-only for the source.
- Parent-process filesystem calls on a vanished mount can hang in the kernel;
  all probes stay in the bounded worker or in subprocesses with timeouts.
- Tests cannot use real disks: `diskutil` access goes through a small adapter that
  tests replace with a fake (present/absent/mounted elsewhere/hung).

## Known limitation (must be stated to the owner and in the docs)

If the drive vanishes from the bus entirely (the USB-SATA bridge or the drive
firmware has given up), no software remount can bring it back; only a physical
replug or a power cycle can. In that case the loop waits, then stops with the
escalation message. It helps when the device re-enumerates on its own or stays
visible but unmounted.

## Left to the builder

- Module layout (e.g. `reconnect.py` + a `diskutil` adapter), exact table schema,
  polling interval details, message wording.
- Whether the remount check needs `-plist` parsing via `plistlib` (expected).
- Verifying on a real macOS volume that `diskutil mount readOnly` on an
  unmounted APFS/HFS+ volume does not trigger an fsck repair; if it can, use the
  safest available read-only mount path and document it.

## Open decisions (owner)

1. Optional user-supplied `--reconnect-hook CMD` run after the cooldown when the
   device is absent (e.g. a uhubctl or smart-plug script), counted against the
   same limits. Proposed: not in this iteration.
