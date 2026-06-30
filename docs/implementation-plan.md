# macOS Data Rescue MVP plan

## Goal
Build a usable private MVP CLI for macOS service data rescue. It should copy a damaged/unstable mounted user home directory to a safe destination without one bad file stopping the whole job.

## MVP commands

```bash
macos-data-rescue init --job-dir <dir> --source <home> --dest <dest> [--profile customer-home]
macos-data-rescue scan --job-dir <dir> [--phase important|photos|library|all]
macos-data-rescue copy --job-dir <dir> [--phase important|photos|library|all] [--timeout <seconds>] [--limit <n>]
macos-data-rescue resume --job-dir <dir> [same options as copy]
macos-data-rescue status --job-dir <dir>
macos-data-rescue report --job-dir <dir> [--format markdown|json]
```

## Data model
Use SQLite manifest in `<job-dir>/manifest.sqlite` with tables for job config and files.
Track relative path, size, mtime, kind, phase, status, attempts, error, copied bytes, timestamps.
Statuses: `pending`, `copying`, `copied`, `failed`, `timed_out`, `skipped`.

## Copy behavior
- Source is read-only.
- Destination path mirrors source relative paths.
- Per-file copy runs in a child subprocess with a timeout so a stuck read can be killed.
- Copy to `.<filename>.rescue-tmp`, fsync/close where feasible, then atomic rename.
- Preserve mtime and mode; best-effort xattrs on macOS.
- On failure/timeout, record error and continue.
- Re-running should skip already copied files with matching size/mtime.

## Profiles/phases
`customer-home` profile:
- important: Desktop, Documents, Downloads
- photos: Pictures, Movies, Music
- library: selected Library data excluding caches/logs/temp
- all: everything not excluded

Default excludes: `.Trash`, `Library/Caches`, `Library/Logs`, common browser/app caches, node_modules, `.Spotlight-V100`, `.fseventsd`.

## Tests
- scan creates manifest with phases and excludes
- copy copies files and preserves content
- resume skips copied files
- timeout/failure marks one file failed/timed_out and continues
- report summarizes copied/failed/skipped bytes and paths

## Done
- Private GitHub repo exists.
- CLI package is installable/runnable with `uv run macos-data-rescue`.
- Tests pass.
- Local smoke fixture demonstrates scan/copy/report.
- README explains real technician workflow for Share Disk mounted volumes.
