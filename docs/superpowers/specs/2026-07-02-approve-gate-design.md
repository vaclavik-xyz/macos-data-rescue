# Approve Gate Design

Date: 2026-07-02
Status: approved (Filip: "pridej approve")

## Problem

The approval gates for `app-data`, `applications`, and `full-home` are
behavioral only: the runbook says "ask first" and `next` prints them under
`ask-before:`, but the CLI executes a gated `scan` regardless. A confused
or disobedient agent model can copy more customer data than agreed. The
promise "the agent asks before expanding scope" should be technical, not
just documented.

## Decision

New `approve` command that records an operator decision in the job config;
`scan` refuses gated phases on `customer-home` jobs until the approval is
recorded.

## CLI surface

- `approve --job-dir JOB --phase {app-data,applications,full-home} [--by NAME]`
  - records config key `approved:<phase>` with a UTC timestamp and the
    optional operator name; prints
    `approved phase=<phase> at=<timestamp>` (plus ` by=<name>` when given).
  - re-approving overwrites the record (idempotent).
  - argparse rejects non-gated phases (exit 2).
- `scan --phase <gated>` on a `customer-home` job without a recorded
  approval raises a `RescueError` naming the exact `approve` command to
  run (exit 1); nothing is scanned.
- `copy`/`resume` are gated too, because manifests written by older
  versions (or upgraded mid-job) can already contain gated rows created
  without an approval record:
  - `copy`/`resume --phase <gated>` on a `customer-home` job requires the
    approval for that phase;
  - `copy`/`resume --phase all` on a `customer-home` job refuses when the
    manifest contains any rows of an unapproved gated phase, listing every
    missing `approve` command (re-running verification on a finished older
    job therefore requires recording the original customer consent once
    per gated phase — deliberate);
  - legacy phase selections (`important`, `photos`, `library`) select only
    their own rows and stay ungated;
  - restore jobs are never gated.
- Legacy phases (`important`, `photos`, `library`) stay ungated for
  existing jobs and older scripts; the runbook documents that agents should
  use the customer phases. `all` is ungated only as long as it selects no
  rows of an unapproved gated phase (see the copy/resume rule above); the
  legacy `scan --phase all` itself records no gated customer phases.
- Restore jobs are unaffected (they have a single, already-approved scope
  agreed at intake).

## `next` integration

- Unapproved, untouched gated phases are listed under `ask-before:` and the
  suggested commands become the `approve` invocations (one per phase) with
  the instruction to record the operator/customer decision first, then run
  `next` again.
- An approved gated phase is driven like a core phase: untouched ->
  `scan`, committed work rows -> `copy`, interrupted scan -> resume scan.
- The end-to-end convergence test simulates the operator by executing the
  suggested `approve` command; a blind executor must still converge to the
  handoff state, and must NOT be able to scan a gated phase before the
  approve step.

## Out of scope

- Revoking approvals (`decline`); an unapproved phase simply stays listed.
- Gating legacy phases.
- Approval of the restore profile as a whole.

## Testing

- Gated scan without approval fails with exit 1, names the approve
  command, and records no rows.
- `approve` then `scan` succeeds; the config records timestamp (and name
  with `--by`); output line locked.
- Legacy `library` scan still works without approval (compat lock-in).
- `next` suggests `approve` for unapproved gates and switches to
  `action=scan phase=<gated>` once approved.
- Updated convergence test: rescue converges only through the approve
  step; a pre-approval gated scan attempt fails.
