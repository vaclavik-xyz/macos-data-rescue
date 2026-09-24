# Architecture research notes

## Research and verified constraints

- The project target is a macOS technician CLI for data rescue from damaged or unstable Macs mounted through Share Disk / Target Disk workflows.
- The tool should be deterministic; AI is orchestration only, not part of rescue behavior.
- Core architecture decision: file-by-file rescue with a SQLite manifest, resumable state, per-file isolation, and report output.
- Copying must run per file in an isolated child process so one stuck read does not stop the whole job.
- Timeout handling should kill/abandon a stuck worker and continue; unbounded waits after timeout are dangerous on failing disks.
- Destination writes should use collision-safe temp files in the destination directory, then publish via atomic `os.replace`.
- Symlinks should be skipped rather than followed, because following absolute symlinks can copy unrelated files from the technician host.
- `--limit` should count files actually needing work, not already matching copied rows; otherwise resume can starve later files.
- Manifest reads must not hold a SQLite cursor open while copy writes update statuses; batch reads avoid read/write self-locks on older/non-WAL manifests.
- On this macOS Python, `os.listxattr`, `os.getxattr`, and `os.setxattr` are absent, so xattr/resource fork preservation uses a native `ctypes`/libSystem backend instead of Python stdlib xattr calls.

## Resulting shipped contract

The MVP now ships:

- `init`, `scan`, `copy`, `resume`, `status`, `report` CLI commands.
- SQLite manifest under the job directory.
- Phase-based scanning: visible-home/hidden-home/app-data/applications/full-home, with legacy important/photos/library/all compatibility.
- Per-file child-process copy with timeout.
- Atomic temp-file destination writes.
- Symlink skip policy.
- Resume for failed/timed-out/copying rows and copied rows whose destination is missing.
- Markdown and JSON reports.
- Best-effort macOS xattr/resource fork copy with deliberate `com.apple.quarantine` and `com.apple.macl` skips plus visible xattr failure warnings.
- Regression tests for timeout/failure continuation, symlink safety, temp collision, streamed/batched manifest selection, xattr preservation, and resume limit starvation.

## Remaining hardening

1. Add destination free-space preflight with a conservative operator-facing warning before long copy runs.
2. Add timeout-guarded or interrupt-friendly scanning for severely failing disks where `os.walk`/`stat` can hang before copy starts.
3. Consider explicit retry policy flags for failed/timed-out rows after the MVP stabilizes.

## Review corrections (2026-09-24)

- Existing jobs keep their source, destination, profile, and creation time.
  Use a new job directory for a different source or destination.
- Job and destination must be disjoint. A destination must not contain the
  source; application scans also validate their additional source roots.
- Scan and copy writers use an advisory job lock. Do not run another tool
  that changes the source or destination while a rescue is running.
- Scan access errors stop the command without marking coverage complete.
  Committed batches remain copyable; correct access and repeat the scan.
- Cleanup only removes temporary files registered by this job with matching
  device/inode. Old unregistered `.rescue-tmp` files require technician review;
  filenames alone cannot distinguish leftovers from customer content.
- Rescanning unchanged rows retains copy warnings; changed content/kind/source
  resets retry attempts. Customer reports describe the recorded files and do
  not certify complete source coverage.
