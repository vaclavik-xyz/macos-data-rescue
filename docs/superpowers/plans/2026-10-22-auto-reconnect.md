# Automatic Reconnect And Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in `copy`/`resume --auto-reconnect` that waits out a storage loss, cools the disk down, remounts the same volume (source read-only), verifies identity, and resumes, with persisted hard limits that end in an imaging/escalation recommendation.

**Architecture:** A thin `diskutil` adapter (`volumes.py`) with an injectable runner; a persisted event log plus limit rules (`storage_events.py`); a pure wait/remount state machine (`reconnect.py`) driven by the adapter and the existing bounded storage probe; `copy_job` gains an outer retry loop around one copy pass. CLI flags, `next`, `status`, reports and docs surface it.

**Tech Stack:** Python 3.11 stdlib (`plistlib`, `subprocess`, `sqlite3`, `signal`), pytest, macOS `diskutil`.

**Spec:** `docs/superpowers/specs/2026-10-22-auto-reconnect-design.md`

## Global Constraints

- Stdlib only; external tools: `diskutil` only.
- Never write to the source. Source remount is always `diskutil mount readOnly`; unmount is always plain `diskutil unmount` (never `force`); the destination is never unmounted.
- Every `diskutil` call has a timeout (default 60 s); a timeout or non-zero exit means "not available yet", never an exception that kills the run.
- Defaults: cooldown 300 s, wait 1800 s, poll 15 s, max reconnects 6 (since last acknowledgement), max 3 losses per rolling 3600 s, give up after 2 consecutive zero-progress cycles.
- `--auto-reconnect` is off by default; without it `copy`/`resume` behave exactly as on `main` (exit 3 on pause).
- ENOSPC/EDQUOT pauses are never reconnectable.
- Paths recorded in the job are never rewritten.
- Tests never call the real `diskutil`; they use `FakeDiskutil` (Task 1) and directory renames to simulate loss, like `tests/test_operations.py`.
- Conventional commits, no `Co-Authored-By`; `uv run pytest -q` and `uv run ruff check src tests` before each commit; after each commit `roborev wait` + `roborev show HEAD`, fix only proven High/Medium in scope.

## Review Focus

1. Volume returns writable at the same mount point (macOS auto-mount) → must be unmounted and remounted read-only before resuming (Task 3 test `test_source_back_writable_is_remounted_read_only`).
2. Volume comes back at `"/Volumes/X 1"` and plain unmount fails → run stops, paths untouched (Task 3 `test_mounted_elsewhere_unmount_fails_gives_up`).
3. `resume --auto-reconnect` restarted after a give-up → must not loop again without `--acknowledge-storage-loss` (Task 2 `test_gave_up_blocks_until_acknowledged`, Task 4 `test_restart_after_give_up_requires_acknowledgement`).
4. Disk drops twice on the same file → that file becomes `failed`, the queue continues (Task 4 `test_same_file_lost_twice_is_failed_and_queue_continues`).
5. SIGTERM (GUI "Stop") while waiting → exit 3, activity not left `running`/`waiting`, `stopped` event recorded, no give-up block (Task 4 `test_sigterm_while_waiting_exits_3_and_records_stopped`).

---

### Task 1: diskutil adapter and recorded volume identity

**Files:**
- Create: `src/macos_data_rescue/volumes.py`
- Modify: `src/macos_data_rescue/storage.py` (add identity load/record next to `load_anchors`)
- Create: `tests/fakes.py` (`FakeDiskutil`), `tests/test_volumes.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class VolumeInfo: uuid: str | None; mount_point: str | None; fs_type: str | None; writable: bool | None; locked: bool`
  - `Runner = Callable[[list[str], float], tuple[int, bytes]]` (returncode, stdout); default runs `subprocess.run` with `timeout`, returning `(124, b"")` on `TimeoutExpired`/`OSError`.
  - `class Diskutil: __init__(self, run: Runner = default_runner, timeout: float = 60.0)`; `info(self, target: str) -> VolumeInfo | None` (`diskutil info -plist <target>`, parsed with `plistlib`; keys `VolumeUUID`, `MountPoint` (empty string → None), `FilesystemType`, `WritableVolume`, locked from `FileVault`+absent mount or `Locked` if present; None on non-zero/timeout/parse error); `mount(self, uuid: str, *, read_only: bool) -> bool` (`diskutil mount readOnly <uuid>` / `diskutil mount <uuid>`); `unmount(self, uuid: str) -> bool` (`diskutil unmount <uuid>`; the string `force` must never appear in argv).
  - `NETWORK_FS = frozenset({"smbfs", "afpfs", "nfs", "webdav"})`
  - `def volume_identity(path: Path, diskutil: Diskutil) -> dict` → `{"uuid", "mount_point", "fs_type", "network": bool, "read_only": bool}`; mount point found with `preflight.find_mount_point`; uuid None when `info` fails or fs is network.
  - In `storage.py`: `def load_volume_identities(conn, source: Path, dest: Path, diskutil: Diskutil) -> dict` → `{"source": {...}, "destination": {...}}`, stored in `config` key `volume_identities`; recorded lazily on first call while storage is available (same upgrade pattern as `load_anchors`); never overwritten once stored.
  - `tests/fakes.py`: `class FakeDiskutil(Diskutil)` holding a dict `uuid -> state` with `present`, `mount_point`, `writable`, `locked`, call log `calls: list[tuple]`, and hooks so `mount`/`unmount` can rename tmp directories to simulate the volume reappearing. `info(target)` accepts either a uuid or a mount point.

- [ ] **Step 1: Failing tests** in `tests/test_volumes.py`:
  - `test_info_parses_plist` — runner returns a plist with `VolumeUUID="A-1"`, `MountPoint="/Volumes/Data"`, `FilesystemType="apfs"`, `WritableVolume=False` → `VolumeInfo("A-1", "/Volumes/Data", "apfs", False, False)`.
  - `test_info_unmounted_volume_has_no_mount_point` — `MountPoint=""` → `mount_point is None`.
  - `test_info_returns_none_on_timeout_and_error` — runner returns `(124, b"")` and `(1, b"")` → `None`.
  - `test_mount_source_uses_read_only_and_never_force` — `mount("A-1", read_only=True)` argv == `["diskutil", "mount", "readOnly", "A-1"]`; `unmount("A-1")` argv == `["diskutil", "unmount", "A-1"]`.
  - `test_network_volume_identity_has_no_uuid` — fs_type `smbfs` → identity `uuid is None and network is True`.
  - `test_identities_recorded_once` — two calls to `load_volume_identities` with a FakeDiskutil whose uuid changes between calls → second returns the first stored value.
- [ ] **Step 2:** `uv run pytest tests/test_volumes.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `volumes.py`, `load_volume_identities`, `tests/fakes.py`.
- [ ] **Step 4:** `uv run pytest -q` → all pass.
- [ ] **Step 5:** Commit `feat: record source and destination volume identity`.

### Task 2: persisted storage events and limit rules

**Files:**
- Create: `src/macos_data_rescue/storage_events.py`, `tests/test_storage_events.py`
- Modify: `src/macos_data_rescue/manifest.py:create_schema` (new table)

**Interfaces:**
- Produces:
  - Table `storage_events(id integer primary key, at text not null, at_epoch real not null, role text, kind text not null, detail text)`; `kind ∈ {"lost", "cooldown", "remount-attempt", "remounted", "resumed", "gave-up", "stopped", "acknowledged"}`.
  - `def record_event(conn, kind: str, *, role: str | None = None, detail: dict | None = None, now: float | None = None) -> None` (detail stored as JSON; commits).
  - `def list_events(conn) -> list[dict]` (ordered by id; `detail` decoded).
  - `@dataclass(frozen=True) class ReconnectLimits: max_reconnects: int = 6; max_losses_per_window: int = 3; window_seconds: float = 3600.0; max_zero_progress_cycles: int = 2`
  - `def blocked_by_give_up(conn) -> bool` — True when the latest event among `gave-up`/`acknowledged` is `gave-up`.
  - `def acknowledge(conn) -> None` — records `acknowledged`.
  - `def limit_breach(conn, limits: ReconnectLimits, now: float) -> str | None` — evaluated right after a `lost` event is recorded; returns a human reason or None. Rules, counting only events after the latest `acknowledged`:
    - count of `resumed` ≥ `max_reconnects` → `"reconnect limit reached (6)"`;
    - count of `lost` with `at_epoch > now - window_seconds` > `max_losses_per_window` → `"more than 3 disconnects within 60 minutes"`;
    - the last `max_zero_progress_cycles` `lost` events (each preceded by a `resumed`) all have `detail["bytes_since_resume"] == 0` → `"no progress between the last 2 reconnects"`.

- [ ] **Step 1: Failing tests** in `tests/test_storage_events.py` (all with an in-memory/manifest conn and explicit `now`):
  - `test_window_limit_counts_losses_in_last_hour` — 4 `lost` within 3600 s → breach text contains `"60 minutes"`; same 4 spread over 3 h → `None`.
  - `test_reconnect_limit_counts_since_acknowledgement` — 6 `resumed` → breach; `acknowledge` then 1 `lost` → `None`.
  - `test_zero_progress_two_cycles` — `resumed, lost(0), resumed, lost(0)` → breach; `resumed, lost(0), resumed, lost(10)` → `None`.
  - `test_gave_up_blocks_until_acknowledged` — `gave-up` → `blocked_by_give_up` True; after `acknowledge` → False; `stopped` alone → False.
  - `test_schema_migrates_existing_manifest` — `migrate_manifest` on a job created by `init` adds the table.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q` → pass.
- [ ] **Step 5:** Commit `feat: persist storage loss events and reconnect limits`.

### Task 3: reconnect state machine

**Files:**
- Create: `src/macos_data_rescue/reconnect.py`, `tests/test_reconnect.py`
- Modify: `src/macos_data_rescue/storage.py` (`StoragePaused` gains fields), `src/macos_data_rescue/copier.py` (ENOSPC/EDQUOT pause sets `reconnectable=False`)

**Interfaces:**
- Consumes: `Diskutil`, `VolumeInfo`, `load_volume_identities` (Task 1); `record_event`, `limit_breach`, `ReconnectLimits` (Task 2); existing `CopyWorker.check_storage(timeout)` as the bounded probe.
- Produces:
  - `class StoragePaused(Exception): __init__(self, message: str, *, reconnectable: bool = True, file_id: int | None = None)` (existing `raise StoragePaused(msg)` call sites keep working).
  - `@dataclass(frozen=True) class ReconnectPolicy: cooldown: float = 300.0; wait: float = 1800.0; poll: float = 15.0; limits: ReconnectLimits = ReconnectLimits()`
  - `class ReconnectGaveUp(Exception)` (message is the operator-facing reason; it always ends with the escalation hint `"stop file-level copying and image the disk (ddrescue) or escalate; see docs/agent-runbook.md"` except for identity/mount-point/unlock stops, which say "operator decision required").
  - `class Reconnector: __init__(self, conn, identities: dict, probe: Callable[[], None], policy: ReconnectPolicy, diskutil: Diskutil, *, on_wait: Callable[[dict], None] | None = None, sleep=time.sleep, clock=time.time)`. `probe()` raises `StoragePaused` when anchors are not available (copy_job passes `lambda: worker.check_storage(timeout)`).
  - `def recover(self, reason: str, bytes_since_resume: int) -> None` — records `lost`, checks `limit_breach` (→ record `gave-up`, raise `ReconnectGaveUp`), records `cooldown`, calls `on_wait({"phase": "cooldown", "until": epoch, "attempt": n, "reason": reason})`, sleeps cooldown in `poll`-sized slices, then polls until `wait` expires: for every role whose identity has a uuid, call `_restore_role(role)`; then `probe()`; on success record `remounted` (if any mount happened) and `resumed`, return. On wait expiry record `gave-up` and raise `ReconnectGaveUp` (reason mentions "stale mount, reboot or replug" when the volume was listed at the recorded mount point but the probe kept failing).
  - `_restore_role(role) -> None` rules (spec "Loop" step 4): info None → wait; locked → give up ("volume needs unlocking"); not mounted → `mount(uuid, read_only=role == "source")`; source mounted at recorded point but `writable` → `unmount` then `mount(read_only=True)`; mounted at a different point → source: `unmount` + `mount(read_only=True)`, if unmount fails or it lands elsewhere again → give up (operator decision); destination: give up. Records `remount-attempt` for each mount/unmount call. Network/no-uuid roles are only re-probed.

- [ ] **Step 1: Failing tests** in `tests/test_reconnect.py` (policy `cooldown=0, wait=0.2, poll=0.01`; FakeDiskutil; `probe` checks a tmp anchor dir exists):
  - `test_volume_back_unmounted_is_mounted_read_only_and_resumes` — after `recover`, calls contain `("mount", uuid, True)` and events end with `remounted, resumed`.
  - `test_source_back_writable_is_remounted_read_only` — calls == `[("unmount", uuid), ("mount", uuid, True)]`.
  - `test_mounted_elsewhere_unmount_fails_gives_up` — unmount returns False → `ReconnectGaveUp` with `"operator decision"`; no `mount` call.
  - `test_destination_is_never_unmounted` — destination at a different mount point → give up, no `("unmount", dest_uuid)`.
  - `test_volume_never_returns_gives_up_after_wait` — `ReconnectGaveUp`, last event `gave-up`.
  - `test_stale_mount_reason` — FakeDiskutil reports mounted at recorded point, probe keeps raising → reason contains `"stale mount"`.
  - `test_limit_breach_gives_up_before_cooldown` — pre-seed 3 `lost` events within the hour → `recover` raises without sleeping (sleep spy not called).
  - `test_network_volume_is_only_waited_for` — identity `uuid=None`; FakeDiskutil sees zero mount/unmount calls; resumes once the dir is renamed back by a sleep hook.
  - `test_locked_volume_gives_up` — `locked=True` → reason contains `"unlock"`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q` → pass.
- [ ] **Step 5:** Commit `feat: add storage reconnect state machine`.

### Task 4: copy integration, CLI flags, signals

**Files:**
- Modify: `src/macos_data_rescue/copier.py` (`copy_job`, `process_row`), `src/macos_data_rescue/activity.py`, `src/macos_data_rescue/cli.py`
- Test: `tests/test_auto_reconnect.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `copy_job(job_dir, *, phase, timeout, limit=None, path=None, fallback_from=None, reconnect: ReconnectPolicy | None = None, acknowledge_storage_loss: bool = False, diskutil: Diskutil | None = None) -> CopySummary`. `reconnect` with `path` → `RescueError("--auto-reconnect cannot be combined with --path")`. If `acknowledge_storage_loss`, call `acknowledge(conn)` first. If `reconnect` is set and `blocked_by_give_up(conn)` → print-free: set `reconnect = None` and put `"auto-reconnect disabled until --acknowledge-storage-loss"` into `CopySummary.notes: list[str]` (printed by CLI).
  - Structure: move the current body after `load_anchors` into `_copy_pass(...)` that raises `StoragePaused`; outer loop catches it: not reconnectable or `reconnect is None` → `summary.paused = str(exc)` (today's behavior); else if `exc.file_id` equals the file id of the previous loss in this run → `mark_result(..., "failed", error="storage lost twice while reading this file")` and restore the consumed attempt; then `Reconnector.recover(...)`; `ReconnectGaveUp` → `summary.paused = str(exc)`. `attempted`, `handled_ids`, `limit` budget persist across passes. `process_row` passes `file_id=row["id"]` into the `StoragePaused` it raises.
  - `CopySummary` gains `reconnects: int = 0` and `notes: list[str]`; `as_line()` appends `reconnects=N` only when N > 0.
  - `Activity.wait(self, info: dict) -> None` (state `"waiting"`, `reason`, `wait_phase`, `wait_until`, `attempt`); `Activity.resume_running() -> None`; `finish` guard treats `"waiting"` like `"running"`; `activity_snapshot` maps `"waiting"` without a live writer to `"interrupted"`; `activity_text` adds `wait_until=` and `attempt=` when waiting.
  - CLI `add_copy_options`: `--auto-reconnect` (store_true), `--reconnect-cooldown` (non-negative float, 300), `--reconnect-wait` (positive float, 1800), `--max-reconnects` (positive int, 6), `--acknowledge-storage-loss` (store_true). For `copy`/`resume`, install SIGTERM handler raising `KeyboardInterrupt`. `KeyboardInterrupt` during `recover` → record `stopped`, `summary.paused = "stopped while waiting for storage"`, exit 3; during a copy pass → existing `interrupted` path, exit 130.
- [ ] **Step 1: Failing tests** in `tests/test_auto_reconnect.py` (fixture like `tests/test_operations.py::fixture`; FakeDiskutil whose `mount` renames the unplugged dir back; tiny policy):
  - `test_disconnect_mid_copy_auto_resumes` — `copy_job(..., reconnect=policy, diskutil=fake)` → `copied == 3`, `paused is None`, `reconnects == 1`, events contain `lost, cooldown, remount-attempt, remounted, resumed`.
  - `test_without_flag_behavior_is_unchanged` — same disconnect, `reconnect=None` → paused, `b` and `c` pending, attempts 0, no FakeDiskutil calls.
  - `test_full_destination_never_reconnects` — ENOSPC monkeypatch as in `test_full_destination_pauses_and_preserves_retry_budget` → paused, no `lost` event.
  - `test_same_file_lost_twice_is_failed_and_queue_continues` — `b` triggers loss twice → `b` `failed` with that error, `a` and `c` copied.
  - `test_restart_after_give_up_requires_acknowledgement` — seed `gave-up`; `copy_job(reconnect=policy)` on a disconnect → paused, note present, no FakeDiskutil calls; with `acknowledge_storage_loss=True` → resumes.
  - `test_cli_flags_and_exit_code` — `run_cli('copy', ..., '--auto-reconnect', '--reconnect-cooldown', '0', '--reconnect-wait', '0.1')` with a missing source and no real volume identity → exit 3, `state=paused` (the real `diskutil` must not be reachable: identities with `uuid=None` are seeded into `config` before the call).
  - `test_sigterm_while_waiting_exits_3_and_records_stopped` — subprocess `copy --auto-reconnect --reconnect-cooldown 30` on a disconnected source with seeded `uuid=None` identities; poll `status` until `state=waiting`; send SIGTERM → exit 3, last event `stopped`, `blocked_by_give_up` False, activity not `running`/`waiting`.
  - `test_status_shows_waiting` — during the same run, `status` stdout contains `state=waiting` and `attempt=1`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest -q` → pass (existing `tests/test_operations.py` must pass unchanged).
- [ ] **Step 5:** Commit `feat: resume copying automatically after storage reconnect`.

### Task 5: guidance, reports, docs

**Files:**
- Modify: `src/macos_data_rescue/guide.py`, `src/macos_data_rescue/reporting.py`, `docs/agent-runbook.md`, `docs/rescue-operations.md`, `README.md`
- Test: `tests/test_guide.py`, `tests/test_reporting.py`

**Interfaces:**
- Consumes: `list_events`, `blocked_by_give_up` (Task 2).
- Produces:
  - `next`: when the latest activity is `paused` and `blocked_by_give_up` → `state: storage-escalation (<reason>)`, lines recommending imaging with `ddrescue` and a mapfile or lab escalation, and only then the explicit `resume ... --auto-reconnect --acknowledge-storage-loss` line labelled "only if the technician decides to continue file-level copying". When waiting with a live writer → `state: waiting-for-storage` and no command.
  - `report` (markdown + JSON payload key `storage_events`): table of events (time, role, kind, detail).
  - `status`: `storage_losses=N` appended when N > 0.
  - Customer report (cs + en via `CustomerReportText`): one neutral line when `lost` count > 0, e.g. en `"The source disk disconnected {n} times during the rescue; copying resumed after each reconnect."`, cs `"Zdrojový disk se během záchrany {n}× odpojil; kopírování po každém připojení pokračovalo."`; omitted when 0.
  - Docs: runbook section "Overnight runs and automatic reconnect" (what it can/cannot do, the known limitation from the spec, cooling, Energy Saver/caffeinate, limits, acknowledgement); rescue-operations section replacing "operator must reconnect" with the opt-in loop and Volume UUID identity; README flag table.
- [ ] **Step 1: Failing tests:** `test_next_recommends_imaging_after_give_up`, `test_next_waiting_has_no_command`, `test_report_lists_storage_events`, `test_customer_report_mentions_disconnects_only_when_present` (cs and en).
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement + docs.
- [ ] **Step 4:** `uv run pytest -q` and `uv run ruff check src tests` → pass; smoke test on a `/tmp` fixture: init → scan → `copy --auto-reconnect --reconnect-cooldown 1 --reconnect-wait 20`, rename the source away mid-run and back after ~3 s (network-style identity, no diskutil) → run finishes with `reconnects=1`.
- [ ] **Step 5:** Commit `docs: describe automatic reconnect and overnight runs` (code + docs may be one `feat:` commit if docs are small).

### Follow-up (separate PR after GUI PR #8 merges)

GUI: checkbox "Automaticky obnovit po odpojení disku" (default on) mapped to `--auto-reconnect`; `waiting` state in the GUI state model with countdown and attempt; "Stop waiting" sends the existing graceful stop; after a give-up the GUI asks for acknowledgement before passing `--acknowledge-storage-loss`. Not part of this plan's branch.
