# MVP slices and follow-up backlog

This document tracks the MVP that was implemented and the next hardening slices.

## Shipped MVP

- `init`: create a SQLite manifest under the job directory and reject job/destination paths inside the source tree.
- `scan`: walk the mounted source home or a single phase-scoped subset and classify files into customer-facing phases.
- Scan hardening: manifest rows are committed in batches, and `scan --timeout` / `scan --limit` can intentionally stop with a partial manifest ready for copy; repeated scans of the same phase resume from a saved per-phase cursor.
- Customer-facing recovery phases: `visible-home`, `hidden-home`, `app-data`, `applications`, and `full-home`, while preserving legacy `important`, `photos`, `library`, and `all`.
- `applications` can rescue source volume `/Applications` and `~/Applications` bundles into distinct destination prefixes with validated per-row `source_path`.
- `copy` / `resume`: copy one file at a time through a child process with per-file timeout.
- Atomic destination writes through unique temp files in the destination directory.
- Post-copy byte-count verification prevents files from being marked `copied` when the worker reads fewer bytes than the manifest expected.
- Stale internal temp cleanup before `copy` / `resume`, without touching source data or manifest-tracked hidden files.
- Symlink safety: symlinks are recorded and marked `skipped`, never followed.
- Resume semantics for failed/timed-out/copying rows and copied rows whose destination disappeared.
- `--limit` counts files that actually need work, not already-matching copied rows.
- `status` and `report --format markdown|json`.
- Regression test suite covering scan/excludes, phase-scoped scan, scan batching/timeout/limit/cursor resume, application roots, copy completeness, resume, timeout/failure continuation, symlink safety, temp cleanup/collision, streamed manifest selection, source write guards, safe metadata copy, exit codes, and limit starvation.
- Reports include general warnings plus per-file suspected iCloud dataless placeholder markers in Markdown and JSON, including macOS `SF_DATALESS` flag detection.
- macOS extended attributes and resource forks are copied best-effort through libSystem; `com.apple.quarantine` and `com.apple.macl` are skipped, and xattr failures become visible report warnings instead of failing content rescue.

## Verification gates

```bash
uv run pytest
```

Smoke example:

```bash
workdir=$(mktemp -d)
mkdir -p "$workdir/source/Desktop" "$workdir/source/Library/Caches"
printf visible > "$workdir/source/Desktop/a.txt"
printf cache > "$workdir/source/Library/Caches/skip.txt"
uv run macos-data-rescue init --job-dir "$workdir/job" --source "$workdir/source" --dest "$workdir/dest"
uv run macos-data-rescue scan --job-dir "$workdir/job" --phase visible-home --timeout 30
uv run macos-data-rescue copy --job-dir "$workdir/job" --phase visible-home --timeout 2
uv run macos-data-rescue status --job-dir "$workdir/job"
uv run macos-data-rescue report --job-dir "$workdir/job" --format json
```

## Post-MVP hardening backlog

1. Add destination free-space preflight with a conservative operator-facing warning before long copy runs.
2. Add a hard child-process scanner watchdog for severe kernel I/O hangs inside one `os.walk`/`stat` call.
3. Add an optional `--exclude-from` file for technician-maintained skip patterns.
4. Add `--retry-failed/--no-retry-failed` policy controls.
5. Add CSV report output for CRM import if useful.
6. Add an SSH orchestration wrapper for service Macs once the CLI stabilizes.
