# macOS Data Rescue

Private technician CLI for rescuing user data from damaged Macs mounted through Share Disk / Target Disk workflows.

The goal is simple: **one bad file must not stop the whole rescue**. The tool scans a mounted user home into a SQLite manifest, copies files one by one with per-file timeouts, records failures, and can be resumed safely.

## Current MVP

- `init` creates a rescue job manifest.
- `scan` records source files with phases and default excludes; `--phase`, `--timeout`, and `--limit` can rescue visible home data before slower Library/app/application choices.
- Scan commits manifest rows in batches and stores a per-phase cursor, so an interrupted or timeout/limit-bounded scan can leave useful rows ready for `copy` and the next scan of the same phase continues forward.
- `copy` / `resume` copy file-by-file through an isolated worker process.
- Per-file timeout marks stuck files as `timed_out` and continues.
- Symlinks are skipped instead of followed, to avoid copying unrelated technician-host paths.
- Other non-regular files (FIFOs, sockets, devices) are recorded and skipped instead of copied, so they cannot stall the copy phase.
- Destination writes use unique temp files in the destination directory, then atomic `os.replace`.
- A file is marked `copied` only after the worker copies the expected byte count from the manifest.
- macOS extended attributes and resource forks are copied best-effort with a native libSystem xattr backend.
- `copy` / `resume` clean stale internal `*.rescue-tmp` files in relevant destination directories before copying.
- `status` prints manifest counts.
- `preflight` runs the environment checks from the runbook (source readable and not `/`, job/dest outside the source, mount read-only state, write probes, free space vs remaining manifest bytes) and exits non-zero on failure.
- `next` inspects the manifest and prints the exact next recommended command, so the operator — human or agent — never has to track the workflow state machine; approval-gated phases are listed separately and are never auto-suggested as required.
- `report` prints Markdown or JSON suitable for service notes, including per-file suspected iCloud placeholder warnings.
- `customer-report` writes an English or Czech customer-facing Markdown/PDF handoff report into the visible recovery root.

## Install / run locally

```bash
uv sync
uv run macos-data-rescue --help
```

No runtime dependencies are used; `pytest` is only a dev dependency.

## Typical service workflow

Before using the CLI on real customer data, read [docs/agent-runbook.md](docs/agent-runbook.md).

The workflow below can also be driven step-by-step: run `preflight` before `init`, then after every command run `next --job-dir "$JOB"` and execute what it prints. Phases marked "ask first" still require operator/customer approval.

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
uv run macos-data-rescue scan --job-dir "$JOB" --phase visible-home --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase visible-home --timeout 3600
```

If scan prints `stopped=timeout` or `stopped=limit`, the manifest still contains the rows committed so far. Start `copy` for that phase, then repeat the same `scan --phase ...` later; it resumes after the last committed scan cursor for that phase until the phase completes.

Then automatically include hidden home dotfiles/dotfolders, with obvious cache ballast pruned:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase hidden-home --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase hidden-home --timeout 3600
```

Ask the operator/customer before slower or less portable scopes:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase app-data --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase app-data --timeout 3600

uv run macos-data-rescue scan --job-dir "$JOB" --phase applications --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase applications --timeout 3600
```

If the customer explicitly wants maximum practical home coverage and there is enough time/space, run full-home after the focused phases:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase full-home --timeout 300
uv run macos-data-rescue resume --job-dir "$JOB" --phase all --timeout 3600
```

Check progress and produce a report:

```bash
uv run macos-data-rescue status --job-dir "$JOB"
uv run macos-data-rescue customer-report --job-dir "$JOB" --format pdf
uv run macos-data-rescue customer-report --job-dir "$JOB" --format markdown
uv run macos-data-rescue customer-report --job-dir "$JOB" --format pdf --language cs
uv run macos-data-rescue customer-report --job-dir "$JOB" --format markdown --language cs
uv run macos-data-rescue report --job-dir "$JOB" --format markdown > "$JOB/report.md"
uv run macos-data-rescue report --job-dir "$JOB" --format json > "$JOB/report.json"
```

`customer-report` writes a customer-facing handoff report to the recovery root by default, next to `user-data/` as `recovery-report.pdf` or `recovery-report.md`. English is the default. Use `--language cs` for Czech output, which defaults to `recovery-report-cs.pdf` or `recovery-report-cs.md` so it does not overwrite the English report. The PDF includes a summary, copied-data status, top-level recovered folder breakdown, and Library-area breakdown when applicable. The detailed `report` command stays on stdout and is intended for technician notes or `.rescue` artifacts.

## Restore to the customer's new disk

The same CLI handles the opposite direction: rescued data on the service disk -> the customer's new disk. A `restore` job mirrors the rescued tree 1:1 — no excludes are applied, and `Volume Applications/` / `Home Applications/` folders are copied as-is.

```bash
JOB="/Volumes/RecoverySSD/Customer/.restore"
SRC="/Volumes/RecoverySSD/Customer/user-data"
DST="/Volumes/Novy Mac/Users/customer"

uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST" --profile restore
uv run macos-data-rescue scan --job-dir "$JOB"
uv run macos-data-rescue copy --job-dir "$JOB" --timeout 3600
uv run macos-data-rescue status --job-dir "$JOB"
```

Restore jobs have a single scope: `scan` takes no `--phase` and records all rows with phase `restore` (`copy`/`resume` may use `--phase restore` or the default `all`). Existing destination files at the same paths are overwritten — restore into a fresh home folder. Two tool-wide limitations still apply: empty directories are not recreated (a rescued tree produced by this tool contains none), and symbolic links are recorded as `skipped` instead of recreated — check `report` before handoff. Files written over Share Disk / Target Disk Mode are owned by the service account; fix ownership on the new Mac afterwards (see [docs/agent-runbook.md](docs/agent-runbook.md)).

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
| `visible-home` | non-hidden top-level home data except `Library`, `Applications`, and clear cache/trash ballast; includes standard folders and customer-created folders |
| `hidden-home` | top-level dotfiles/dotfolders, excluding clear cache/package/temp ballast |
| `app-data` | curated customer-relevant `~/Library` data such as Mail, Messages, Safari, Keychains, MobileSync backups, and selected Application Support |
| `applications` | application bundles from the source volume `/Applications` and `~/Applications`, copied under `Volume Applications/` and `Home Applications/` |
| `full-home` | visible, hidden, and Library content under the home folder, still applying safe cache/log/temp excludes |

Legacy phases remain available for existing jobs and older scripts:

| Legacy phase | Includes |
|---|---|
| `important` | `Desktop`, `Documents`, `Downloads` |
| `photos` | `Pictures`, `Movies`, `Music` |
| `library` | selected `Library` data, excluding caches/logs |
| `all` | all manifest rows for copy/resume; default scan keeps the original full-home behavior |
| `restore` | restore-profile jobs only: every file in the rescued tree, 1:1, no excludes |

Re-running `scan` for another phase upserts rows into the same manifest without deleting earlier phase results. Re-running the same phase after `stopped=timeout` or `stopped=limit` resumes after that phase's saved scan cursor; when the phase reaches the end, the cursor is cleared so future scans refresh from the beginning.

**Overlapping phases relabel rows:** when a later scan covers a file that an earlier phase already recorded (for example `full-home` after `visible-home`), the row's `phase` column is updated to the later phase. Copy status and results are preserved, but `copy --phase visible-home` no longer selects those rows; use the later phase (or `--phase all`) for follow-up copy/resume runs.

`scan --timeout SECONDS` stops cooperatively between files and prints `stopped=timeout`. `scan --limit N` records at most `N` scan results for that run and prints `stopped=limit` when the run reached that cap. Both modes commit rows in batches, so `copy` can start from the partial manifest.

Default excludes include `.Trash`, `Library/Caches`, `Library/Logs`, `node_modules`, `.Spotlight-V100`, `.fseventsd`, `__pycache__`, and common cache folders.

## Notes / limitations

- Source data is never modified, and `init` refuses a job directory or destination inside the source tree.
- Empty directories are not recreated in the MVP.
- Hard links are not detected; each linked path is copied as an independent full copy, which can make the destination larger than the source.
- macOS extended attributes/resource forks are preserved best-effort through libSystem. The copier deliberately skips `com.apple.quarantine` and `com.apple.macl`; other xattr failures are reported as per-file warnings. `copied` guarantees content byte-count completeness, not full metadata preservation.
- **Copy completeness:** `copied` means the worker copied the same byte count that was recorded in the manifest for that file. If the worker reads fewer bytes, the file is marked `failed`, any partial temp output is discarded, and JSON/Markdown reports show the partial byte count.
- **iCloud / Optimize Mac Storage warning:** files offloaded by iCloud Drive or Photos may exist on the mounted disk only as dataless placeholders. Over Share Disk / Target Disk Mode they can copy as empty or tiny files and cannot be downloaded from the mounted volume. The report marks specific files as `suspected iCloud dataless placeholder` when the macOS dataless file flag or conservative path/xattr/size heuristics match; the customer must be told that those files may not have been physically present on disk, and recovery may require the live signed-in Mac or iCloud.com/export.
- Per-file timeouts protect the copy phase. Scan `--timeout` is cooperative between files and scan commits rows in batches, but a severe disk/kernel I/O hang inside one filesystem call can still stall scan.
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
