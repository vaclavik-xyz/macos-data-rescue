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
