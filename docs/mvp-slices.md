# MVP slices and follow-up backlog

This document tracks the MVP that was implemented and the next hardening slices.

## Shipped MVP

- `init`: create a SQLite manifest under the job directory and reject job/destination paths inside the source tree.
- `scan`: walk the mounted source home and classify files into phases.
- `copy` / `resume`: copy one file at a time through a child process with per-file timeout.
- Atomic destination writes through unique temp files in the destination directory.
- Symlink safety: symlinks are recorded and marked `skipped`, never followed.
- Resume semantics for failed/timed-out/copying rows and copied rows whose destination disappeared.
- `--limit` counts files that actually need work, not already-matching copied rows.
- `status` and `report --format markdown|json`.
- Regression test suite covering scan/excludes, copy, resume, timeout/failure continuation, symlink safety, temp collision, streamed manifest selection, source write guards, safe metadata copy, and limit starvation.
- Reports include general warnings plus per-file suspected iCloud dataless placeholder markers in Markdown and JSON.

## Verification gates

```bash
uv run pytest
```

Smoke example:

```bash
workdir=$(mktemp -d)
mkdir -p "$workdir/source/Desktop" "$workdir/source/Library/Caches"
printf important > "$workdir/source/Desktop/a.txt"
printf cache > "$workdir/source/Library/Caches/skip.txt"
uv run macos-data-rescue init --job-dir "$workdir/job" --source "$workdir/source" --dest "$workdir/dest"
uv run macos-data-rescue scan --job-dir "$workdir/job"
uv run macos-data-rescue copy --job-dir "$workdir/job" --phase important --timeout 2
uv run macos-data-rescue status --job-dir "$workdir/job"
uv run macos-data-rescue report --job-dir "$workdir/job" --format json
```

## Post-MVP hardening backlog

1. Add macOS extended attributes/resource fork preservation via `ctypes`/libSystem instead of Python stdlib xattr APIs, which are not available on this macOS Python.
2. Sweep stale `*.rescue-tmp` files at the start of `copy`/`resume`.
3. Add timeout-guarded or interrupt-friendly scanning for severely failing disks where `os.walk`/`stat` can hang before copy starts.
4. Add an optional `--exclude-from` file for technician-maintained skip patterns.
5. Add `--retry-failed/--no-retry-failed` policy controls.
6. Add CSV report output for CRM import if useful.
7. Add an SSH orchestration wrapper for service Macs once the CLI stabilizes.
