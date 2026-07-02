# Preflight And Next Commands Design

Date: 2026-07-02
Status: approved (Filip: "pridej preflight i next")

## Problem

The runbook is agent-ready for capable models, but two parts still live in
the agent's head: composing shell preflight checks, and tracking the
workflow state machine (which phase next, when to copy vs re-scan, when to
generate reports). Weak models (Haiku-class, Qwen-27B-class) drift there.
Both belong in the deterministic CLI, per AGENTS.md: rescue behavior must
not depend on an LLM.

## Decision

Two new read-mostly subcommands:

- `preflight` — one command that runs the environment checks from the
  runbook and prints pass/warn/fail lines plus a summary.
- `next` — inspects the job manifest and prints the exact next recommended
  command(s), separating "run this now" from "ask the operator first".

## `preflight`

Usage:

- `preflight --job-dir JOB` — job exists: source/dest come from the
  manifest config.
- `preflight --job-dir JOB --source SRC --dest DST` — before `init`:
  no manifest required; both paths must be given.

Checks (line format: `ok|warn|fail <check> <detail>`):

1. `source-exists` — source is an existing directory (fail otherwise).
2. `source-not-root` — realpath of source is not `/` (fail).
3. `source-readable` — source directory entries can be listed; reports the
   entry count (fail on PermissionError with a Full Disk Access hint).
4. `containment` — job-dir and dest are not the source and not inside it,
   using the existing inode-aware guard (fail).
5. `source-mount` — reports the mount point of source and whether it is
   mounted read-only; `warn` when writable ("prefer a read-only source
   mount"), never fail.
6. `job-writable`, `dest-writable` — mkstemp+unlink probe in the nearest
   existing ancestor of each (fail on error). No writes ever touch source.
7. `free-space` — statvfs free bytes on the dest volume; if a manifest
   exists, compare against the byte sum of rows still needing work
   (pending/copying/failed/timed_out): `fail` when free < needed,
   otherwise `ok` with both numbers. Without a manifest, report free
   space only.

Last line: `preflight=ok checks=N warnings=N failures=N` or
`preflight=fail ...`. Exit 0 when no check failed, 1 otherwise. The source
tree is never written to and never deep-walked (a damaged disk must not be
stressed; only the top-level directory listing is read).

## `next`

Usage: `next --job-dir JOB`.

- No manifest at JOB: print an `init` template with the job-dir filled in
  and SRC/DST placeholders, exit 0 (guidance, not an error).
- Corrupt/unreadable manifest: normal error, exit 1.

Suggested commands are printed exactly as runnable lines in runbook style
with every interpolated path escaped via `shlex.quote` (paths may contain
quotes, `$`, or backticks), scan timeout 300, copy timeout 3600.

Customer-home state machine, evaluated in order:

1. Core phases `visible-home`, then `hidden-home`. Within a phase,
   copy-first — committed rows are rescued before more scanning, matching
   the runbook's stopped=timeout guidance:
   - rows with status `pending`/`copying`, or `failed`/`timed_out` with
     `attempts < 2` -> suggest `copy` for the phase;
   - else a persisted scan cursor for the phase -> suggest the same `scan`
     (resume);
   - else no rows and no cursor -> suggest `scan` (skipped entirely when
     any `full-home` rows or cursor exist — full-home supersedes core);
   - otherwise the phase is complete -> evaluate the next one.
2. Gated phases `app-data`, `applications`, `full-home` that already have
   rows or a cursor are driven to completion like core phases.
3. When nothing is left to run automatically:
   - gated phases with no rows are listed under `ask-before:` with their
     scan commands (the runbook approval gate — `next` never claims they
     are required);
   - rows left `failed`/`timed_out` with `attempts >= 2` produce a
     `review:` note pointing at `report`;
   - missing report artifacts are suggested: `customer-report` when
     `<dest-parent>/recovery-report.pdf` is missing, `report --format
     markdown/json` when `<job>/report.md` / `<job>/report.json` are
     missing;
   - when even reports exist: point at the runbook handoff checklist.

Restore profile:

1. Cursor or no rows -> suggest `scan`.
2. Work rows (as above) -> suggest `copy`.
3. All rows `copied`/`skipped` -> print the verification block: run
   `resume` (must print `processed=0`), capture `status` and
   `report --format markdown` into the job dir, cross-check the total row
   count against the rescue job's `copied=` count, and the ownership
   reminder for Share Disk/TDM targets.

Manifests whose rows are all legacy phases (`important`, `photos`,
`library`, `all`): print `status` and a note to follow the runbook
manually; `next` guides only the default customer and restore workflows.

## Out of scope

- `next` does not execute anything and does not verify report freshness
  (existence only).
- No estimation of total source size in preflight (would deep-walk a
  damaged disk).
- No persistence of operator approvals; gates are re-printed every time.

## Testing

TDD per behavior; both commands are pure read paths so tests drive the CLI
end-to-end: preflight ok/fail matrix (missing source, dest inside source,
unwritable dest, free-space fail with a fabricated huge pending row,
read-only warning is a warn not fail, no-manifest mode requires paths);
next walkthrough (fresh job -> visible-home scan; cursor -> same scan;
pending rows -> copy; core complete -> gates + reports; exhausted failures
-> review note; restore job states; legacy-only manifest note; missing
manifest -> init template). A final manual dry run drives a whole rescue
using only `preflight` + `next` outputs.

## Documentation

- README: short section for both commands.
- Runbook: replace the "Useful checks" shell block with `preflight`
  (keeping the manual block as a fallback), and add an agent workflow note:
  after every command, run `next` and follow it; gates still require
  operator/customer approval.
