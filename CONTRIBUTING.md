# Contributing

Thank you for helping improve `macos-data-rescue`.

## Before opening an issue

- Search existing issues first.
- Use synthetic data only. Never attach customer files, manifests, reports, names, paths, filenames, screenshots, or volume identifiers.
- Report security issues privately according to [SECURITY.md](SECURITY.md).
- Remember that this tool targets mounted, unlocked macOS volumes and is not a forensic imager.

## Development

Python 3.11+ and `uv` are required.

```bash
git clone https://github.com/vaclavik-xyz/macos-data-rescue.git
cd macos-data-rescue
uv sync --locked
uv run ruff check src tests
uv run bandit -q -r src
uv run pytest
uv build
```

Before claiming a rescue behavior works, run the local synthetic smoke workflow documented in [README.md](README.md) or [docs/mvp-slices.md](docs/mvp-slices.md).

## Pull requests

- Keep source access read-only and destination writes resumable and atomic where practical.
- Prefer the Python standard library; avoid adding runtime dependencies without a clear need.
- Add regression tests for behavior changes, especially safety boundaries and failure handling.
- Keep fixtures synthetic and deterministic.
- Use Conventional Commits for commit messages.
