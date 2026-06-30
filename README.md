# macOS Data Rescue

Private technician CLI for rescuing user data from damaged Macs mounted through Share Disk / Target Disk workflows.

The goal is simple: **one bad file must not stop the whole rescue**. The tool scans a mounted user home into a SQLite manifest, copies files one by one with per-file timeouts, records failures, and can be resumed safely.

## Current MVP

- `init` creates a rescue job manifest.
- `scan` records source files with phases and default excludes.
- `copy` / `resume` copy file-by-file through an isolated worker process.
- Per-file timeout marks stuck files as `timed_out` and continues.
- Symlinks are skipped instead of followed, to avoid copying unrelated technician-host paths.
- Destination writes use unique temp files in the destination directory, then atomic `os.replace`.
- `copy` / `resume` clean stale internal `*.rescue-tmp` files in relevant destination directories before copying.
- `status` prints manifest counts.
- `report` prints Markdown or JSON suitable for service notes, including per-file suspected iCloud placeholder warnings.

## Install / run locally

```bash
uv sync
uv run macos-data-rescue --help
```

No runtime dependencies are used; `pytest` is only a dev dependency.

## Typical service workflow

Assume the damaged Mac is mounted on a healthy service Mac as:

```text
/Volumes/Macintosh HD - Data/Users/customer
```

Create a job on the recovery SSD:

```bash
JOB="/Volumes/RecoverySSD/Customer/.rescue"
SRC="/Volumes/Macintosh HD - Data/Users/customer"
DST="/Volumes/RecoverySSD/Customer/user-data"

uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST"
uv run macos-data-rescue scan --job-dir "$JOB"
```

Copy the highest-value data first:

```bash
uv run macos-data-rescue copy --job-dir "$JOB" --phase important --timeout 30
uv run macos-data-rescue copy --job-dir "$JOB" --phase photos --timeout 60
uv run macos-data-rescue copy --job-dir "$JOB" --phase library --timeout 30
uv run macos-data-rescue resume --job-dir "$JOB" --phase all --timeout 30
```

Check progress and produce a report:

```bash
uv run macos-data-rescue status --job-dir "$JOB"
uv run macos-data-rescue report --job-dir "$JOB" --format markdown > "$JOB/report.md"
uv run macos-data-rescue report --job-dir "$JOB" --format json > "$JOB/report.json"
```

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Command completed. `copy` / `resume` still return `0` when individual files are `failed` or `timed_out`; check status/report for per-file results. |
| `1` | Job/runtime error such as an unsafe path, missing manifest, unreadable job config, or unexpected runtime failure. |
| `2` | CLI usage error from argparse, such as missing required options or invalid argument values. |

## Phases

Default `customer-home` profile:

| Phase | Includes |
|---|---|
| `important` | `Desktop`, `Documents`, `Downloads` |
| `photos` | `Pictures`, `Movies`, `Music` |
| `library` | selected `Library` data, excluding caches/logs |
| `all` | everything not excluded |

Default excludes include `.Trash`, `Library/Caches`, `Library/Logs`, `node_modules`, `.Spotlight-V100`, `.fseventsd`, `__pycache__`, and common cache folders.

## Notes / limitations

- Source data is never modified, and `init` refuses a job directory or destination inside the source tree.
- Empty directories are not recreated in the MVP.
- macOS extended attributes/resource forks are not reliably preserved by the current Python stdlib path on this macOS; a native xattr backend is planned.
- **iCloud / Optimize Mac Storage warning:** files offloaded by iCloud Drive or Photos may exist on the mounted disk only as dataless placeholders. Over Share Disk / Target Disk Mode they can copy as empty or tiny files and cannot be downloaded from the mounted volume. The report now marks specific files as `suspected iCloud dataless placeholder` when conservative path/xattr/size heuristics match; the customer must be told that those files may not have been physically present on disk, and recovery may require the live signed-in Mac or iCloud.com/export.
- Per-file timeouts protect the copy phase. The scan phase still walks/stats the mounted source directly, so a severe disk/kernel I/O hang can still stall scan.
- For true hardware/kernel I/O hangs, a killed child may not exit immediately; the parent records timeout and continues as far as the OS allows.
- This is not a forensic imaging tool. It is a practical technician rescue copier for mounted, unlocked user data.

## Development

```bash
uv run pytest
```

Before committing: run tests, commit with Conventional Commits, then run:

```bash
roborev wait
roborev show HEAD
```
