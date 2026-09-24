# Rescue operations review — 2026-09-24

## Scope and evidence

Five requested additions: isolated scan I/O, stream SHA256/destination verification,
live copy state, storage pauses, and explicit duplicate suggestions. The icon
rename was merged separately as PR #4 (`c807b63`). No customer data was used.

The shared manifest, CLI and report contracts are delivered as focused commits
on one integration branch. Keeping them together allows migration, safety and
full-workflow verification despite an aggregate diff exceeding 1,000 lines.

- `e3d306d`: integrity. Two Medium findings (worker send failure and timeout
  classification) fixed and tested in `305f14c`.
- `305f14c`: operation state/storage pauses. Review passed; Low follow-ups below.
- `ee5fb01`: isolated scan. One Medium finding: vanished previously failed paths
  could leave permanent coverage errors. The targeted follow-up rechecks only
  old paths not encountered by the completed traversal, inside the same bounded
  worker. Confirmed absence resolves the current-view issue; other failures stay.
- `ae0065a`: candidates and workflow integration. Review passed; one Low below.

Validation: 174 pytest tests; Ruff and Bandit passed; wheel and source archive
built and passed Twine. Synthetic CLI smoke exercised scan, a failed copy,
candidate selection, explicit fallback, SHA256 verification, watch, disconnected
source/pause/resume, and the Czech PDF report.

The post-commit hook and a manual enqueue failed against the local Roborev daemon
with HTTP timeouts. `roborev wait`/`roborev show HEAD` confirmed no queued job;
commit reviews ran with `roborev review SHA --local`, using unchanged global pi /
OpenRouter GLM-5.3-flash / high settings. Raw review logs are local build evidence,
not a new repository configuration.

## Low follow-ups (not release blockers)

Per the review policy, these do not expand this implementation:

- Format the technician verification summary as a table instead of a dict string.
- Differentiate probe-worker transport failure from disk unavailability more
  precisely; pausing is conservative and preserves data but the guidance can be
  ambiguous. A pipe-send race can abort the invocation instead of pausing it.
- Directory/inode anchors are practical outage guards, not a volume UUID check.
  Reconnecting a different filesystem at the same path can reuse an inode. The
  operator must reconnect the original disk; blindly comparing device IDs would
  incorrectly block legitimate remounts on macOS.
- After a copy process crash, another operation holding the shared writer lock
  can make the stale copy activity appear running until that operation ends.
  A future activity owner identity should distinguish the actual writer.
- Candidate ranking labels two extensionless names as having the same extension;
  this affects the evidence wording/ranking only, never automatic selection.

Initial path validation and copy destination setup can still block in parent
filesystem calls. This is documented; tests simulate outages and blocked workers,
not physical failures of a real customer's disk.
