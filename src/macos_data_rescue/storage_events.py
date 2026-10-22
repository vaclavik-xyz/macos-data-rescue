"""Persisted storage-loss events and the hard limits of the reconnect loop.

Limits are computed from the manifest, never from per-process counters, so a
restarted `resume --auto-reconnect` cannot reset them.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

EVENT_KINDS = frozenset({"lost", "cooldown", "remount-attempt", "remounted", "resumed",
                         "gave-up", "stopped", "acknowledged"})


@dataclass(frozen=True)
class ReconnectLimits:
    max_reconnects: int = 6
    max_losses_per_window: int = 3
    window_seconds: float = 3600.0
    max_zero_progress_cycles: int = 2


def has_events_table(conn) -> bool:
    # Read-only callers (status, next) may see a manifest from an older version.
    return conn.execute("select 1 from sqlite_master where type = 'table' and name = 'storage_events'").fetchone() is not None


def record_event(conn, kind: str, *, role: str | None = None, detail: dict | None = None,
                 now: float | None = None) -> None:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown storage event kind: {kind}")
    epoch = time.time() if now is None else now
    at = datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    conn.execute("insert into storage_events(at, at_epoch, role, kind, detail) values(?, ?, ?, ?, ?)",
                 (at, epoch, role, kind, json.dumps(detail, ensure_ascii=False) if detail is not None else None))
    conn.commit()


def list_events(conn) -> list[dict]:
    if not has_events_table(conn):
        return []
    rows = conn.execute("select id, at, at_epoch, role, kind, detail from storage_events order by id").fetchall()
    return [dict(id=row[0], at=row[1], at_epoch=row[2], role=row[3], kind=row[4],
                 detail=json.loads(row[5]) if row[5] else None) for row in rows]


def blocked_by_give_up(conn) -> bool:
    for event in reversed(list_events(conn)):
        if event["kind"] in ("gave-up", "acknowledged"):
            return event["kind"] == "gave-up"
    return False


def acknowledge(conn) -> None:
    record_event(conn, "acknowledged")


def events_since_acknowledgement(conn) -> list[dict]:
    events = list_events(conn)
    for index in range(len(events) - 1, -1, -1):
        if events[index]["kind"] == "acknowledged":
            return events[index + 1:]
    return events


def limit_breach(conn, limits: ReconnectLimits, now: float) -> str | None:
    """Reason to stop reconnecting, evaluated right after a `lost` event."""
    events = events_since_acknowledgement(conn)
    resumed = sum(1 for event in events if event["kind"] == "resumed")
    if resumed >= limits.max_reconnects:
        return f"reconnect limit reached ({limits.max_reconnects})"
    recent = sum(1 for event in events
                 if event["kind"] == "lost" and event["at_epoch"] > now - limits.window_seconds)
    if recent > limits.max_losses_per_window:
        return (f"more than {limits.max_losses_per_window} disconnects within "
                f"{limits.window_seconds / 60:g} minutes")
    # A zero-progress cycle is a loss after a reconnect with no new data copied.
    cycles: list[bool] = []
    after_resume = False
    for event in events:
        if event["kind"] == "resumed":
            after_resume = True
        elif event["kind"] == "lost":
            copied = (event["detail"] or {}).get("bytes_since_resume")
            cycles.append(after_resume and copied == 0)
            after_resume = False
    needed = limits.max_zero_progress_cycles
    if needed > 0 and len(cycles) >= needed and all(cycles[-needed:]):
        return f"no progress between the last {needed} reconnects"
    return None
