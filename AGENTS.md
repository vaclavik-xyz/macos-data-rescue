# Repository rules

## Project
`macos-data-rescue` is an open-source macOS-focused CLI for service technicians rescuing user data from damaged Macs mounted via Share Disk/Target Disk.

## Language and style
- Communicate with Filip in Czech; code, docs, CLI help, and commit messages in English.
- Python 3.11+, standard library first. Avoid runtime dependencies for the MVP.
- Keep CLI deterministic; agents may orchestrate it, but rescue behavior must not depend on an LLM.

## Safety
- Never delete or modify source data.
- Before using the CLI on real customer data, read `docs/agent-runbook.md`.
- Destination writes must be resumable and atomic where practical: copy to temp, then replace/rename.
- Keep manifests/logs under the job directory, not inside source.
- Do not store customer personal data in repository fixtures.

## Git
- Conventional commits only.
- Do not add `Co-Authored-By`.
- After every commit run `roborev wait` and `roborev show HEAD`; fix findings before continuing.

## Verification
- Run `uv run pytest` before commits when tests exist.
- Smoke test the CLI with a local fixture before claiming the MVP works.

## UI and report copy
- The console and reports are used by in-house service technicians. Keep texts short and practical; the app may give a brief hint (e.g. "consider imaging the disk (ddrescue)") but the decision is the technician's.
- Never recommend external services such as data recovery labs, and never reference repo docs or internals (manifest, exit codes, runbook paths) in GUI texts. Raw CLI output belongs in the collapsible Details section.
