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

Useful checks:

```bash
SRC="/Volumes/Macintosh HD - Data/Users/customer"
DST="/Volumes/RecoverySSD/Customer/user-data"
JOB="/Volumes/RecoverySSD/Customer/.rescue"

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

Rescue high-value data first:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase important
uv run macos-data-rescue copy --job-dir "$JOB" --phase important --timeout 30
```

Then continue by value and risk:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase photos
uv run macos-data-rescue copy --job-dir "$JOB" --phase photos --timeout 60

uv run macos-data-rescue scan --job-dir "$JOB" --phase library
uv run macos-data-rescue copy --job-dir "$JOB" --phase library --timeout 30

uv run macos-data-rescue scan --job-dir "$JOB" --phase all
uv run macos-data-rescue resume --job-dir "$JOB" --phase all --timeout 30
```

Check status any time:

```bash
uv run macos-data-rescue status --job-dir "$JOB"
```

Generate reports:

```bash
uv run macos-data-rescue report --job-dir "$JOB" --format markdown > "$JOB/report.md"
uv run macos-data-rescue report --job-dir "$JOB" --format json > "$JOB/report.json"
```

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

## Warnings To Explain

### iCloud / Dataless Placeholders

If a file is reported as a suspected iCloud dataless placeholder, tell the customer plainly: the file may have existed on disk only as a placeholder and the actual data may not have been physically present on the mounted volume. Recovery may require the live signed-in Mac account, iCloud.com export, Photos export, or another cloud source.

Do not imply that an empty or tiny copied placeholder is the complete original file.

### macOS Metadata

Extended attributes and resource forks are copied best-effort. The tool deliberately skips `com.apple.quarantine` and `com.apple.macl`. Other xattr/resource-fork failures are reported as warnings. A `copied` content status means byte-count completeness for file contents, not full metadata preservation.

## Stop Or Escalate

Stop normal file-level copying and escalate when:

- `scan` hangs or the mounted source stops responding.
- Many files become `timed_out` in a short run.
- The destination reports `ENOSPC` or free space is clearly insufficient.
- The source disappears, remounts unexpectedly, or paths change.
- The OS logs or commands show broad hardware I/O errors.
- The disk makes abnormal physical symptoms or repeatedly disconnects.
- The customer needs forensic preservation rather than practical file rescue.

Escalation options include imaging first, `ddrescue`, hardware-level recovery, or stopping work to avoid worsening the device.

## Final Handoff Checklist

- `report.md` and `report.json` exist under `JOB`.
- `status` output has been captured in service notes.
- Sample rescued files from each successful phase open from `DST`.
- The customer is told which files were `failed`, `timed_out`, `skipped`, or had warnings.
- iCloud placeholder warnings are explained as possibly not physically present on disk.
- Metadata limitations are explained when warnings exist or when app bundles/resource-fork-heavy data matters.
- The original source was not modified by the rescue workflow.
