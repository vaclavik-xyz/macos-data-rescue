# Restore Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `restore` job profile that mirrors a rescued tree 1:1 from the service disk onto the customer's new disk using the existing scan/copy machinery.

**Architecture:** `init --profile restore` creates a normal job whose scan walks the whole source without any excludes and labels rows `phase="restore"`. Copy/resume/status/report are reused unchanged. Spec: `docs/superpowers/specs/2026-07-02-restore-profile-design.md`.

**Tech Stack:** Python 3.11 stdlib only, pytest, uv.

## Global Constraints

- No runtime dependencies; stdlib only.
- The CLI never modifies the source tree.
- Conventional commits; run `uv run pytest` before each commit; after each commit run `roborev wait && roborev show HEAD` and fix findings.
- Restore scan applies **no excludes** (`is_excluded`, customer ballast) — 1:1 mirror.
- Manifest rows from a restore scan carry `phase="restore"`.
- The tool stays root-free; ownership is a runbook post-step.

---

### Task 1: init accepts the restore profile

**Files:**
- Modify: `src/macos_data_rescue/cli.py` (init `--profile` choices)
- Test: `tests/test_manifest.py`

**Interfaces:**
- Produces: jobs whose `config.profile == "restore"`; later tasks branch on this value in `scan_job`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_manifest.py`)

```python
def test_init_accepts_restore_profile(tmp_path: Path) -> None:
    source = tmp_path / "rescued" / "user-data"
    write_file(source / "Desktop" / "faktura.txt", b"data")
    job_dir = tmp_path / "restore-job"
    dest_dir = tmp_path / "new-home"

    result = run_cli(
        "init",
        "--job-dir",
        str(job_dir),
        "--source",
        str(source),
        "--dest",
        str(dest_dir),
        "--profile",
        "restore",
    )

    assert "initialized job=" in result.stdout
    assert config_value(job_dir, "profile") == "restore"
```

(`config_value` and `write_file` come from `tests/helpers.py`; extend the existing `from helpers import ...` line if needed.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_manifest.py::test_init_accepts_restore_profile -q`
Expected: FAIL — argparse exits 2 with `invalid choice: 'restore'`.

- [ ] **Step 3: Write minimal implementation** (`src/macos_data_rescue/cli.py`)

```python
    init_parser.add_argument("--profile", default="customer-home", choices=("customer-home", "restore"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_manifest.py::test_init_accepts_restore_profile -q`
Expected: PASS. Then `uv run pytest -q` — all green.

- [ ] **Step 5: Commit**

```bash
git add src/macos_data_rescue/cli.py tests/test_manifest.py
git commit -m "feat: accept restore profile in init"
roborev wait && roborev show HEAD
```

### Task 2: restore scan walks the source 1:1 with phase="restore"

**Files:**
- Modify: `src/macos_data_rescue/scanner.py` (`scan_job`, new `resolve_scan_phase`, `scan_roots`, `should_descend`, `should_include_file`, `manifest_phase_for`)
- Modify: `src/macos_data_rescue/cli.py` (`PHASES` tuple gains `"restore"`)
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `config.profile == "restore"` from Task 1.
- Produces: `resolve_scan_phase(profile: str, phase: str) -> str` (raises `RescueError` on invalid combinations, returns the manifest/cursor phase); manifest rows with `phase="restore"` that Task 4 copies via the default `--phase all` or `--phase restore`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_scanner.py`)

```python
def test_restore_scan_records_everything_without_excludes(tmp_path: Path) -> None:
    source = tmp_path / "rescued-user-data"
    write_file(source / "Desktop" / "faktura.txt", b"desktop")
    write_file(source / "Projects" / "web" / "node_modules" / "pkg" / "index.js", b"js")
    write_file(source / ".Trash" / "old.txt", b"trash")
    write_file(source / "Cache" / "blob.bin", b"cache")
    write_file(source / "Library" / "Caches" / "cache.bin", b"lib cache")
    write_file(source / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist", b"app")
    job_dir = tmp_path / "restore-job"
    dest_dir = tmp_path / "new-home"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(dest_dir), "--profile", "restore",
    )

    result = run_cli("scan", "--job-dir", str(job_dir))

    rows = file_rows(job_dir)
    assert "scanned=6" in result.stdout
    assert sorted(rows) == [
        ".Trash/old.txt",
        "Cache/blob.bin",
        "Desktop/faktura.txt",
        "Library/Caches/cache.bin",
        "Projects/web/node_modules/pkg/index.js",
        "Volume Applications/Legacy.app/Contents/Info.plist",
    ]
    assert {row["phase"] for row in rows.values()} == {"restore"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scanner.py::test_restore_scan_records_everything_without_excludes -q`
Expected: FAIL — scan exits 1 with `unsupported profile: restore`.

- [ ] **Step 3: Write minimal implementation**

`src/macos_data_rescue/cli.py` — add `"restore"` to `PHASES` (used by scan/copy/resume `--phase` choices):

```python
PHASES = (
    "visible-home",
    "hidden-home",
    "app-data",
    "applications",
    "full-home",
    "important",
    "photos",
    "library",
    "restore",
    "all",
)
```

`src/macos_data_rescue/scanner.py` — replace the profile/phase validation at the top of `scan_job` with `resolve_scan_phase` and use its result everywhere the phase feeds the cursor and walker:

```python
def scan_job(
    job_dir: Path,
    *,
    phase: str = "all",
    limit: int | None = None,
    timeout: float | None = None,
    batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
) -> ScanSummary:
    config = load_config(job_dir)
    scan_phase = resolve_scan_phase(config.profile, phase)
    if not config.source.exists():
        raise RescueError(f"source does not exist: {config.source}")
    migrate_manifest(job_dir)
    limiter = ScanLimiter(limit=limit, timeout=timeout)
    cursor = load_scan_cursor(job_dir, scan_phase)
    count = upsert_scanned_files(
        job_dir,
        limiter.wrap(iter_source_files(config.source, phase=scan_phase), skip_until_after=cursor),
        batch_size=batch_size,
        cursor_key=scan_cursor_key(scan_phase),
    )
    if cursor is not None and not limiter.found_cursor and limiter.stopped is None:
        limiter = ScanLimiter(limit=limit, deadline=limiter.deadline)
        count = upsert_scanned_files(
            job_dir,
            limiter.wrap(iter_source_files(config.source, phase=scan_phase)),
            batch_size=batch_size,
            cursor_key=scan_cursor_key(scan_phase),
        )
    if limiter.stopped is None:
        clear_scan_cursor(job_dir, scan_phase)
    return ScanSummary(scanned=count, stopped=limiter.stopped)


def resolve_scan_phase(profile: str, phase: str) -> str:
    if profile == "restore":
        if phase not in {"all", "restore"}:
            raise RescueError(
                "restore profile scans the whole rescued tree; omit --phase"
            )
        return "restore"
    if profile != "customer-home":
        raise RescueError(f"unsupported profile: {profile}")
    if phase == "restore":
        raise RescueError("phase restore requires a restore profile job")
    if phase not in SCAN_PHASES:
        raise RescueError(f"unsupported scan phase: {phase}")
    return phase
```

`scan_roots` — restore walks the whole source:

```python
def scan_roots(source: Path, phase: str) -> tuple[Path, ...]:
    if phase in {"all", "visible-home", "hidden-home", "full-home", "restore"}:
        return (source,)
```

`should_descend` and `should_include_file` — no excludes for restore; add as the first line of BOTH functions:

```python
    if phase == "restore":
        return True
```

`manifest_phase_for`:

```python
def manifest_phase_for(parts: tuple[str, ...], requested_phase: str) -> str:
    if requested_phase == "restore":
        return "restore"
    if requested_phase in CUSTOMER_PHASES:
        return requested_phase
    return phase_for(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scanner.py::test_restore_scan_records_everything_without_excludes -q`
Expected: PASS. Then `uv run pytest -q` — all green (existing scan tests must not change behavior).

- [ ] **Step 5: Commit**

```bash
git add src/macos_data_rescue/cli.py src/macos_data_rescue/scanner.py tests/test_scanner.py
git commit -m "feat: add restore profile scan that mirrors the rescued tree 1:1"
roborev wait && roborev show HEAD
```

### Task 3: phase validation in both directions

**Files:**
- Test: `tests/test_scanner.py` (implementation already exists from Task 2's `resolve_scan_phase`; this task locks it with tests)

**Interfaces:**
- Consumes: `resolve_scan_phase` behavior from Task 2.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_scanner.py`)

```python
def test_restore_scan_rejects_explicit_rescue_phase(tmp_path: Path) -> None:
    source = tmp_path / "rescued-user-data"
    write_file(source / "Desktop" / "faktura.txt", b"desktop")
    job_dir = tmp_path / "restore-job"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(tmp_path / "new-home"), "--profile", "restore",
    )

    result = run_cli("scan", "--job-dir", str(job_dir), "--phase", "visible-home", check=False)

    assert result.returncode == 1
    assert "restore profile scans the whole rescued tree" in result.stderr


def test_customer_scan_rejects_restore_phase(tmp_path: Path) -> None:
    source = tmp_path / "source-home"
    write_file(source / "Desktop" / "invoice.txt", b"desktop")
    job_dir = tmp_path / "job"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(tmp_path / "dest"),
    )

    result = run_cli("scan", "--job-dir", str(job_dir), "--phase", "restore", check=False)

    assert result.returncode == 1
    assert "restore requires a restore profile job" in result.stderr
```

- [ ] **Step 2: Run tests to verify state**

Run: `uv run pytest tests/test_scanner.py -q -k "rejects_explicit_rescue_phase or rejects_restore_phase"`
Expected: PASS if Task 2 implemented `resolve_scan_phase` exactly as written (these are lock-in tests; if either FAILS, fix `resolve_scan_phase` messages to match).

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_scanner.py
git commit -m "test: lock restore/customer phase validation"
roborev wait && roborev show HEAD
```

### Task 4: restore copy round trip, symlink audit, resume

**Files:**
- Test: `tests/test_copier.py` (no production change expected; copy machinery is reused)

**Interfaces:**
- Consumes: restore manifest rows (`phase="restore"`) from Task 2; existing `copy`/`resume` CLI.

- [ ] **Step 1: Write the failing test** (append to `tests/test_copier.py`)

```python
def test_restore_copy_round_trip_records_symlinks_and_resumes_clean(tmp_path: Path) -> None:
    source = tmp_path / "rescued-user-data"
    write_file(source / "Desktop" / "faktura.txt", b"desktop")
    write_file(source / "Projects" / "web" / "node_modules" / "pkg" / "index.js", b"js")
    write_file(source / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist", b"app")
    outside = tmp_path / "outside-dir"
    write_file(outside / "secret.txt", b"secret")
    os.symlink(outside, source / "Desktop" / "rucne-pridany-odkaz")
    job_dir = tmp_path / "restore-job"
    dest_dir = tmp_path / "new-home"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(dest_dir), "--profile", "restore",
    )
    run_cli("scan", "--job-dir", str(job_dir))

    copy = run_cli("copy", "--job-dir", str(job_dir), "--phase", "restore", "--timeout", "5")
    resume = run_cli("resume", "--job-dir", str(job_dir), "--timeout", "5")

    rows = file_rows(job_dir)
    assert "copied=3" in copy.stdout
    assert "skipped=1" in copy.stdout
    assert (dest_dir / "Desktop" / "faktura.txt").read_bytes() == b"desktop"
    assert (dest_dir / "Projects" / "web" / "node_modules" / "pkg" / "index.js").read_bytes() == b"js"
    assert (dest_dir / "Volume Applications" / "Legacy.app" / "Contents" / "Info.plist").read_bytes() == b"app"
    assert rows["Desktop/rucne-pridany-odkaz"]["kind"] == "symlink"
    assert rows["Desktop/rucne-pridany-odkaz"]["status"] == "skipped"
    assert not (dest_dir / "Desktop" / "rucne-pridany-odkaz").exists()
    assert "processed=0" in resume.stdout
    assert "skipped=4" in resume.stdout
```

- [ ] **Step 2: Run test to verify it passes or fails for a real reason**

Run: `uv run pytest tests/test_copier.py::test_restore_copy_round_trip_records_symlinks_and_resumes_clean -q`
Expected: PASS with Tasks 1–2 done (integration lock-in; the copy path is reused). If FAIL, the failure points at a real gap — fix production code, not the test.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_copier.py
git commit -m "test: cover restore copy round trip and resume"
roborev wait && roborev show HEAD
```

### Task 5: restore scan cursor resume with --limit

**Files:**
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: cursor key `scan_cursor:restore` produced by Task 2's `scan_cursor_key(scan_phase)`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_scanner.py`)

```python
def test_restore_scan_limit_resumes_from_persisted_cursor(tmp_path: Path) -> None:
    source = tmp_path / "rescued-user-data"
    for name in ("a.txt", "b.txt", "c.txt"):
        write_file(source / "Desktop" / name, name.encode())
    job_dir = tmp_path / "restore-job"
    run_cli(
        "init", "--job-dir", str(job_dir), "--source", str(source),
        "--dest", str(tmp_path / "new-home"), "--profile", "restore",
    )

    first = run_cli("scan", "--job-dir", str(job_dir), "--limit", "2")
    first_cursor = config_value(job_dir, "scan_cursor:restore")
    second = run_cli("scan", "--job-dir", str(job_dir), "--limit", "2")

    rows = file_rows(job_dir)
    assert "scanned=2" in first.stdout
    assert "stopped=limit" in first.stdout
    assert first_cursor == "Desktop/b.txt"
    assert "scanned=1" in second.stdout
    assert "stopped=" not in second.stdout
    assert sorted(rows) == ["Desktop/a.txt", "Desktop/b.txt", "Desktop/c.txt"]
    assert config_value(job_dir, "scan_cursor:restore") is None
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_scanner.py::test_restore_scan_limit_resumes_from_persisted_cursor -q`
Expected: PASS with Task 2 done (lock-in of cursor reuse). If FAIL, fix the cursor phase wiring in `scan_job`.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_scanner.py
git commit -m "test: cover restore scan cursor resume"
roborev wait && roborev show HEAD
```

### Task 6: README and runbook restore documentation

**Files:**
- Modify: `README.md` (new "Restore to the customer's new disk" section after "Typical service workflow"; add `restore` row to the Phases table)
- Modify: `docs/agent-runbook.md` (new "Restore" chapter at the end)

**Interfaces:**
- Consumes: the CLI surface from Tasks 1–2.

- [ ] **Step 1: Add the README section** (after the "Typical service workflow" section)

```markdown
## Restore to the customer's new disk

The same CLI handles the opposite direction: rescued data on the service
disk -> the customer's new disk. A `restore` job mirrors the rescued tree
1:1 — no excludes are applied, and `Volume Applications/` /
`Home Applications/` folders are copied as-is.

```bash
JOB="/Volumes/RecoverySSD/Customer/.restore"
SRC="/Volumes/RecoverySSD/Customer/user-data"
DST="/Volumes/Novy Mac/Users/customer"

uv run macos-data-rescue init --job-dir "$JOB" --source "$SRC" --dest "$DST" --profile restore
uv run macos-data-rescue scan --job-dir "$JOB"
uv run macos-data-rescue copy --job-dir "$JOB" --timeout 3600
uv run macos-data-rescue status --job-dir "$JOB"
```

Restore jobs have a single scope: `scan` takes no `--phase` and records all
rows with phase `restore`. Existing destination files at the same paths are
overwritten — restore into a fresh home folder. Files written over Share
Disk / Target Disk Mode are owned by the service account; fix ownership on
the new Mac afterwards (see the runbook).
```

Also add to the Phases table:

```markdown
| `restore` | restore-profile jobs only: every file in the rescued tree, 1:1, no excludes |
```

- [ ] **Step 2: Add the runbook chapter** (end of `docs/agent-runbook.md`)

```markdown
## Restore to the customer's new disk

Use a `restore` job (see README) with `--source` pointing at the rescued
`user-data` folder and `--dest` at the target. Three supported setups:

- **New Mac over Share Disk / Target Disk Mode:** dest is the customer's
  home on the mounted new Mac. Files will be owned by the service account.
  After the copy finishes, on the new Mac run
  `diskutil resetUserPermissions / $(id -u customer)` (or
  `sudo chown -R customer:staff /Users/customer`) before handing the Mac
  over, otherwise the customer's account cannot use its own files.
- **External disk for the customer:** dest is a folder on the new external
  disk. macOS ignores ownership on external volumes by default, so no
  ownership step is needed.
- **Directly on the new Mac:** attach the service disk to the new Mac and
  run the CLI there while logged in as the customer's user; ownership is
  then correct automatically. Requires Python 3.11+ (`uv`) on the new Mac.

Notes:

- Restore copies `Volume Applications/` and `Home Applications/` into the
  destination as plain folders. Applications should still be reinstalled;
  these folders are a data archive, not an installation.
- Restore overwrites existing destination files at the same paths; use a
  fresh home folder.
- Symbolic links recorded in the manifest are not recreated (same behavior
  as rescue); check `report` for `skipped` rows before handoff.
```

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: all green (docs only).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/agent-runbook.md
git commit -m "docs: describe the restore workflow"
roborev wait && roborev show HEAD
```

### Task 7: end-to-end smoke test of the restore direction

**Files:**
- None (manual verification; no repo changes)

- [ ] **Step 1: Build a rescued-tree fixture and run the full restore flow**

```bash
S=/tmp/restore-smoke; rm -rf "$S"; mkdir -p "$S/RecoverySSD/Customer/user-data/Desktop" "$S/NewMac/Users/customer"
echo faktura > "$S/RecoverySSD/Customer/user-data/Desktop/faktura.txt"
mkdir -p "$S/RecoverySSD/Customer/user-data/Volume Applications/Legacy.app/Contents"
echo app > "$S/RecoverySSD/Customer/user-data/Volume Applications/Legacy.app/Contents/Info.plist"
uv run macos-data-rescue init --job-dir "$S/RecoverySSD/Customer/.restore" --source "$S/RecoverySSD/Customer/user-data" --dest "$S/NewMac/Users/customer" --profile restore
uv run macos-data-rescue scan --job-dir "$S/RecoverySSD/Customer/.restore"
uv run macos-data-rescue copy --job-dir "$S/RecoverySSD/Customer/.restore" --timeout 30
uv run macos-data-rescue resume --job-dir "$S/RecoverySSD/Customer/.restore" --timeout 30
uv run macos-data-rescue status --job-dir "$S/RecoverySSD/Customer/.restore"
diff -r "$S/RecoverySSD/Customer/user-data" "$S/NewMac/Users/customer"
```

Expected: copy prints `copied=2`, resume prints `processed=0`, status prints `copied=2`, and `diff -r` prints nothing (trees identical).
