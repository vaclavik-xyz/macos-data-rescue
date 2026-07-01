# macOS Data Rescue MVP plan

## Goal
Build a usable private MVP CLI for macOS service data rescue. It should copy a damaged/unstable mounted user home directory to a safe destination without one bad file stopping the whole job.

## MVP commands

```bash
macos-data-rescue init --job-dir <dir> --source <home> --dest <dest> [--profile customer-home]
macos-data-rescue scan --job-dir <dir> [--phase visible-home|hidden-home|app-data|applications|full-home|important|photos|library|all] [--timeout <seconds>] [--limit <n>]
macos-data-rescue copy --job-dir <dir> [--phase visible-home|hidden-home|app-data|applications|full-home|important|photos|library|all] [--timeout <seconds>] [--limit <n>]
macos-data-rescue resume --job-dir <dir> [same options as copy]
macos-data-rescue status --job-dir <dir>
macos-data-rescue report --job-dir <dir> [--format markdown|json]
macos-data-rescue customer-report --job-dir <dir> [--format markdown|pdf] [--language en|cs] [--output <path>]
```

## Data model
Use SQLite manifest in `<job-dir>/manifest.sqlite` with tables for job config and files.
Track relative path, optional source path, size, mtime, kind, phase, status, attempts, error, copied bytes, timestamps.
Statuses: `pending`, `copying`, `copied`, `failed`, `timed_out`, `skipped`.

## Copy behavior
- Source is read-only.
- Destination path mirrors source relative paths.
- Per-file copy runs in a child subprocess with a timeout so a stuck read can be killed.
- Copy to `.<filename>.rescue-tmp`, fsync/close where feasible, then atomic rename.
- Preserve mtime and mode; best-effort xattrs on macOS.
- On failure/timeout, record error and continue.
- Re-running should skip already copied files with matching size/mtime.

## Scan behavior
- Source is read-only.
- Scan commits manifest rows in batches so partial work survives interruption after a committed batch.
- `--timeout` stops cooperatively between files and prints `stopped=timeout`.
- `--limit` stops after a bounded number of scanned files and prints `stopped=limit`.
- A per-phase scan cursor is stored with committed batches, so repeating the same phase resumes after the last committed scanned file and clears the cursor after the phase completes.
- Severe kernel/filesystem hangs inside one `os.walk` or `stat` call can still stall scan; escalate to imaging or a future hard scanner watchdog if that happens.

## Profiles/phases
`customer-home` profile:
- visible-home: non-hidden top-level home data except Library, Applications, and clear cache/trash ballast
- hidden-home: top-level dotfiles/dotfolders excluding clear cache/package/temp ballast
- app-data: curated customer-relevant Library data excluding caches/logs/temp
- applications: source volume `/Applications` and `~/Applications` bundles, copied into distinct destination prefixes
- full-home: broad user home scan with safe cache/log/temp excludes
- legacy important/photos/library/all remain supported for older jobs and scripts

Default excludes: `.Trash`, `Library/Caches`, `Library/Logs`, common browser/app caches, node_modules, `.Spotlight-V100`, `.fseventsd`.

## Tests
- scan creates manifest with phases and excludes
- copy copies files and preserves content
- resume skips copied files
- timeout/failure marks one file failed/timed_out and continues
- report summarizes copied/failed/skipped bytes and paths
- customer-report writes an English or Czech customer-facing Markdown/PDF handoff report to the visible recovery root by default

## Done
- Private GitHub repo exists.
- CLI package is installable/runnable with `uv run macos-data-rescue`.
- Tests pass.
- Local smoke fixture demonstrates scan/copy/report.
- README explains real technician workflow for Share Disk mounted volumes.
