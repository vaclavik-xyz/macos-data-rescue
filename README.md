<p align="center">
  <img src="assets/brand/Favicon.svg" width="144" height="144" alt="macOS Data Rescue icon">
</p>

<h1 align="center">macOS Data Rescue</h1>

<p align="center"><strong>Open-source technician CLI for rescuing user data from damaged Macs mounted through Share Disk / Target Disk workflows.</strong></p>

> [!CAUTION]
> This is a practical file-rescue tool, not a forensic imager. Test your workflow first, keep the source read-only, and use `ddrescue` or a professional recovery service when the hardware is failing. The software is provided without warranty; see [LICENSE](LICENSE).

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
- `copy` / `resume` clean only job-registered stale temporary files whose device/inode still match. Unregistered older temporary files are retained for technician review.
- `status` prints manifest counts; `--watch` adds current file, transferred bytes, speed, errors, and remaining known queue.
- `verify` checks destination SHA256 against digests captured during copying, without rereading source files.
- `find-duplicates` suggests metadata-matched alternatives for failed files; every fallback remains an explicit operator choice.
- Storage loss or a full destination pauses copying (exit 3); reconnect/free space and `resume`.
- Opt-in `--auto-reconnect` waits out a storage loss, remounts the same volume (source read-only) and resumes within persisted limits; see [Automatic reconnect](#automatic-reconnect).
- `preflight` runs the environment checks from the runbook (source readable and not `/`, job/dest outside the source, mount read-only state, write probes, free space vs remaining manifest bytes) and exits non-zero on failure.
- `next` inspects the manifest and prints the exact next recommended command, so the operator — human or agent — never has to track the workflow state machine; approval-gated phases are listed separately and are never auto-suggested as required.
- `approve` records the operator/customer decision for a gated phase (`app-data`, `applications`, `full-home`, legacy `library`) with a timestamp and optional `--by` name. `scan` refuses gated phases until approved — including the legacy default no-`--phase` scan, which reaches `~/Library` — and `copy`/`resume` refuse selections containing unapproved gated rows (covers manifests from older versions).
- `report` prints Markdown or JSON suitable for service notes, including per-file suspected iCloud placeholder warnings.
- `customer-report` writes an English or Czech customer-facing Markdown/PDF handoff report into the visible recovery root.
- `gui` (or `macos-data-rescue-gui`) opens a Tk technician console (standard library only, Czech default / English) that drives the same CLI commands with live progress, preflight results, consent dialogs and reports.

## Install

Clone the repository and install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/vaclavik-xyz/macos-data-rescue.git
cd macos-data-rescue
uv sync
```

After the repository is public, you can also install the tool directly from GitHub:

```bash
uv tool install git+https://github.com/vaclavik-xyz/macos-data-rescue.git
macos-data-rescue --help
macos-data-rescue-gui --help
```

The application has no runtime dependencies (Python standard library only). Development and security-check tools are isolated in the dev dependency group.

## Technician console (GUI)

On a freshly installed Mac, copy the repository folder to the Mac (for example from a USB drive; `git clone` itself needs the Xcode Command Line Tools) and run:

```bash
./setup-fresh-mac.sh            # or double-click macos-data-rescue.command in Finder
```

The script installs a pinned `uv` release into `~/.local/bin` if needed (no sudo, Homebrew or Xcode), runs `uv sync --locked --no-dev` with a uv-managed Python 3.11+ that includes Tk, and opens the console. Grant the Terminal app **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access) so the mounted customer home is readable.

From an existing checkout:

```bash
uv run macos-data-rescue gui --lang cs          # or: uv run macos-data-rescue-gui
uv run macos-data-rescue gui --job-dir "/Volumes/RecoverySSD/Customer/.rescue"
```

The console is a front end for the CLI, so every rule in this README and in [docs/agent-runbook.md](docs/agent-runbook.md) applies; read the runbook before working on real customer data. In addition:

- `preflight` runs before `init` and again before every `scan`, `copy` and `resume`; a failed check stops the operation and is shown on the Setup tab.
- An opened job shows its source, destination and profile read-only. While an operation runs, everything that could write to the job (init, preflight, consent, phase changes, another scan/copy) is locked.
- The recommended-step button runs only `scan`, `copy`, `resume`, `verify`, `status`, `report` and `customer-report`. When `next` asks for consent, it opens the consent dialog instead, which requires the approving person's name and an explicit confirmation; consent is never recorded automatically.
- **Stop** interrupts the running command (SIGINT, then SIGTERM/SIGKILL if it does not exit). Committed progress stays in the manifest; continue with the recommended step. The console stays locked until the process has really exited.
- Exit code 3 shows *storage unavailable*: reconnect the original disk (or free destination space), then press **Resume**, which reruns the paused copy with the same options after a fresh preflight.
- "Keep the Mac awake" runs `caffeinate -dims -w <pid>` bound to the running command only.

## CLI quick start

```bash
uv run macos-data-rescue --help
uv run macos-data-rescue preflight --job-dir "$JOB" --source "$SRC" --dest "$DST"
uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST"
uv run macos-data-rescue next --job-dir "$JOB"
```

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
uv run macos-data-rescue approve --job-dir "$JOB" --phase app-data --by "customer name"
uv run macos-data-rescue scan --job-dir "$JOB" --phase app-data --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase app-data --timeout 3600

uv run macos-data-rescue approve --job-dir "$JOB" --phase applications --by "customer name"
uv run macos-data-rescue scan --job-dir "$JOB" --phase applications --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase applications --timeout 3600
```

If the customer explicitly wants maximum practical home coverage and there is enough time/space, run full-home after the focused phases:

```bash
uv run macos-data-rescue approve --job-dir "$JOB" --phase full-home --by "customer name"
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

## Job profiles: customer-home, volume, restore

`macos-data-rescue` supports three dedicated job profiles passed via `--profile <name>` at `init` (or selected via radio buttons in the GUI Console). The profile determines the scan scope, default exclusion policies, custom exclusion support, and approval gating.

### Profile comparison

| Profile | Intended source | Scan scope / phases | Default exclusions | Custom `--exclude` | Approval gates | Destination write behavior |
|---|---|---|---|---|---|---|
| `customer-home` *(default)* | Mounted macOS user home (`/Users/username`) | Phased (`visible-home`, `hidden-home`, `app-data`, `applications`, `full-home`) | Yes (caches, trash, logs, temp, `.Spotlight*`, `node_modules`, etc.) | ❌ Not allowed | ✅ Required for `app-data`, `applications`, `full-home` | Safe write to temp + atomic replace |
| `volume` | External disk, partition, project folder, or Trash (`/.Trashes/501`) | Single scope (`volume` phase) | ❌ None (everything included by default) | ✅ Optional repeatable globs | ❌ None (unrestricted) | Safe write to temp + atomic replace |
| `restore` | Rescued data tree (`user-data`) on recovery disk | Single scope (`restore` phase) | ❌ None (1:1 mirror of rescued files) | ❌ Not allowed | ❌ None (unrestricted) | Overwrites existing files at matching destination paths |

---

### 1. `customer-home` Profile (Default)

The `customer-home` profile is engineered specifically for rescuing macOS user home folders (e.g. `/Volumes/Macintosh HD - Data/Users/customer`). Because a user home contains gigabytes of ephemeral browser caches, transient logs, virtual environments, and complex application support trees, this profile uses a **prioritized, phased rescue workflow**:

#### Phased execution order
- **`visible-home` (Ungated, recommended first):**  
  Rescues standard visible user directories (`Desktop`, `Documents`, `Downloads`, `Pictures`, `Music`, `Movies`) and all customer-created non-hidden folders. Skips `~/Library`, `~/Applications`, `.Trash`, and cache ballast. Rescues the files the customer cares about most with minimal risk of stalling on broken system or cache files.
- **`hidden-home` (Ungated, recommended second):**  
  Rescues top-level configuration dotfiles and dotfolders (e.g. `.ssh`, `.gitconfig`, `.zshrc`, shell history), with obvious package ballast (e.g. `.npm`, `.cache`) pruned.
- **`app-data` (Approval-gated):**  
  Rescues curated, high-value customer data from `~/Library`: Apple Mail (`Mail`), iMessage/SMS attachments (`Messages`), Safari history and bookmarks (`Safari`), Keychain databases (`Keychains`), iOS device backups (`Application Support/MobileSync`), Notes, and essential application support folders.
- **`applications` (Approval-gated):**  
  Rescues application bundles (`.app`) from the source volume root `/Applications` and user `~/Applications` into separate archive folders: `Volume Applications/` and `Home Applications/`.
- **`full-home` (Approval-gated):**  
  Broad traversal across the entire home folder (visible, hidden, and complete `~/Library`), while still enforcing safe cache/log/temp exclusion rules.

#### Default exclusions
The `customer-home` profile automatically filters out disposable system and application ballast:
- `.Trash`, `.Trashes`, `.fseventsd`, `.Spotlight-V100`
- `Library/Caches`, `Library/Logs`, `Library/Saved Application State`
- Virtual environments (`.venv`), `node_modules`, `__pycache__`, `.pytest_cache`
- Browser and media caches (Google Chrome cache, Spotify cache, etc.)

> [!NOTE]
> Custom `--exclude` arguments are **not permitted** in `customer-home` mode (raises `RescueError`). This ensures deterministic behavior and reliable customer handoff reports.

#### Approval gating
`app-data`, `applications` and `full-home` (and the legacy `library` scope) reach private application data such as Mail, Messages and Keychains or large application trees, so they need the customer's decision first. `scan` refuses these phases and `copy`/`resume` refuse selections containing their rows until the decision is recorded. Ask the customer first; never approve on their behalf:

```bash
uv run macos-data-rescue approve --job-dir "$JOB" --phase app-data --by "Customer Consent via Phone"
```

In the GUI, **Record consent…** opens a dialog that requires the approving person's name and an explicit confirmation.

#### Customer handoff reporting
Jobs created with `customer-home` generate comprehensive, customer-friendly PDF and Markdown reports (`customer-report`) with a visual breakdown of recovered folders and Library data in English or Czech (`--language cs`).

---

### 2. `volume` Profile

The `volume` profile is designed for rescuing non-home sources, such as an entire external disk (`/Volumes/CustomerDisk`), a secondary data partition, an isolated project repository, or recovery directories such as `/Volumes/CustomerDisk/.Trashes/501`.

#### Key characteristics:
- **Single scan scope:** Traverses the entire source tree in one run. Run `scan` without `--phase` (or default `all`); all manifest rows are tagged with phase `volume`. Copy or resume using `--phase volume` or `--phase all`.
- **Zero default exclusions:** Unlike `customer-home`, the `volume` profile does **not** prune caches, Trash, `node_modules`, or backup directories (`Backups.backupdb`). On a secondary drive or recovered trash folder, what looks like "cache" or "trash" might be the exact lost data the customer needs!
- **No approval gates:** Scanning and copying proceed directly without requiring `approve` commands.

#### CLI usage example:
```bash
SRC="/Volumes/CustomerDisk/.Trashes/501"
DST="/Volumes/RecoverySSD/Customer/recovered-trash"
JOB="/Volumes/RecoverySSD/Customer/.trash-rescue"

# 1. Preflight safety check
uv run macos-data-rescue preflight --job-dir "$JOB" --source "$SRC" --dest "$DST"

# 2. Initialize volume job (with optional custom exclusions)
uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST" --profile volume \
  --exclude '.Spotlight*' --exclude 'Backups.backupdb*'

# 3. Scan and copy
uv run macos-data-rescue scan --job-dir "$JOB" --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --timeout 3600
uv run macos-data-rescue status --job-dir "$JOB"
```

Put `JOB` and `DST` in disjoint directories on the recovery disk. Existing destination files at matching paths are replaced. **Empty directories are not recreated; symlinks and special files are recorded and skipped.** Ownership, bootability and a complete disk image are not recreated — the result is neither a bootable volume nor a filesystem image.

In the GUI, select **Disk / folder (volume)**; an exclude field (comma-separated globs) appears.

#### Custom repeatable exclusions (`--exclude`)
When using `--profile volume`, operators can specify custom exclusion globs during `init`:
- Patterns are case-sensitive globs matched against source-relative paths (without leading `/`).
- Asterisks (`*`) can match across directory separators `/`.
- When a directory path matches an exclude pattern, its entire subtree is pruned during scan.
- Exclusions are permanently recorded in `manifest.sqlite` and the JSON audit report. They are immutable for the lifetime of the job; re-initializing with different excludes is rejected.

---

### 3. `restore` Profile

The `restore` profile handles the final migration step: mirroring rescued data from the technician recovery drive back onto the customer's fresh or repaired Mac, replacement internal drive, or clean external volume.

#### Key characteristics:
- **1:1 Mirror (zero exclusions):** The restore scan walks the rescued directory (`user-data`) with zero filters. Every file present on the recovery disk is scheduled for restoration. Custom `--exclude` flags are not permitted.
- **Single scope:** Scans the whole tree without `--phase` (or default `all`); all manifest rows are tagged with phase `restore`. Copy or resume using `--phase restore` or `--phase all`.
- **Destination overwriting:** Existing destination files at matching relative paths are overwritten. Restoring should always target a fresh user home folder or clean target directory.
- **Tool-wide limits:** empty directories are not recreated and symbolic links are recorded as `skipped`; check `report` before handoff. Do not generate `customer-report` from a restore job — the customer report comes from the rescue job.
- **Application archives restored as data:** `Volume Applications/` and `Home Applications/` in the rescued tree are mirrored as plain data archive folders; they do not automatically install back into `/Applications`. Applications should be properly reinstalled on the new macOS installation.

#### CLI usage example:
```bash
JOB="/Volumes/RecoverySSD/Customer/.restore"
SRC="/Volumes/RecoverySSD/Customer/user-data"
DST="/Volumes/New Mac/Users/customer"

# 1. Preflight safety check
uv run macos-data-rescue preflight --job-dir "$JOB" --source "$SRC" --dest "$DST"

# 2. Initialize restore job
uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST" --profile restore

# 3. Scan and copy
uv run macos-data-rescue scan --job-dir "$JOB" --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --timeout 3600

# 4. Verify completion
uv run macos-data-rescue status --job-dir "$JOB"
uv run macos-data-rescue resume --job-dir "$JOB"  # Must print processed=0
```

#### Crucial post-restore ownership and permissions
When rescuing or restoring over Share Disk / Target Disk Mode, all files created on the destination disk are owned by the **technician service account's UID/GID**, not the customer's account on the new Mac.

> [!IMPORTANT]
> Before handing the Mac back to the customer, you **must reset file permissions** on the new Mac. If you skip this, macOS will lock the customer out of their own restored files.
>
> On the customer's new Mac, open Terminal as admin and run:
> ```bash
> # Option A (macOS recommended):
> diskutil resetUserPermissions / $(id -u customer)
>
> # Option B (direct chown):
> sudo chown -R customer:staff /Users/customer
> ```
> *(Replace `customer` with the customer's actual short username).*

---

### Profile immutability and job safety

To prevent accidental data corruption or bypassing security gates:
- A job's profile is permanently committed to `manifest.sqlite` during `init`.
- If you re-run `init` pointing to an existing job directory, the profile and custom exclusions must match the existing job configuration. Attempting to switch profiles (e.g. from `customer-home` to `restore`) will exit with an error.
- To use a different profile or change exclusion patterns, create a fresh job directory (e.g. `.rescue-volume` or `.restore`).

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Command completed. `copy` / `resume` still return `0` when individual files are `failed` or `timed_out`; check status/report for per-file results. |
| `1` | Job/runtime error such as an unsafe path, missing manifest, unreadable job config, or unexpected runtime failure. |
| `2` | CLI usage error from argparse, such as missing required options or invalid argument values. |
| `3` | `copy` / `resume` paused: storage unavailable, destination full, or `--auto-reconnect` gave up or was stopped while waiting. Unattempted rows stay pending; reconnect the original disks or free space, then run `next` and `resume` with the same options. |
| `130` | `copy` / `resume` interrupted (Ctrl-C / SIGTERM) during a copy pass; `resume` continues. |

## Automatic reconnect

Off by default. Overnight runs on a disk that keeps dropping off the bus can use:

| Flag | Default | Meaning |
|---|---:|---|
| `--auto-reconnect` | off | Wait, cool down, remount the recorded volume (source `readOnly`, never `force`, destination never unmounted), verify the Volume UUID, resume. |
| `--reconnect-cooldown SECONDS` | 300 | Leave the disk alone after a loss. |
| `--reconnect-wait SECONDS` | 1800 | How long to wait for the volume after the cooldown. |
| `--max-reconnects N` | 6 | Successful reconnects allowed since the last acknowledgement. |
| `--acknowledge-storage-loss` | off | Re-enable the loop after a give-up (`next` recommends imaging first). |

More than 3 disconnects within 60 minutes or two reconnects without new data also stop the loop. A drive that vanished from the bus needs a physical replug. Read [docs/agent-runbook.md](docs/agent-runbook.md) "Overnight runs and automatic reconnect" first.

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
| `volume` | volume-profile jobs only: every file in the source tree (subject to any custom `--exclude` globs) |

Re-running `scan` for another phase upserts rows into the same manifest without deleting earlier phase results. Re-running the same phase after `stopped=timeout` or `stopped=limit` resumes after that phase's saved scan cursor; when the phase reaches the end, the cursor is cleared so future scans refresh from the beginning.

**Overlapping phases relabel rows:** when a later scan covers a file that an earlier phase already recorded (for example `full-home` after `visible-home`), the row's `phase` column is updated to the later phase. Copy status and results are preserved, but `copy --phase visible-home` no longer selects those rows; use the later phase (or `--phase all`) for follow-up copy/resume runs.

`scan --timeout SECONDS` bounds the overall traversal and prints `stopped=timeout`. `--io-timeout SECONDS` bounds each listing/stat operation (default 30); failed paths are recorded while other siblings continue. `scan --limit N` records at most `N` scan results for that run and prints `stopped=limit` when the run reached that cap. Both modes commit rows in batches, so `copy` can start from the partial manifest.

Default excludes include `.Trash`, `Library/Caches`, `Library/Logs`, `node_modules`, `.Spotlight-V100`, `.fseventsd`, `__pycache__`, and common cache folders.

## Notes / limitations

- Source data is never modified, and `init` refuses a job directory or destination inside the source tree.
- Empty directories are not recreated in the MVP.
- Hard links are not detected; each linked path is copied as an independent full copy, which can make the destination larger than the source.
- macOS extended attributes/resource forks are preserved best-effort through libSystem. The copier deliberately skips `com.apple.quarantine` and `com.apple.macl`; other xattr failures are reported as per-file warnings. `copied` guarantees content byte-count completeness, not full metadata preservation.
- **Copy completeness:** `copied` means the worker copied the same byte count that was recorded in the manifest for that file. If the worker reads fewer bytes, the file is marked `failed`, any partial temp output is discarded, and JSON/Markdown reports show the partial byte count.
- **iCloud / Optimize Mac Storage warning:** files offloaded by iCloud Drive or Photos may exist on the mounted disk only as dataless placeholders. Over Share Disk / Target Disk Mode they can copy as empty or tiny files and cannot be downloaded from the mounted volume. The report marks specific files as `suspected iCloud dataless placeholder` when the macOS dataless file flag or conservative path/xattr/size heuristics match; the customer must be told that those files may not have been physically present on disk, and recovery may require the live signed-in Mac or iCloud.com/export.
- Scan traversal and copy reads use isolated workers. Scan path validation and copy destination setup still involve parent-process filesystem calls; severe kernel I/O hangs there can still stall the command. See [rescue operations](docs/rescue-operations.md) for exact timeout and pause behavior.
- For true hardware/kernel I/O hangs, a killed child may not exit immediately; the parent records timeout and continues as far as the OS allows.
- This is not a forensic imaging tool. It is a practical technician rescue copier for mounted, unlocked user data.

## Unreadable compressed files and recovery from a duplicate

When a source read fails with `ENOTSUP`, the copy worker checks `UF_COMPRESSED`
and the hidden `com.apple.decmpfs` metadata. If the flag is set and metadata
is missing/unreadable, it records `unreadable-compressed-flag`. Detection
runs inside the per-file timeout, at the first read failure; it does not add
extra scan reads. The destination is not published on failure, and no
zero-filled substitute is produced. Status and technician/customer reports
count these separately. The result means the compressed content is not
addressable on this mounted source; it does not diagnose bad sectors or
prove that another copy cannot recover the file.

If a technician identifies a readable duplicate **inside the same source**,
select the failed row and alternate file explicitly:

```bash
uv run macos-data-rescue copy --job-dir "$JOB" \
  --path 'damaged-folder/photo.heic' \
  --fallback-from 'readable-folder/photo.heic' --timeout 300
```

Both paths are relative to the job's source (the original `--path` is the
exact manifest path). Only that failed/timed-out/unreadable regular-file row
is attempted. No automatic duplicate matching is performed. The alternate
must not traverse symlinks. The usual expected byte-count check and atomic
publication apply; equal size alone is not proof that the alternate is the
same content. Verify identity using independent evidence before selecting it.

Success is `copied_from_fallback`. The manifest and JSON/Markdown reports
retain the alternate path, original error, and alternate modification time.
Customer reports count it as recovered and identify the alternate-source
count separately. Resume uses the persisted alternate mapping, including
when the destination is missing; rescanning changed original content clears
the mapping. A failed alternate attempt keeps the old destination intact and
retains its mapping for retry. Start a new job to reset an unwanted mapping.

## Development

```bash
uv sync --locked
uv run ruff check src tests
uv run bandit -q -r src
uv run pytest
uv build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes. Never attach real customer manifests, reports, paths, filenames, or recovered data to an issue or test fixture. Report security problems privately as described in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

Before committing: run tests, commit with Conventional Commits, then run:

```bash
roborev wait
roborev show HEAD
```
