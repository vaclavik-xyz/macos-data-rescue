# Restore Profile Design

Date: 2026-07-02
Status: approved (approach A chosen by Filip)

## Problem

The CLI covers the rescue direction: damaged customer disk -> service
recovery disk. Technicians also need the opposite direction: rescued data on
the service disk -> the customer's new disk. Running the existing
`customer-home` phases over a rescued tree almost works, but only
implicitly: scan excludes are re-applied (silently dropping anything a
technician added by hand, such as a folder named `Temp` or a project with
`node_modules`), and the behavior is not an explicit guarantee.

## Decision

Add a `restore` job profile that mirrors the rescued tree 1:1 with the
existing copy machinery. No separate restore command, no consumption of the
original rescue manifest (rejected as YAGNI), no runbook-only variant
(rejected as fragile).

## Requirements (confirmed with Filip)

- All three restore targets must work the same way:
  1. new Mac mounted via Share Disk / Target Disk Mode (copy into the
     customer's home),
  2. external disk handed to the customer (plain 1:1 copy),
  3. running the CLI directly on the new Mac with the service disk attached.
- `Volume Applications/` and `Home Applications/` in the rescued tree are
  copied as-is into the destination (no mapping back to `/Applications`).
- File ownership is handled as a documented post-restore step in the agent
  runbook (`diskutil resetUserPermissions` / `chown`), not by the tool; the
  CLI stays root-free.

## CLI surface

- `init --profile restore --job-dir JOB --source RESCUED_USER_DATA --dest NEW_HOME`
  - `--profile` choices become `("customer-home", "restore")`.
  - All existing init guards stay (source must exist, job-dir/dest must not
    be inside source, inode-based containment check).
- `scan --job-dir JOB` on a restore job scans the whole source tree. Any
  explicit `--phase` other than the default `all` — including
  `--phase restore` — is a `RescueError`; the restore profile has exactly
  one scan scope. (`--phase restore` remains meaningful for `copy`/`resume`,
  where it selects the restore rows.)
- Manifest rows are stored with `phase = "restore"`. `"restore"` is added to
  the CLI `PHASES` choices so `copy`/`resume --phase restore` select exactly
  those rows; the default `--phase all` also matches.
- `scan --phase restore` on a `customer-home` job is a `RescueError`.
- `copy`, `resume`, `status`, `report`, `customer-report` work unchanged.

## Scan semantics (the actual difference)

- Restore scan walks the source with the existing deterministic walker
  (sorted dirs/files, `followlinks=False`) but applies **no excludes**: no
  `is_excluded`, no customer ballast filtering. What exists on the service
  disk is what gets restored.
- Directory and file symlinks are recorded as `kind=symlink` rows exactly
  like the rescue direction (and later skipped by copy). A rescued tree
  normally contains none, but hand-added ones must not vanish silently.
- iCloud placeholder warning heuristics stay enabled; rescued placeholder
  files that kept their provider xattrs keep warning the technician.
- Scan cursor/resume, `--timeout`, `--limit`, and batch commits reuse the
  existing mechanism with cursor key `scan_cursor:restore`.

## Copy semantics

Unchanged: persistent worker, per-file timeout with kill+respawn, temp file
plus atomic `os.replace`, byte-count verification, xattr best effort,
resume verification with the 2-second mtime tolerance. Existing destination
files at the same paths are overwritten; restore targets a fresh home. The
non-regular-kind skip applies as in rescue.

## Documentation

- README: restore section (init/scan/copy example, 1:1 semantics, overwrite
  note).
- docs/agent-runbook.md: restore chapter with the three scenarios and the
  mandatory ownership post-step for the Share Disk/TDM scenario
  (`diskutil resetUserPermissions / <uid>` on the new Mac, or
  `chown -R uid:gid` of the restored home), plus the note that running
  directly on the new Mac requires Python 3.11+/uv there.

## Out of scope

- Empty directories: they are not recorded or recreated, matching the
  tool-wide MVP limitation. A rescued tree produced by this tool contains
  none (copy only creates parents of copied files); hand-added empty
  directories on the service disk are not restored, and the restore README
  section says so.
- Mapping applications back to `/Applications`.
- Ownership handling inside the tool (`--owner`).
- Restore-specific report wording.
- Consuming the original rescue manifest for a cross-job audit chain.

## Testing

TDD per behavior:

- `init --profile restore` succeeds and stores the profile.
- Restore scan records everything 1:1, including paths every rescue phase
  would exclude (`node_modules`, `.Trash`, top-level `Cache`,
  `Library/Caches`) and `Volume Applications/` content, with
  `phase="restore"`.
- Restore scan records symlinks as `kind=symlink`; copy skips them.
- `scan --phase visible-home` (any non-default phase) on a restore job
  fails with exit 1; `scan --phase restore` on a customer-home job fails.
- `copy`/`resume` round-trip: restored tree matches the rescued tree;
  resume re-copies nothing.
- Restore scan `--limit` resumes from the persisted cursor.
