# Agent Runbook: Customer Data Rescue

This runbook is for agents or technicians using `macos-data-rescue` on real customer data. The tool is a practical technician rescue copier for mounted, unlocked macOS user data. It is not a forensic imager and does not replace `ddrescue` or professional clean-room recovery for failing hardware.

## Scope

Use this tool when:

- The customer Mac or disk is mounted on a healthy service Mac through Share Disk, Target Disk Mode, or an external enclosure.
- FileVault is unlocked and the target user home is readable.
- The goal is to rescue user files while one bad file, timeout, or metadata failure must not stop the whole run.

Do not use this tool as the first response when the disk is disappearing, making severe hardware noises, producing broad I/O errors, or must be preserved for forensic evidence. Stop and image/escalate instead.

## Preflight

- Confirm FileVault is unlocked and the source user home is mounted.
- Put both the job directory and destination on the recovery disk, never under the source.
- Prefer a read-only source mount when possible. If not possible, treat the source as read-only operationally.
- Grant the terminal app Full Disk Access on the service Mac.
- Confirm the recovery disk has enough free space for expected rescued data plus reports.
- Close Finder windows, previews, Spotlight searches, photo apps, and any tool that may write metadata to the source.
- Do not run cleanup, delete, repair, indexing, or metadata-modifying commands on the source.
- Record the exact source, destination, and job paths in service notes before starting.

## Intake Questions

Confirm these before starting a real customer run:

- What is the exact mounted source user home path?
- What recovery disk path should hold `JOB` and `DST`?
- Is the default scope acceptable: `visible-home` plus `hidden-home`?
- Should the run include app data from `~/Library` such as Mail, Messages, Safari, Notes, Keychains, or iPhone/iPad backups?
- Should the run include application bundles from source volume `/Applications` and `~/Applications`?
- Is a broader `full-home` attempt requested after focused recovery, and is there enough destination space/time?
- Does the source look unstable enough to stop and image/escalate before file-level copy?

Run the built-in preflight instead of composing shell checks by hand:

```bash
SRC="/Volumes/Macintosh HD - Data/Users/customer"
DST="/Volumes/RecoverySSD/Customer/user-data"
JOB="/Volumes/RecoverySSD/Customer/.rescue"

uv run macos-data-rescue preflight --job-dir "$JOB" --source "$SRC" --dest "$DST"
```

It verifies the source exists, is readable and is not `/`, that `JOB` and
`DST` are outside the source, reports the source mount's read-only state,
write-probes `JOB` and `DST` (never the source), and checks dest free space.
Exit code 1 means fix the failures before continuing. On an existing job,
run it with `--job-dir` only; it then also compares free space against the
bytes the manifest still needs. Manual fallback checks:

```bash
test -d "$SRC"
test "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$SRC")" != "/"
df -h "$(dirname "$DST")" "$(dirname "$JOB")"
mount | grep -F "/Volumes/Macintosh HD"
```

## Source Safety Rules

- Never create `JOB` or `DST` inside `SRC`.
- Never delete, move, rename, chmod, chown, xattr-write, or cleanup anything under `SRC`.
- Never run `rm`, `find -delete`, `xattr -d`, `chflags`, `diskutil repairVolume`, or `mdutil` against the source during rescue.
- If a command would write to the source, stop and change the plan.
- Destination writes are allowed only under `DST` and `JOB`.

The CLI also refuses job/destination paths inside source, but operators must still verify paths before running commands.

## Guided Mode For Agents

After `init`, you do not have to track the workflow state yourself. After
every command, run:

```bash
uv run macos-data-rescue next --job-dir "$JOB"
```

and execute exactly what it prints. `next` applies copy-first ordering
(committed rows are rescued before more scanning), resumes interrupted
scans, and stops retrying files whose failures are exhausted. Phases listed
under `ask-before:` are enforced, not advisory: `scan`/`copy` refuse them
until the operator/customer decision is recorded with
`approve --job-dir "$JOB" --phase <phase> --by "name"`. Ask the customer,
record the decision, and only then continue — never approve on your own.
`next` works for both rescue and restore jobs; the manual sequence below
documents what it will walk you through.

## Command Sequence

Install/sync dependencies from the repo checkout on the service Mac:

```bash
cd /path/to/macos-data-rescue
uv sync
```

Set paths. Adjust these before running:

```bash
JOB="/Volumes/RecoverySSD/Customer/.rescue"
SRC="/Volumes/Macintosh HD - Data/Users/customer"
DST="/Volumes/RecoverySSD/Customer/user-data"
```

Initialize the manifest:

```bash
uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST"
```

Rescue normal visible home data first. This includes standard folders and customer-created non-hidden folders, while skipping `Library`, `Applications`, and obvious cache/trash ballast:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase visible-home --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase visible-home --timeout 3600
```

If scan reports `stopped=timeout` or `stopped=limit`, start copying the rows already committed, then repeat the same `scan --phase ...` later. The manifest stores a per-phase scan cursor, so the next scan continues after the last committed file for that phase until the phase reaches the end. Copy jobs can run for hours; the timeout is per file, not a whole-job timer.

Then copy hidden home dotfiles/dotfolders. This is part of the default customer-home workflow because hidden home data can matter for ordinary users too:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase hidden-home --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase hidden-home --timeout 3600
```

The phases below are gated: `scan` (and `copy`/`resume` selections that include their rows) refuse to run until the operator/customer decision is recorded with `approve`. Ask first, then record who approved:

Run `app-data` only after explicit approval:

```bash
uv run macos-data-rescue approve --job-dir "$JOB" --phase app-data --by "customer name"
uv run macos-data-rescue scan --job-dir "$JOB" --phase app-data --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase app-data --timeout 3600
```

Run `applications` only after explicit approval:

```bash
uv run macos-data-rescue approve --job-dir "$JOB" --phase applications --by "customer name"
uv run macos-data-rescue scan --job-dir "$JOB" --phase applications --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase applications --timeout 3600
```

Run `full-home` only after focused recovery, if maximum practical coverage is requested and space/time allow:

```bash
uv run macos-data-rescue approve --job-dir "$JOB" --phase full-home --by "customer name"
uv run macos-data-rescue scan --job-dir "$JOB" --phase full-home --timeout 300
uv run macos-data-rescue resume --job-dir "$JOB" --phase all --timeout 3600
```

If the `applications` phase also ran, user applications from `~/Applications` end up twice under `DST` (`Home Applications/` from the applications phase and `Applications/` from full-home). This overlap is expected; account for it when estimating destination space.

Legacy phase names remain supported for older jobs and scripts. `important` and `photos` are ungated; `library` and the default no-`--phase` scan reach `~/Library` and therefore require the `library` approval first:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase important --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase important --timeout 3600
uv run macos-data-rescue scan --job-dir "$JOB" --phase photos --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase photos --timeout 3600
uv run macos-data-rescue approve --job-dir "$JOB" --phase library --by "customer name"
uv run macos-data-rescue scan --job-dir "$JOB" --phase library --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --phase library --timeout 3600
```

Check status any time:

```bash
uv run macos-data-rescue status --job-dir "$JOB"
```

Generate reports:

```bash
uv run macos-data-rescue customer-report --job-dir "$JOB" --format pdf
uv run macos-data-rescue customer-report --job-dir "$JOB" --format markdown
uv run macos-data-rescue customer-report --job-dir "$JOB" --format pdf --language cs
uv run macos-data-rescue customer-report --job-dir "$JOB" --format markdown --language cs
uv run macos-data-rescue report --job-dir "$JOB" --format markdown > "$JOB/report.md"
uv run macos-data-rescue report --job-dir "$JOB" --format json > "$JOB/report.json"
```

`customer-report` writes `recovery-report.pdf` or `recovery-report.md` into the visible recovery root next to `user-data/` by default. English is the default. Use `--language cs` for Czech output; the default filenames are `recovery-report-cs.pdf` and `recovery-report-cs.md` so the English report is not overwritten. The customer PDF includes the outcome summary, top-level recovered folder breakdown, and Library-area breakdown when applicable. Keep the full Markdown/JSON reports under `JOB` for technician review.

## Exit Codes And File Statuses

Exit codes describe whether the command itself ran:

- `0`: command completed. `copy` and `resume` can still return `0` when individual files are `failed` or `timed_out`.
- `1`: job/runtime error, such as unsafe paths, missing manifest, missing source, or unexpected failure.
- `2`: CLI usage error from argparse, such as invalid phase or missing required option.

Per-file statuses in `status` and reports are the real rescue outcome:

- `pending`: scanned but not copied yet.
- `copying`: interrupted during copy; `resume` will retry it.
- `copied`: content byte count matched the expected scanned size. This does not guarantee all metadata was preserved.
- `failed`: copy failed for that file and the tool continued.
- `timed_out`: the per-file worker exceeded timeout and the tool continued.
- `skipped`: intentionally not copied, currently used for symlinks to avoid following external targets.
Warnings are separate per-file report fields, not statuses. They are customer-visible notes such as suspected iCloud dataless placeholder or xattr preservation issue.

Scan output may include `stopped=timeout` or `stopped=limit`. That is not a copy failure. It means the scan command intentionally stopped after committing a partial manifest; run `copy`/`resume`, then repeat the same phase scan if more coverage is needed. Repeated scans of the same phase resume from the saved cursor and clear it after the phase completes.

## Warnings To Explain

### iCloud / Dataless Placeholders

If a file is reported as a suspected iCloud dataless placeholder, tell the customer plainly: the file may have existed on disk only as a placeholder and the actual data may not have been physically present on the mounted volume. Recovery may require the live signed-in Mac account, iCloud.com export, Photos export, or another cloud source.

Do not imply that an empty or tiny copied placeholder is the complete original file.

### macOS Metadata

Extended attributes and resource forks are copied best-effort. The tool deliberately skips `com.apple.quarantine` and `com.apple.macl`. Other xattr/resource-fork failures are reported as warnings. A `copied` content status means byte-count completeness for file contents, not full metadata preservation.

### Applications

The `applications` phase copies `.app` bundle files from the source volume `/Applications` and `~/Applications` into `Volume Applications/` and `Home Applications/` under `DST`. Do not promise that copied apps will launch on another Mac, keep licenses, preserve activation, or replace a proper reinstall.

## Stop Or Escalate

Stop normal file-level copying and escalate when:

- `scan` hangs or the mounted source stops responding.
- `scan --timeout` repeatedly stops before producing useful rows for a phase.
- Many files become `timed_out` in a short run.
- The destination reports `ENOSPC` or free space is clearly insufficient.
- The source disappears, remounts unexpectedly, or paths change.
- The OS logs or commands show broad hardware I/O errors.
- The disk makes abnormal physical symptoms or repeatedly disconnects.
- The customer needs forensic preservation rather than practical file rescue.

Escalation options include imaging first, `ddrescue`, hardware-level recovery, or stopping work to avoid worsening the device.

## Final Handoff Checklist

- `recovery-report.pdf` exists in the visible recovery root next to `user-data/`.
- `report.md` and `report.json` exist under `JOB` for technician review.
- `status` output has been captured in service notes.
- Sample rescued files from each successful phase open from `DST`.
- The customer is told which files were `failed`, `timed_out`, `skipped`, or had warnings.
- iCloud placeholder warnings are explained as possibly not physically present on disk.
- Metadata limitations are explained when warnings exist or when app bundles/resource-fork-heavy data matters.
- The original source was not modified by the rescue workflow.

## Restore to the customer's new disk

Use a `restore` job with `--source` pointing at the rescued `user-data`
folder and `--dest` at the target. A restore scan applies none of the
rescue excludes — every file present in the rescued tree is selected.
Tool-wide copy limitations still apply: symlinks and other non-regular
entries are recorded as `skipped`, and empty directories are not recreated
(see Restore Notes).

### Restore Intake Questions

Confirm these before starting a restore:

- Which setup applies: new Mac over Share Disk/TDM, external disk for the
  customer, or running directly on the new Mac?
- What is the exact destination path? Restore overwrites existing files at
  the same paths, so the target should be a fresh home or empty folder.
- For the Share Disk/TDM setup: what is the customer's account name on the
  new Mac, for the ownership step after the copy?
- Should the `Volume Applications/` and `Home Applications/` archive
  folders be transferred too? They restore as plain folders either way and
  never replace reinstalling applications.

### Restore Preflight

- The rescue job is finished and reviewed: `status` on the rescue job shows
  no unexplained `failed`, `timed_out`, or `pending` rows.
- The destination volume has enough free space for the rescued data.
- The destination home/folder is fresh, or overwriting is explicitly
  intended and approved.

### Restore Command Sequence

```bash
JOB="/Volumes/RecoverySSD/Customer/.restore"
SRC="/Volumes/RecoverySSD/Customer/user-data"
DST="/Volumes/New Mac/Users/customer"

uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST" --profile restore
uv run macos-data-rescue scan --job-dir "$JOB" --timeout 300
uv run macos-data-rescue copy --job-dir "$JOB" --timeout 3600
uv run macos-data-rescue status --job-dir "$JOB"
```

Restore `scan` takes no `--phase`; the job has a single scope and rows are
recorded with phase `restore`. `stopped=timeout`/`stopped=limit` handling,
the scan cursor, and `resume` work exactly as in the rescue direction.

### Restore Verification And Handoff

- Cross-check the counts once the restore scan has completed (no
  `stopped=` in the output): the total row count in the restore job's
  `status` (sum of all statuses) must equal the rescue job's `copied=`
  count — every copied file produced exactly one file in `user-data/`,
  and skipped rows produced none. A single uninterrupted scan prints the
  same number as `scanned=`; interrupted-and-resumed scans only show it in
  `status`. Investigate any difference — it means files were added to or
  removed from the rescued tree by hand.
- `status` shows every row `copied` or intentionally `skipped` (symlinks);
  `failed`, `timed_out`, and `pending` are zero or explained in notes.
- Run `resume` once after the copy finishes: it must print `processed=0`,
  which proves every file verified in place on the new disk.
- Attach `status` and `report --format markdown` output of the restore job
  to the service notes as transfer evidence.
- For the Share Disk/TDM setup, the ownership step below was executed on
  the new Mac before handing it over.
- Sample restored files open from the destination.

### Restore Setups

Three supported setups:

- **New Mac over Share Disk / Target Disk Mode:** dest is the customer's
  home on the mounted new Mac. Files will be owned by the service account.
  After the copy finishes, on the new Mac run
  `diskutil resetUserPermissions / $(id -u customer)` (or
  `sudo chown -R customer:staff /Users/customer`) before handing the Mac
  over, otherwise the customer's account cannot use its own files.
- **External disk for the customer:** dest is a folder on the new external
  disk. macOS ignores ownership on external volumes by default, so no
  ownership step is needed.
- **Directly on the new Mac:** attach the service disk to the new Mac and
  run the CLI there while logged in as the customer's user; ownership is
  then correct automatically. Requires Python 3.11+ (`uv`) on the new Mac.

### Restore Notes

- Restore copies `Volume Applications/` and `Home Applications/` into the
  destination as plain folders. Applications should still be reinstalled;
  these folders are a data archive, not an installation.
- Restore overwrites existing destination files at the same paths; use a
  fresh home folder.
- Empty directories are not recreated (tool-wide limitation); a rescued
  tree produced by this tool contains none.
- Symbolic links recorded in the manifest are not recreated (same behavior
  as rescue); check `report` for `skipped` rows before handoff.
- Reporting: the customer-facing PDF is the one generated from the rescue
  job (`customer-report`). For the restore transfer itself, attach
  `status` and `report --format markdown` output of the restore job to the
  service notes as evidence that every rescued file reached the new disk;
  do not generate `customer-report` from a restore job.

## Non-home source intake and rescue

Use `--profile volume` for an external disk, a bare project/photo folder,
embedded photo library, or `/Volumes/<Volume>/.Trashes/<uid>`. Confirm the
exact source and requested exclusions with the operator. The complete
selected tree is included by default, including caches, Trash and
`Backups.backupdb`; no home phases or home approval gates apply.

Place `JOB` and `DST` in disjoint directories on the recovery disk. Run
`preflight`, then `init --profile volume`, `scan` without `--phase`, and
`copy` (same arguments as the README non-home example). Repeat bounded scans
after copying committed rows, or follow `next`. Use repeatable
`init --exclude 'source-relative-glob'` only for explicitly excluded paths;
patterns and scope are immutable for that job and appear in the JSON report.

Explain before handoff: empty directories and symbolic links are not
recreated, special files are skipped, and the result is neither a bootable
volume nor a filesystem image. Check `status` and both reports, sample copied
files, and review every inaccessible path. A scan access error means coverage
is incomplete even when all previously recorded files have been copied.
