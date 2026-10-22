import sqlite3

import pytest

from helpers import write_file
from macos_data_rescue.manifest import connect, init_manifest, migrate_manifest
from macos_data_rescue.storage_events import (
    ReconnectLimits,
    acknowledge,
    blocked_by_give_up,
    limit_breach,
    list_events,
    record_event,
)

LIMITS = ReconnectLimits()


@pytest.fixture
def conn(tmp_path):
    write_file(tmp_path / "source" / "a", b"a")
    init_manifest(tmp_path / "job", tmp_path / "source", tmp_path / "dest", "volume")
    connection = connect(tmp_path / "job")
    yield connection
    connection.close()


def test_window_limit_counts_losses_in_last_hour(conn):
    for offset in (0, 600, 1200, 1800):
        record_event(conn, "lost", role="source", now=10_000 + offset)
    assert "60 minutes" in limit_breach(conn, LIMITS, 10_000 + 1800)


def test_window_limit_ignores_losses_spread_over_hours(conn):
    for offset in (0, 3600, 7200, 10_800):
        record_event(conn, "lost", now=10_000 + offset)
    assert limit_breach(conn, LIMITS, 10_000 + 10_800) is None


def test_reconnect_limit_counts_since_acknowledgement(conn):
    for index in range(6):
        record_event(conn, "resumed", now=index * 4000)
    assert "reconnect limit reached (6)" == limit_breach(conn, LIMITS, 30_000)
    acknowledge(conn)
    record_event(conn, "lost", now=40_000)
    assert limit_breach(conn, LIMITS, 40_000) is None


def test_zero_progress_two_cycles(conn):
    for kind, copied in (("resumed", None), ("lost", 0), ("resumed", None), ("lost", 0)):
        record_event(conn, kind, detail=None if copied is None else {"bytes_since_resume": copied}, now=0)
    assert "no progress" in limit_breach(conn, LIMITS, 0)


def test_progress_in_last_cycle_is_not_a_breach(conn):
    for kind, copied in (("resumed", None), ("lost", 0), ("resumed", None), ("lost", 10)):
        record_event(conn, kind, detail=None if copied is None else {"bytes_since_resume": copied}, now=0)
    assert limit_breach(conn, LIMITS, 0) is None


def test_first_loss_of_a_run_is_not_a_zero_progress_cycle(conn):
    for kind, copied in (("lost", 0), ("resumed", None), ("lost", 0)):
        record_event(conn, kind, detail=None if copied is None else {"bytes_since_resume": copied}, now=0)
    assert limit_breach(conn, LIMITS, 0) is None


def test_gave_up_blocks_until_acknowledged(conn):
    record_event(conn, "stopped")
    assert not blocked_by_give_up(conn)
    record_event(conn, "gave-up", detail={"reason": "wait expired"})
    assert blocked_by_give_up(conn)
    acknowledge(conn)
    assert not blocked_by_give_up(conn)
    assert [event["kind"] for event in list_events(conn)] == ["stopped", "gave-up", "acknowledged"]
    assert list_events(conn)[1]["detail"] == {"reason": "wait expired"}


def test_unknown_event_kind_is_rejected(conn):
    with pytest.raises(ValueError):
        record_event(conn, "exploded")


def test_schema_migrates_existing_manifest(tmp_path):
    write_file(tmp_path / "source" / "a", b"a")
    init_manifest(tmp_path / "job", tmp_path / "source", tmp_path / "dest", "volume")
    raw = sqlite3.connect(tmp_path / "job" / "manifest.sqlite")
    raw.execute("drop table storage_events")
    raw.commit()
    raw.close()
    conn = connect(tmp_path / "job")
    try:
        assert list_events(conn) == [] and not blocked_by_give_up(conn)
    finally:
        conn.close()
    migrate_manifest(tmp_path / "job")
    conn = connect(tmp_path / "job")
    try:
        record_event(conn, "lost")
        assert len(list_events(conn)) == 1
    finally:
        conn.close()
