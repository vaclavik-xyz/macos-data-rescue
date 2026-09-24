# Project review — 2026-09-24

## Scope

Reviewed the CLI, manifest/schema migration, scanner, isolated copy worker,
preflight, workflow guidance, both report renderers, tests, packaging, CI,
and operator documentation against baseline `a593394`. GitHub had one open
issue, [#2](https://github.com/vaclavik-xyz/macos-data-rescue/issues/2), with
three requested capabilities. No open PRs or other worktrees existed at intake.

## Findings and resolution

| Finding | Resolution / regression evidence |
| --- | --- |
| Re-init could silently change source/destination while retaining prior outcomes and approvals | Job identity is immutable; `test_reinit_keeps_source_dest_and_creation_time` |
| Destination could contain source or overlap the manifest directory | Reject overlapping layouts in init/load/preflight; `test_init_rejects_overlapping_layout` |
| Application scope extends outside the home and lacked equivalent write guards | Validate additional roots for scan/copy/report; application-source safety tests |
| Concurrent scan/copy could overwrite state and interfere with cleanup | Advisory per-job writer lock; second-process rejection test |
| Cleanup guessed ownership from a `.rescue-tmp` filename | Persistent device/inode registry; preserve unowned/replaced files and retry failed cleanup |
| Scanner swallowed walk/stat failures and could mark an incomplete scan done | Surface the error, retain an incomplete cursor, keep committed rows; scan-root and walk-error tests |
| Rescan retained exhausted attempts for changed files and lost copy warnings for unchanged files | Reset attempts on content/kind/source changes; retain unchanged-row warnings and fallback provenance |
| Destination symlinks could satisfy resume size/mtime verification | Require a regular destination; internal destination-symlink regression |
| Report source protection missed case aliases and external application roots | Inode-aware containment and application-root checks |
| Customer reports overstated source coverage and assumed a home folder | Describe processed manifest rows and selected source; retain explicit coverage limitations |
| Maximum-length filenames exceeded destination NAME_MAX when used as temp prefixes | Fixed-length random temp names; real 255-character filename test |
| PDF notes could cross the footer when additional paragraphs were added | Reserve the full wrapped paragraph/bullet height |
| Non-home rescue had no named profile or explicit exclude policy (#2) | `volume`, immutable repeated `--exclude` patterns, guidance, docs and full-tree round-trip tests |
| ENOTSUP with missing compression metadata appeared as an undifferentiated failure (#2) | Timed-worker diagnosis and distinct status throughout reporting/guidance; simulated fault plus real healthy macOS compression tests |
| Successful replacement from a duplicate could not be recorded truthfully (#2) | Explicit `--path` + `--fallback-from`, durable provenance, byte-count validation, atomic replacement, truthful reports and resumable alternate mapping |

Automated review observations from the first two commits were also verified:
failed temp cleanup retains its registration, temp-stat failures remain per-file,
destination-symlink verification is tested, and trailing slashes in exclusion
patterns are normalized.

## Delivery structure and limits

Changes are split into focused commits for safety, generic source selection,
and recovery outcomes. The aggregate branch exceeds roughly 1,000 changed
lines because the new statuses cross manifest/copy/guidance/report boundaries
and include regression fixtures. They stay together as one issue delivery so
operators cannot receive a status without matching report/resume support;
automatic reviews examine individual commit scopes.

No customer data or production rescue job was used. A healthy macOS compressed
file is constructed only under a temporary test directory; unreadable compressed
metadata is simulated. Real damaged HFS+ media and Share Disk hardware remain
unverified. Scan deadlines are cooperative; source filesystem calls may block.
Recovery verifies byte counts, not equivalence to an unreadable original.
Empty directories, symlinks, special files, ownership and bootability retain
the documented limitations. A job lock does not coordinate different jobs or
external programs writing to the same destination.

## Verification

- Baseline: 122 tests passed. Final implementation: 157 tests passed on macOS
  with Python 3.13; Ruff and Bandit passed.
- Source/wheel builds and Twine metadata checks passed.
- A separate CLI smoke fixture completed preflight, bounded scan, copy,
  explicit duplicate recovery, no-op resume and a new restore job. Rescue
  finished with 4 direct copies, 1 fallback, 1 intentionally skipped symlink,
  and zero unresolved files; restore copied all 5 regular files.
- Fixture source size/mtime/mode remained unchanged. Recovered content was
  checked with SHA-256, and restored bytes matched the fixture.
- English/Czech Markdown and PDF reports were generated. Rendered Czech PDF
  pages were inspected; headings now stay with the first table row and notes
  remain above the footer.
