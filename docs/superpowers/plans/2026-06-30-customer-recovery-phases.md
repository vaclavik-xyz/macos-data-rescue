# Customer Recovery Phases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace customer-facing recovery workflow with `visible-home`, `hidden-home`, `app-data`, `applications`, and `full-home` while preserving legacy phase compatibility.

**Architecture:** Keep scanner behavior path-predicate based. Home phases use the existing manifest model; `applications` adds a nullable per-row `source_path` so the copier can safely read source files outside the configured user home. Docs and runbook become the primary operator interface.

**Tech Stack:** Python 3.11+ stdlib runtime, SQLite manifest migrations, argparse CLI, pytest via `uv run pytest -q`.

---

## File Structure

- Modify `src/macos_data_rescue/cli.py`
  - Expand phase choices accepted by `scan`, `copy`, and `resume`.
- Modify `src/macos_data_rescue/scanner.py`
  - Add customer-facing phase constants and path predicates.
  - Add curated `app-data` Library roots.
  - Add source-volume application roots for the `applications` phase.
- Modify `src/macos_data_rescue/manifest.py`
  - Add nullable `source_path` column for rows whose source is outside the configured home.
  - Extend `ScannedFile` and upsert logic.
- Modify `src/macos_data_rescue/copier.py`
  - Validate manifest row `source_path` against allowed application roots before copying; otherwise keep current `source_root / relative_path`.
- Modify `src/macos_data_rescue/reporting.py`
  - Include `source_path` in JSON reports when present.
- Modify `tests/test_cli.py`
  - Add focused regression tests for new phases, legacy compatibility, app-data, applications, migration, and copy semantics.
- Modify `README.md`
  - Replace `important/photos/library/all` as recommended workflow with `visible-home + hidden-home`, then explicit app-data/applications choices.
- Modify `docs/agent-runbook.md`
  - Add intake questions and new command sequence.
- Modify `docs/mvp-slices.md`
  - Mark customer-facing phases as shipped after implementation.
- Modify `docs/implementation-plan.md`
  - Update CLI command examples.
- Keep `docs/superpowers/specs/2026-06-30-customer-recovery-phases-design.md` as the design source.

---

## Task 1: Add `visible-home` And `hidden-home`

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `src/macos_data_rescue/cli.py`
- Modify: `src/macos_data_rescue/scanner.py`

- [ ] **Step 1: Write failing tests for customer home phases**

Add these tests near existing scan phase tests in `tests/test_cli.py`:

```python
def test_scan_phase_visible_home_records_non_hidden_home_without_library_or_dot_items(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Projects" / "client" / "brief.txt", b"brief")
    write_file(source / "Applications" / "UserOnly.app" / "Contents" / "Info.plist", b"app")
    write_file(source / "Library" / "Mail" / "mailbox", b"mail")
    write_file(source / ".ssh" / "config", b"ssh")
    write_file(source / ".zshrc", b"zsh")
    write_file(source / ".Trash" / "old.txt", b"trash")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Desktop/invoice.txt",
        "Pictures/photo.jpg",
        "Projects/client/brief.txt",
    ]
    assert {row["phase"] for row in rows.values()} == {"visible-home"}


def test_scan_phase_hidden_home_records_dot_items_without_cache_ballast(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / ".ssh" / "config", b"ssh")
    write_file(source / ".config" / "app" / "settings.json", b"{}")
    write_file(source / ".zshrc", b"zsh")
    write_file(source / ".cache" / "browser" / "cache.bin", b"cache")
    write_file(source / ".npm" / "_cacache" / "blob", b"cache")
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "hidden-home")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        ".config/app/settings.json",
        ".ssh/config",
        ".zshrc",
    ]
    assert {row["phase"] for row in rows.values()} == {"hidden-home"}
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_phase_visible_home_records_non_hidden_home_without_library_or_dot_items \
  tests/test_cli.py::test_scan_phase_hidden_home_records_dot_items_without_cache_ballast
```

Expected: both fail with argparse `invalid choice` or unsupported scan phase.

- [ ] **Step 3: Add new phase choices to CLI**

In `src/macos_data_rescue/cli.py`, replace the phase tuple with:

```python
PHASES = (
    "visible-home",
    "hidden-home",
    "important",
    "photos",
    "library",
    "all",
)
```

- [ ] **Step 4: Add scanner predicates for visible and hidden home**

In `src/macos_data_rescue/scanner.py`, add constants near the existing phase constants:

```python
CUSTOMER_PHASES = ("visible-home", "hidden-home")
LEGACY_PHASES = ("important", "photos", "library", "all")
SCAN_PHASES = CUSTOMER_PHASES + LEGACY_PHASES
CUSTOMER_HOME_TOP_LEVEL_EXCLUDES = {
    ".cache",
    ".npm",
    ".pnpm-store",
    ".Trash",
    ".yarn",
    "Logs",
    "tmp",
}
VISIBLE_HOME_TOP_LEVEL_EXCLUDES = CUSTOMER_HOME_TOP_LEVEL_EXCLUDES | {
    ".Spotlight-V100",
    ".fseventsd",
    "Applications",
    "Library",
}
HIDDEN_HOME_TOP_LEVEL_EXCLUDES = {
    ".Trash",
    ".cache",
    ".npm",
    ".pnpm-store",
    ".yarn",
    ".gradle",
    ".Trash",
    ".Spotlight-V100",
    ".fseventsd",
}
HIDDEN_HOME_EXCLUDE_PARTS = {"node_modules", "__pycache__", "Cache", "Caches", "cache", "caches", "tmp", "Temp"}
```

Replace `iter_source_files()` with phase-aware filtering:

```python
def iter_source_files(source: Path, *, phase: str = "all"):
    for scan_root in scan_roots(source, phase):
        yield from iter_tree(source, scan_root, phase)
```

Change `iter_tree()` signature and filtering:

```python
def iter_tree(source: Path, scan_root: Path, phase: str):
    for root, dirs, files in os.walk(scan_root, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs.sort()
        files.sort()
        kept_dirs = []
        for dirname in dirs:
            rel_parts = relative_parts(source, root_path / dirname)
            if should_descend(rel_parts, phase):
                kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for filename in files:
            path = root_path / filename
            rel_parts = relative_parts(source, path)
            if not should_include_file(rel_parts, phase):
                continue
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            yield ScannedFile(
                relative_path="/".join(rel_parts),
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                mode=stat.S_IMODE(info.st_mode),
                kind=file_kind(info.st_mode),
                phase=manifest_phase_for(rel_parts, phase),
                warning=warning_for(path, rel_parts, info),
            )
```

Add these helpers:

```python
def should_descend(parts: tuple[str, ...], phase: str) -> bool:
    if is_excluded(parts):
        return False
    if phase in CUSTOMER_PHASES and is_customer_home_ballast(parts):
        return False
    if phase == "visible-home":
        return is_visible_home_path(parts)
    if phase == "hidden-home":
        return is_hidden_home_path(parts)
    if phase == "full-home":
        return True
    return True


def should_include_file(parts: tuple[str, ...], phase: str) -> bool:
    if is_excluded(parts):
        return False
    if phase in CUSTOMER_PHASES and is_customer_home_ballast(parts):
        return False
    if phase == "visible-home":
        return is_visible_home_path(parts)
    if phase == "hidden-home":
        return is_hidden_home_path(parts)
    if phase == "full-home":
        return True
    return True


def is_visible_home_path(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    first = parts[0]
    return not first.startswith(".") and first not in VISIBLE_HOME_TOP_LEVEL_EXCLUDES


def is_customer_home_ballast(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    first = parts[0]
    return first in CUSTOMER_HOME_TOP_LEVEL_EXCLUDES


def is_hidden_home_path(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    first = parts[0]
    if not first.startswith(".") or first in HIDDEN_HOME_TOP_LEVEL_EXCLUDES:
        return False
    return not any(part in HIDDEN_HOME_EXCLUDE_PARTS for part in parts)


def manifest_phase_for(parts: tuple[str, ...], requested_phase: str) -> str:
    if requested_phase in CUSTOMER_PHASES:
        return requested_phase
    return phase_for(parts)
```

Update the existing `yield ScannedFile(... phase=phase_for(rel_parts) ...)` to use `manifest_phase_for(rel_parts, phase)`.

- [ ] **Step 5: Update scan roots for home phases**

In `scan_roots()`, handle the new home phases:

```python
def scan_roots(source: Path, phase: str) -> tuple[Path, ...]:
    if phase in {"all", "visible-home", "hidden-home", "full-home"}:
        return (source,)
    if phase == "important":
        names = IMPORTANT_DIRS
    elif phase == "photos":
        names = PHOTO_DIRS
    elif phase == "library":
        names = {"Library"}
    else:
        raise RescueError(f"unsupported scan phase: {phase}")
```

Do not add `app-data` or `applications` here yet; later tasks add them.

- [ ] **Step 6: Run tests to verify GREEN**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_phase_visible_home_records_non_hidden_home_without_library_or_dot_items \
  tests/test_cli.py::test_scan_phase_hidden_home_records_dot_items_without_cache_ballast \
  tests/test_cli.py::test_scan_creates_manifest_with_phases_and_excludes \
  tests/test_cli.py::test_scan_phase_important_only_records_high_value_dirs \
  tests/test_cli.py::test_scan_phase_photos_adds_rows_without_duplicating_existing_manifest
```

Expected: all pass.

- [ ] **Step 7: Run full suite and commit**

Run:

```bash
uv run pytest -q
git diff --check
git add src/macos_data_rescue/cli.py src/macos_data_rescue/scanner.py tests/test_cli.py
git commit -m "feat: add visible and hidden home phases"
roborev wait && roborev show HEAD
```

Expected: tests pass and Roborev reports no issues.

---

## Task 2: Add `app-data` Curated Library Phase

**Files:**
- Modify: `src/macos_data_rescue/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `src/macos_data_rescue/scanner.py`

- [ ] **Step 1: Write failing tests for curated app data**

Add this test near the phase scan tests:

```python
def test_scan_phase_app_data_records_curated_library_without_cache_ballast(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Library" / "Mail" / "V10" / "mailbox", b"mail")
    write_file(source / "Library" / "Messages" / "chat.db", b"messages")
    write_file(source / "Library" / "Safari" / "Bookmarks.plist", b"bookmarks")
    write_file(source / "Library" / "Keychains" / "login.keychain-db", b"keychain")
    write_file(source / "Library" / "Application Support" / "Example" / "data.sqlite", b"data")
    write_file(source / "Library" / "Application Support" / "Example" / "Caches" / "blob", b"cache")
    write_file(source / "Library" / "Caches" / "cache.bin", b"cache")
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "app-data")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Library/Application Support/Example/data.sqlite",
        "Library/Keychains/login.keychain-db",
        "Library/Mail/V10/mailbox",
        "Library/Messages/chat.db",
        "Library/Safari/Bookmarks.plist",
    ]
    assert {row["phase"] for row in rows.values()} == {"app-data"}


def test_scan_phase_app_data_does_not_follow_symlinked_library_root(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    outside = tmp_path / "outside-library"
    write_file(outside / "Mail" / "mailbox", b"outside")
    source.mkdir()
    os.symlink(outside, source / "Library")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "app-data")

    assert file_rows(job_dir) == {}
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_phase_app_data_records_curated_library_without_cache_ballast \
  tests/test_cli.py::test_scan_phase_app_data_does_not_follow_symlinked_library_root
```

Expected: fail because `app-data` scan roots are unsupported, include wrong rows, or follow a symlinked Library root.

- [ ] **Step 3: Add app-data phase choices and curated Library roots**

In `src/macos_data_rescue/cli.py`, add `"app-data"` to `PHASES` immediately after `"hidden-home"`:

```python
PHASES = (
    "visible-home",
    "hidden-home",
    "app-data",
    "important",
    "photos",
    "library",
    "all",
)
```

In `src/macos_data_rescue/scanner.py`, extend `CUSTOMER_PHASES`:

```python
CUSTOMER_PHASES = ("visible-home", "hidden-home", "app-data")
```

In `src/macos_data_rescue/scanner.py`, add:

```python
APP_DATA_LIBRARY_PREFIXES = (
    ("Library", "Application Support"),
    ("Library", "Calendars"),
    ("Library", "Containers", "com.apple.Notes"),
    ("Library", "Group Containers", "group.com.apple.notes"),
    ("Library", "Keychains"),
    ("Library", "Mail"),
    ("Library", "Messages"),
    ("Library", "MobileSync", "Backup"),
    ("Library", "Safari"),
)
```

Add helper:

```python
def is_app_data_path(parts: tuple[str, ...]) -> bool:
    if is_excluded(parts):
        return False
    return any(parts[: len(prefix)] == prefix for prefix in APP_DATA_LIBRARY_PREFIXES)
```

Update `should_descend()`:

```python
if phase == "app-data":
    return path_could_match_prefix(parts, APP_DATA_LIBRARY_PREFIXES)
```

Update `should_include_file()`:

```python
if phase == "app-data":
    return is_app_data_path(parts)
```

Add prefix helper:

```python
def path_could_match_prefix(parts: tuple[str, ...], prefixes: tuple[tuple[str, ...], ...]) -> bool:
    return any(
        parts == prefix[: len(parts)] or parts[: len(prefix)] == prefix
        for prefix in prefixes
    )
```

Update `scan_roots()`:

```python
if phase == "app-data":
    library = source / "Library"
    return (library,) if is_real_directory(library) else ()
```

Add the shared root helper near `scan_roots()`:

```python
def is_real_directory(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)
```

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_phase_app_data_records_curated_library_without_cache_ballast \
  tests/test_cli.py::test_scan_phase_app_data_does_not_follow_symlinked_library_root \
  tests/test_cli.py::test_scan_creates_manifest_with_phases_and_excludes
```

Expected: pass.

- [ ] **Step 5: Run full suite and commit**

Run:

```bash
uv run pytest -q
git diff --check
git add src/macos_data_rescue/cli.py src/macos_data_rescue/scanner.py tests/test_cli.py
git commit -m "feat: add curated app data recovery phase"
roborev wait && roborev show HEAD
```

Expected: tests pass and Roborev reports no issues.

---

## Task 3: Add `full-home` Semantics And Legacy Compatibility Tests

**Files:**
- Modify: `src/macos_data_rescue/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `src/macos_data_rescue/scanner.py`

- [ ] **Step 1: Write failing tests for full-home and legacy all**

Add:

```python
def test_scan_phase_full_home_records_visible_hidden_and_library_with_full_home_phase(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Applications" / "UserOnly.app" / "Contents" / "Info.plist", b"app")
    write_file(source / ".ssh" / "config", b"ssh")
    write_file(source / "Library" / "Mail" / "mailbox", b"mail")
    write_file(source / "Library" / "Caches" / "cache.bin", b"cache")
    write_file(source / ".cache" / "browser" / "blob", b"cache")
    write_file(source / ".npm" / "_cacache" / "blob", b"cache")
    write_file(source / "tmp" / "scratch", b"tmp")
    write_file(source / "Logs" / "debug.log", b"log")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "full-home")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        ".ssh/config",
        "Applications/UserOnly.app/Contents/Info.plist",
        "Desktop/invoice.txt",
        "Library/Mail/mailbox",
    ]
    assert {row["phase"] for row in rows.values()} == {"full-home"}


def test_scan_without_phase_keeps_legacy_all_classification(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    write_file(source / "Pictures" / "photo.jpg", b"jpeg")
    write_file(source / "Library" / "Mail" / "mailbox", b"mail")
    write_file(source / "Projects" / "notes.txt", b"notes")
    job_dir, _, _ = init_and_scan(tmp_path, source)

    rows = file_rows(job_dir)
    assert rows["Desktop/invoice.txt"]["phase"] == "important"
    assert rows["Pictures/photo.jpg"]["phase"] == "photos"
    assert rows["Library/Mail/mailbox"]["phase"] == "library"
    assert rows["Projects/notes.txt"]["phase"] == "all"
```

- [ ] **Step 2: Run tests to verify RED/GREEN state**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_phase_full_home_records_visible_hidden_and_library_with_full_home_phase \
  tests/test_cli.py::test_scan_without_phase_keeps_legacy_all_classification
```

Expected before implementation: `full-home` test may fail if hidden files are skipped, cache ballast is included, or phase is not set to `full-home`; legacy all should pass.

- [ ] **Step 3: Add full-home phase choice and semantics**

In `src/macos_data_rescue/cli.py`, add `"full-home"` to `PHASES` immediately after `"app-data"`:

```python
PHASES = (
    "visible-home",
    "hidden-home",
    "app-data",
    "full-home",
    "important",
    "photos",
    "library",
    "all",
)
```

In `src/macos_data_rescue/scanner.py`, extend `CUSTOMER_PHASES`:

```python
CUSTOMER_PHASES = ("visible-home", "hidden-home", "app-data", "full-home")
```

Ensure `full-home` uses customer ballast excludes but includes dotfiles and Library.

In `src/macos_data_rescue/scanner.py`, confirm these functions contain:

```python
def should_descend(parts: tuple[str, ...], phase: str) -> bool:
    if is_excluded(parts):
        return False
    if phase in CUSTOMER_PHASES and is_customer_home_ballast(parts):
        return False
    if phase == "full-home":
        return True
```

```python
def should_include_file(parts: tuple[str, ...], phase: str) -> bool:
    if is_excluded(parts):
        return False
    if phase in CUSTOMER_PHASES and is_customer_home_ballast(parts):
        return False
    if phase == "full-home":
        return True
```

`manifest_phase_for()` must return `requested_phase` for `full-home`. `is_customer_home_ballast()` must exclude top-level cache/log/temp roots such as `.cache`, `.npm`, `tmp`, and `Logs`, but must not exclude `Library` or `Applications` because `full-home` is the explicit phase that includes those home items after normal cache excludes.

- [ ] **Step 4: Run full suite and commit if code changed**

Run:

```bash
uv run pytest -q
git diff --check
git add src/macos_data_rescue/cli.py src/macos_data_rescue/scanner.py tests/test_cli.py
git commit -m "feat: add full home recovery phase"
roborev wait && roborev show HEAD
```

If only tests were added and code from Task 1 already passed them, still commit the tests with the same message.

---

## Task 4: Add `applications` Phase With Per-Row Source Paths

**Files:**
- Modify: `src/macos_data_rescue/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `src/macos_data_rescue/manifest.py`
- Modify: `src/macos_data_rescue/scanner.py`
- Modify: `src/macos_data_rescue/copier.py`
- Modify: `src/macos_data_rescue/reporting.py`

- [ ] **Step 1: Write failing applications scan/copy test**

Add:

```python
def test_scan_and_copy_applications_reads_volume_applications_outside_home(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    volume_app = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    user_app = source / "Applications" / "UserOnly.app" / "Contents" / "Info.plist"
    write_file(volume_app, b"volume app")
    write_file(user_app, b"user app")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    rows = file_rows(job_dir)
    assert sorted(rows) == [
        "Volume Applications/Legacy.app/Contents/Info.plist",
        "Home Applications/UserOnly.app/Contents/Info.plist",
    ]
    assert {row["phase"] for row in rows.values()} == {"applications"}
    assert (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").read_bytes() == b"volume app"
    assert (dest_dir / "Home Applications" / "UserOnly.app" / "Contents" / "Info.plist").read_bytes() == b"user app"


def test_scan_phase_applications_does_not_follow_symlinked_application_roots(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    outside = tmp_path / "outside-apps"
    write_file(outside / "External.app" / "Contents" / "Info.plist", b"outside")
    source.mkdir(parents=True)
    os.symlink(outside, volume / "Applications")
    os.symlink(outside, source / "Applications")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")

    assert file_rows(job_dir) == {}


def test_scan_phase_applications_uses_last_users_segment_for_volume_root(tmp_path: Path) -> None:
    host_like_root = tmp_path / "Users" / "admin"
    volume = host_like_root / "Mounted Air"
    source = volume / "Users" / "dan"
    wrong_host_app = tmp_path / "Applications" / "Host.app" / "Contents" / "Info.plist"
    correct_volume_app = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    write_file(wrong_host_app, b"host app")
    write_file(correct_volume_app, b"volume app")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")

    rows = file_rows(job_dir)
    assert sorted(rows) == ["Volume Applications/Legacy.app/Contents/Info.plist"]


def test_applications_json_report_includes_source_path(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    write_file(app_file, b"volume app")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")

    payload = json.loads(run_cli("report", "--job-dir", str(job_dir), "--format", "json").stdout)

    [item] = payload["files"]
    assert item["relative_path"] == "Volume Applications/Legacy.app/Contents/Info.plist"
    assert item["source_path"] == str(app_file)


def test_scan_and_copy_applications_preserves_bundle_directories_named_cache(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Caches" / "keep.dat"
    write_file(app_file, b"bundle data")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))

    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")

    assert (
        dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Caches" / "keep.dat"
    ).read_bytes() == b"bundle data"


def test_copy_rejects_manifest_source_path_outside_application_roots(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    outside = tmp_path / "outside.txt"
    write_file(app_file, b"volume app")
    write_file(outside, b"outside")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set source_path = ?", (str(outside),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")
    row = file_rows(job_dir)["Volume Applications/Legacy.app/Contents/Info.plist"]

    assert "failed=1" in result.stdout
    assert row["status"] == "failed"
    assert "outside allowed application roots" in row["error"]
    assert not (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").exists()


def test_copy_rejects_manifest_source_path_under_symlinked_user_applications(tmp_path: Path) -> None:
    volume = tmp_path / "Mounted Air"
    source = volume / "Users" / "dan"
    app_file = volume / "Applications" / "Legacy.app" / "Contents" / "Info.plist"
    outside_app = tmp_path / "outside-apps" / "External.app" / "Contents" / "Info.plist"
    write_file(app_file, b"volume app")
    write_file(outside_app, b"outside")
    source.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "outside-apps", source / "Applications")
    job_dir = tmp_path / "job"
    dest_dir = tmp_path / "dest"
    run_cli("init", "--job-dir", str(job_dir), "--source", str(source), "--dest", str(dest_dir))
    run_cli("scan", "--job-dir", str(job_dir), "--phase", "applications")
    conn = sqlite3.connect(job_dir / "manifest.sqlite")
    try:
        conn.execute("update files set source_path = ?", (str(outside_app),))
        conn.commit()
    finally:
        conn.close()

    result = run_cli("copy", "--job-dir", str(job_dir), "--phase", "applications", "--timeout", "2")
    row = file_rows(job_dir)["Volume Applications/Legacy.app/Contents/Info.plist"]

    assert "failed=1" in result.stdout
    assert row["status"] == "failed"
    assert "outside allowed application roots" in row["error"]
```

- [ ] **Step 2: Write failing source_path migration/report test**

Add:

```python
def test_manifest_migration_adds_source_path_for_application_rows(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    db = job_dir / "manifest.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            create table config (
                key text primary key,
                value text not null
            );
            create table files (
                id integer primary key,
                relative_path text not null unique,
                size integer not null,
                mtime_ns integer not null,
                mode integer not null,
                kind text not null,
                phase text not null,
                status text not null default 'pending',
                attempts integer not null default 0,
                error text,
                warning text,
                copied_bytes integer not null default 0,
                scanned_at text not null,
                started_at text,
                finished_at text,
                updated_at text not null
            );
            """
        )
        conn.executemany(
            "insert into config(key, value) values(?, ?)",
            {
                "source": str((tmp_path / "source-home").resolve(strict=False)),
                "dest": str((tmp_path / "dest").resolve(strict=False)),
                "profile": "customer-home",
            }.items(),
        )
        conn.commit()
    finally:
        conn.close()

    run_cli("status", "--job-dir", str(job_dir))
    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("pragma table_info(files)")}
    finally:
        conn.close()
    assert "source_path" in columns
```

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_and_copy_applications_reads_volume_applications_outside_home \
  tests/test_cli.py::test_scan_phase_applications_does_not_follow_symlinked_application_roots \
  tests/test_cli.py::test_scan_phase_applications_uses_last_users_segment_for_volume_root \
  tests/test_cli.py::test_applications_json_report_includes_source_path \
  tests/test_cli.py::test_scan_and_copy_applications_preserves_bundle_directories_named_cache \
  tests/test_cli.py::test_copy_rejects_manifest_source_path_outside_application_roots \
  tests/test_cli.py::test_copy_rejects_manifest_source_path_under_symlinked_user_applications \
  tests/test_cli.py::test_manifest_migration_adds_source_path_for_application_rows
```

Expected: fail because `applications` roots, symlink-safe root checks, and `source_path` do not exist.

- [ ] **Step 4: Extend manifest schema**

In `src/macos_data_rescue/cli.py`, add `"applications"` to `PHASES` immediately after `"full-home"`:

```python
PHASES = (
    "visible-home",
    "hidden-home",
    "app-data",
    "full-home",
    "applications",
    "important",
    "photos",
    "library",
    "all",
)
```

In `src/macos_data_rescue/scanner.py`, extend `CUSTOMER_PHASES`:

```python
CUSTOMER_PHASES = ("visible-home", "hidden-home", "app-data", "full-home", "applications")
```

In `src/macos_data_rescue/manifest.py`, update `ScannedFile`:

```python
@dataclass(frozen=True)
class ScannedFile:
    relative_path: str
    size: int
    mtime_ns: int
    mode: int
    kind: str
    phase: str
    warning: str | None = None
    source_path: str | None = None
```

Add `source_path text` to `create table files` after `relative_path`:

```sql
source_path text,
```

Add migration:

```python
ensure_column(conn, "files", "source_path", "text")
```

Update `upsert_scanned_files()` insert and update SQL:

```sql
insert into files(
    relative_path, source_path, size, mtime_ns, mode, kind, phase, status,
...
source_path = excluded.source_path,
```

Pass `item.source_path` as the second inserted value.

- [ ] **Step 5: Resolve and validate copier source path per row**

In `src/macos_data_rescue/copier.py`, replace:

```python
source = source_root / row["relative_path"]
```

with:

```python
source = resolve_row_source(source_root, row)
```

Keep `dest = dest_root / row["relative_path"]`.

Wrap source resolution in `process_row()` before starting the worker:

```python
    try:
        source = resolve_row_source(source_root, row)
    except ValueError as exc:
        mark_result(
            job_dir,
            row["id"],
            "failed",
            error=f"ValueError: {exc}",
            warning=scan_warning,
        )
        summary.failed += 1
        return
```

Keep `summary.processed += 1` and `mark_copying(job_dir, row["id"])` before this block so the failed validation is visible as an attempted file. Do not let this exception escape `copy_job`, because one bad or modified manifest row must not stop the rescue.

Add these helpers in `copier.py`:

```python
def resolve_row_source(source_root: Path, row: Any) -> Path:
    source_path = row["source_path"] if "source_path" in row.keys() else None
    if not source_path:
        return source_root / row["relative_path"]
    if row["phase"] != "applications":
        raise ValueError("manifest source_path is only allowed for applications phase")
    candidate = Path(source_path).resolve(strict=False)
    roots = application_source_roots_for_home(source_root)
    if not any(is_same_or_inside(candidate, root) for root in roots):
        raise ValueError(f"manifest source_path outside allowed application roots: {source_path}")
    return Path(source_path)


def application_source_roots_for_home(source_root: Path) -> tuple[Path, ...]:
    roots = []
    volume_apps = volume_root_for_home(source_root) / "Applications"
    user_apps = source_root / "Applications"
    for root in (volume_apps, user_apps):
        if is_real_directory(root):
            roots.append(root.resolve(strict=False))
    return tuple(roots)


def volume_root_for_home(source: Path) -> Path:
    parts = source.parts
    users_indexes = [index for index, part in enumerate(parts) if part == "Users"]
    if users_indexes:
        users_index = users_indexes[-1]
        if users_index > 0 and users_index + 1 < len(parts):
            return Path(*parts[:users_index])
    return source.parent


def is_real_directory(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode)


def is_same_or_inside(candidate: Path, parent: Path) -> bool:
    return candidate == parent or parent in candidate.parents
```

Do not accept arbitrary `source_path` values from a modified manifest. A bad application row should fail that file and continue the job, not copy from unrelated host paths.

- [ ] **Step 6: Include source_path in JSON report**

In `src/macos_data_rescue/reporting.py`, update `row_to_dict()`:

```python
payload = {
    "relative_path": row["relative_path"],
    ...
    "copied_bytes": row["copied_bytes"],
}
if "source_path" in row.keys() and row["source_path"]:
    payload["source_path"] = row["source_path"]
return payload
```

- [ ] **Step 7: Add application roots in scanner**

In `src/macos_data_rescue/scanner.py`, add:

```python
def volume_root_for_home(source: Path) -> Path:
    parts = source.parts
    users_indexes = [index for index, part in enumerate(parts) if part == "Users"]
    if users_indexes:
        users_index = users_indexes[-1]
        if users_index > 0 and users_index + 1 < len(parts):
            return Path(*parts[:users_index])
    return source.parent


def application_scan_roots(source: Path) -> tuple[tuple[Path, str], ...]:
    roots: list[tuple[Path, str]] = []
    volume_apps = volume_root_for_home(source) / "Applications"
    user_apps = source / "Applications"
    if is_real_directory(volume_apps):
        roots.append((volume_apps, "Volume Applications"))
    if is_real_directory(user_apps):
        roots.append((user_apps, "Home Applications"))
    return tuple(roots)
```

Extend `iter_source_files()` before home-root scanning:

```python
if phase == "applications":
    for root, dest_prefix in application_scan_roots(source):
        yield from iter_application_tree(root, dest_prefix)
    return
```

Add:

```python
def iter_application_tree(scan_root: Path, dest_prefix: str):
    for root, dirs, files in os.walk(scan_root, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs.sort()
        files.sort()
        for filename in files:
            path = root_path / filename
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            rel = Path(dest_prefix) / path.relative_to(scan_root)
            yield ScannedFile(
                relative_path=str(rel).replace(os.sep, "/"),
                source_path=str(path),
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                mode=stat.S_IMODE(info.st_mode),
                kind=file_kind(info.st_mode),
                phase="applications",
                warning=warning_for(path, tuple(rel.parts), info),
            )
```

- [ ] **Step 8: Run tests to verify GREEN**

Run:

```bash
uv run pytest -q \
  tests/test_cli.py::test_scan_and_copy_applications_reads_volume_applications_outside_home \
  tests/test_cli.py::test_scan_phase_applications_does_not_follow_symlinked_application_roots \
  tests/test_cli.py::test_scan_phase_applications_uses_last_users_segment_for_volume_root \
  tests/test_cli.py::test_applications_json_report_includes_source_path \
  tests/test_cli.py::test_scan_and_copy_applications_preserves_bundle_directories_named_cache \
  tests/test_cli.py::test_copy_rejects_manifest_source_path_outside_application_roots \
  tests/test_cli.py::test_copy_rejects_manifest_source_path_under_symlinked_user_applications \
  tests/test_cli.py::test_manifest_migration_adds_source_path_for_application_rows
```

Expected: pass.

- [ ] **Step 9: Run full suite and commit**

Run:

```bash
uv run pytest -q
git diff --check
git add src/macos_data_rescue/cli.py src/macos_data_rescue/manifest.py src/macos_data_rescue/scanner.py src/macos_data_rescue/copier.py src/macos_data_rescue/reporting.py tests/test_cli.py
git commit -m "feat: support application bundle recovery phase"
roborev wait && roborev show HEAD
```

Expected: tests pass and Roborev reports no issues.

---

## Task 5: Update Docs And Agent Intake

**Files:**
- Modify: `README.md`
- Modify: `docs/agent-runbook.md`
- Modify: `docs/mvp-slices.md`
- Modify: `docs/implementation-plan.md`
- Test: `uv run pytest -q`

- [ ] **Step 1: Update README phase table**

Replace the README phase table with:

```markdown
| Phase | Includes |
|---|---|
| `visible-home` | non-hidden top-level home items except `Library` and excluded cache/trash |
| `hidden-home` | top-level dotfiles/dotfolders except clear cache/package-manager ballast |
| `app-data` | explicit opt-in curated `~/Library` app data |
| `applications` | explicit opt-in `/Applications` and `~/Applications` bundles |
| `full-home` | explicit advanced full user home with cache/log/temp excludes |
| `important`, `photos`, `library`, `all` | legacy compatibility phases |
```

- [ ] **Step 2: Update README workflow**

Replace the service workflow command sequence with:

```bash
uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST"

uv run macos-data-rescue scan --job-dir "$JOB" --phase visible-home
uv run macos-data-rescue copy --job-dir "$JOB" --phase visible-home --timeout 30

uv run macos-data-rescue scan --job-dir "$JOB" --phase hidden-home
uv run macos-data-rescue copy --job-dir "$JOB" --phase hidden-home --timeout 30
```

Then add explicit opt-in examples:

```bash
uv run macos-data-rescue scan --job-dir "$JOB" --phase app-data
uv run macos-data-rescue copy --job-dir "$JOB" --phase app-data --timeout 30

uv run macos-data-rescue scan --job-dir "$JOB" --phase applications
uv run macos-data-rescue copy --job-dir "$JOB" --phase applications --timeout 30
```

- [ ] **Step 3: Update agent runbook intake**

In `docs/agent-runbook.md`, replace the current phase-first workflow with:

```markdown
Default customer home recovery is `visible-home` followed by `hidden-home`.
Before running `app-data`, ask whether the customer wants application data such as Mail, Messages, Safari, Notes, Keychains, or iPhone/iPad backups.
Before running `applications`, ask whether the customer wants application bundles copied, and explain that launchability/licensing is not guaranteed.
```

Also update command examples to use the new phases.

- [ ] **Step 4: Update project docs**

In `docs/mvp-slices.md`, add:

```markdown
- Customer-facing recovery phases: `visible-home`, `hidden-home`, `app-data`, `applications`, and `full-home`, with legacy phase compatibility.
```

In `docs/implementation-plan.md`, update command syntax:

```bash
macos-data-rescue scan --job-dir <dir> [--phase visible-home|hidden-home|app-data|applications|full-home|important|photos|library|all]
macos-data-rescue copy --job-dir <dir> [--phase visible-home|hidden-home|app-data|applications|full-home|important|photos|library|all] [--timeout <seconds>] [--limit <n>]
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
uv run pytest -q
git diff --check
git add README.md docs/agent-runbook.md docs/mvp-slices.md docs/implementation-plan.md
git commit -m "docs: update recovery workflow phases"
roborev wait && roborev show HEAD
```

Expected: tests pass and Roborev reports no issues.

---

## Task 6: Final Verification And Push

**Files:**
- No code changes unless Roborev finds issues.

- [ ] **Step 1: Run final test suite**

Run:

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run final Roborev check**

Run:

```bash
roborev wait && roborev show HEAD
```

Expected: `No issues found.`

- [ ] **Step 3: Verify git state**

Run:

```bash
git status --short --branch
git log --oneline --max-count=6
```

Expected: branch is ahead of `origin/main` by the new implementation commits and working tree is clean.

- [ ] **Step 4: Push**

Run:

```bash
git push origin main
git ls-remote origin refs/heads/main
```

Expected: remote `refs/heads/main` matches local `HEAD`.

---

## Self-Review

- Spec coverage: visible home, hidden home, app-data, applications, full-home, legacy compatibility, docs, reporting, and source safety each have implementation tasks.
- Scope: applications are isolated to their own task because they require manifest schema and copy-source resolution changes.
- Type consistency: new nullable `source_path` is introduced in `ScannedFile`, schema, report payload, and copier source resolution in one task.
- No placeholders: tasks include concrete file paths, test names, commands, snippets, expected outputs, and commit messages.
