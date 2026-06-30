# Customer Recovery Phases Design

## Goal

Make the CLI and agent workflow match how technicians describe recovery to customers: "we rescue the home folder" without silently deciding what is important for the customer. The tool should separate normal home data, hidden home items, application data, and applications so the operator can choose scope explicitly.

## Current Problem

The current phases are useful internally but customer-facing wording is weak:

- `important` sounds like the tool decides what matters.
- `photos` is a narrow subset even though media belongs in normal home recovery.
- `library` is too broad because `~/Library` mixes valuable app data with large cache/log/temp content.
- `all` is ambiguous because it can mean "all scanned rows" to the copier and "full home" to an operator.

## Proposed Phases

### `visible-home`

Default customer-home phase. Scan and copy all non-hidden top-level items in the user home, excluding `Library` and clear cache/log/temp/system trash. This includes standard folders and customer-created folders:

- `Desktop`
- `Documents`
- `Downloads`
- `Pictures`
- `Movies`
- `Music`
- `Public`
- `Sites`
- any other non-hidden top-level directory or file in the home folder

This phase is the main answer to "rescue the home folder" for ordinary customer data.

### `hidden-home`

Default customer-home phase, run automatically after `visible-home` in the recommended workflow. Scan and copy top-level dotfiles and dotfolders in the user home, while excluding clear cache/package-manager/temp ballast.

Examples that should be included when present:

- `.ssh`
- `.gnupg`
- `.config`
- `.local`
- `.zshrc`
- `.bashrc`
- `.gitconfig`
- any other customer-created dotfile or dotfolder not matched by excludes

Examples that should be excluded or pruned:

- `.cache`
- `node_modules`
- package-manager caches such as npm/pnpm/yarn cache directories
- log/temp/cache directories under hidden folders where safe to identify

The purpose is neutral: hidden home data can matter for many users, not only developers.

### `app-data`

Explicit opt-in phase. Scan and copy curated customer-relevant parts of `~/Library`, not the whole Library tree by default.

Initial target areas:

- Mail data
- Messages data and attachments
- Safari user data such as bookmarks
- Notes data where practical
- Contacts/Calendars where practical
- Keychains
- MobileSync backups
- selected `Application Support` content, with cache/log/temp pruning

The operator must ask before running this phase because it can be large, slow, and less customer-visible than home files.

### `applications`

Explicit opt-in phase. Preserve application bundles from:

- `/Applications`
- `~/Applications`

The runbook and report wording must be clear: copying an `.app` bundle does not guarantee the app will launch on another Mac, keep licenses, or preserve activation state.

### `full-home`

Explicit advanced phase. Scan and copy the user home including visible, hidden, and Library content, while still applying safe cache/log/temp excludes. This is for cases with enough destination space/time or when the customer explicitly requests maximum practical coverage.

## Compatibility

Keep existing phase names working for now to avoid breaking existing jobs and tests:

- `important`: legacy alias for the current Desktop/Documents/Downloads subset.
- `photos`: legacy/media subset for Pictures/Movies/Music.
- `library`: legacy broad Library scan with current excludes.
- `all`: keep copy semantics as "all manifest rows"; for scan, keep current full-home behavior or alias to `full-home` with documented wording.

New docs should stop recommending `important` as the primary workflow. Existing jobs with old phase values must remain copyable/resumable.

## Recommended Operator Workflow

Default customer recovery:

1. `scan --phase visible-home`
2. `copy --phase visible-home`
3. `scan --phase hidden-home`
4. `copy --phase hidden-home`

Then ask:

1. Do you want application data from `~/Library` such as Mail, Messages, Safari, Notes, Keychains, or iPhone/iPad backups?
2. Do you want application bundles from `/Applications` and `~/Applications`?
3. Is there enough destination space for the requested scope?

Run `app-data`, `applications`, or `full-home` only after explicit operator/customer choice.

## Agent Intake

Before real customer recovery, the agent must ask or confirm:

- Source user home path.
- Destination and job path, both outside source.
- Whether the normal default scope is acceptable: `visible-home + hidden-home`.
- Whether to include `app-data`.
- Whether to include `applications`.
- Whether a broad `full-home` attempt is desired and there is enough destination space.
- Whether the source appears unstable enough to stop and image/escalate first.

## Reporting

Reports should show the selected phase names exactly, so the customer handoff can say what was attempted:

- visible home data
- hidden home data
- app data
- applications
- full home

Warnings remain per-file, not statuses. Existing iCloud dataless and metadata best-effort wording remains valid.

## Implementation Notes

The scanner should classify by path predicates rather than hard-coded customer value judgments. A future implementation should keep changes small:

- Add new scan phase choices.
- Add path predicates for visible top-level home items and hidden top-level home items.
- Add curated Library roots for `app-data`.
- Add support for application roots outside the user home without allowing job/destination paths under source.
- Preserve source read-only behavior.
- Update README, runbook, tests, and compatibility docs.

## Out Of Scope

- Forensic imaging.
- Parallel scanning/copying.
- Full app migration or license transfer guarantees.
- Replacing customer/operator judgment about whether Library/applications are needed.
