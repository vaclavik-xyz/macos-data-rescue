# Rescue operations

Accepted scope: isolated scan I/O, copy-time SHA256 and destination verification,
watchable progress, recoverable storage pauses, and explicit duplicate suggestions.
The icon master is `assets/brand/Favicon.svg`.

Delivery is split into focused commits (integrity, operation state, scanning,
candidate discovery) with tests and independent commit reviews. They share the
manifest/CLI/report contracts and are integrated on one branch so migration and
end-to-end behavior can be checked together.

## Integrity

`verify --job-dir JOB [--phase PHASE] [--timeout 30] [--limit N]` hashes only the
recovered destination. Every new successful copy stores SHA256 calculated from
its original read stream, including technician-selected fallback copies.
Verification compares that digest and byte count, rejects symlinks, and bounds
individual reads using an isolated process. Exit 1 means failed or unverifiable
files. Jobs copied by older versions have no trusted baseline and are reported as
unverifiable; verification never rereads the damaged source to manufacture one.

SHA256 covers file contents, not metadata, source coverage, or whether an explicit
fallback is truly the same original document. A successful verification describes
the destination at that time; later changes require another verification.

## Live operation state and storage pauses

`status --job-dir JOB --watch [--interval 1] [--count N]` prints stable text
snapshots suitable for a terminal or log. Ctrl-C stops only the watcher. Rates
are averages for this copy invocation, including failed transfer bytes. Remaining
bytes and ETA cover the known manifest queue in the selected phase, not unscanned
data. A stopped writer is shown as interrupted; watch never edits the manifest.

Copy exits with code 3 when storage becomes unavailable or destination writes
report ENOSPC/EDQUOT. It preserves unattempted rows and returns the active file to
pending without consuming a retry. Reconnect the original disks or free space,
then use `resume` with the same options. `next` explains the paused state.
Read-only directory identity probes run in the bounded worker before each file.
Recorded source/destination ancestor paths and inodes prevent silently recreating
an absent mount directory. These are practical outage guards, not a volume UUID
identity certificate: the operator must reconnect the original disks. Keep the
job directory on a reliable local disk; loss of the job disk itself prevents
persisting a pause reason. Existing path validation and destination setup still
involve parent-process filesystem calls and remain subject to kernel I/O hangs.

## Isolated scan I/O

`scan --job-dir JOB --io-timeout 30 [--timeout TOTAL] [--limit N]` performs
list/stat/xattr calls in a persistent spawned process. A timed-out or crashed
worker is killed and replaced before scanning the next path. If the OS cannot
kill it, scanning stops instead of accumulating blocked processes. The overall
time budget is absolute and includes worker startup and cursor replay.

Failed paths persist in `scan_issues` with phase and error; `report` lists them,
customer reports flag incomplete coverage, and scan exits 1. A failed directory
listing means its unknown subtree cannot be enumerated, but other known siblings
are still scanned. Retry the same scan after correcting the source problem.
Jobs with unresolved paths restart traversal so repaired files before the old
cursor are not missed. Successful reinspection clears each corresponding issue;
unvisited issues remain. Old partial cursors are reset once when upgrading the
traversal engine, while copied statuses and digests remain intact.
