import os
import time
from pathlib import Path

from helpers import config_value, file_rows, run_cli, write_file
from macos_data_rescue import scanner
from macos_data_rescue.manifest import scan_issues
from macos_data_rescue.scan_worker import ScanWorker


def slow_worker(conn):
    try:
        while True:
            operation, text, _ = conn.recv()
            path = Path(text)
            if path.name == 'blocked':
                time.sleep(5)
            if operation == 'list':
                with os.scandir(path) as entries:
                    result = {'names': sorted(entry.name for entry in entries)}
            else:
                result = {'info': path.stat(follow_symlinks=False), 'warning': None}
            conn.send(result)
    except (EOFError, OSError):
        pass
    finally:
        conn.close()


def job_fixture(tmp_path):
    source, dest, job = (tmp_path / name for name in ('source', 'dest', 'job'))
    write_file(source / 'blocked' / 'saved-later', b'blocked')
    write_file(source / 'healthy', b'ok')
    run_cli('init', '--job-dir', str(job), '--source', str(source), '--dest', str(dest), '--profile', 'volume')
    return job, source


def test_hung_entry_is_isolated_and_retry_recovers_before_cursor(tmp_path, monkeypatch):
    from macos_data_rescue import scan_worker
    job, source = job_fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(scan_worker, 'ScanWorker', lambda timeout, deadline: ScanWorker(timeout, deadline, target=slow_worker))
        started = time.monotonic()
        summary = scanner.scan_job(job, io_timeout=0.4)
    assert time.monotonic() - started < 3
    assert summary.scanned == 1 and summary.issues == 1
    assert list(file_rows(job)) == ['healthy']
    assert scan_issues(job)[0]['path'] == str(source / 'blocked')
    assert config_value(job, 'scan_done:volume') is None
    assert 'Unscanned paths' in run_cli('report', '--job-dir', str(job)).stdout
    assert 'coverage is incomplete' in run_cli('next', '--job-dir', str(job)).stdout
    retry = scanner.scan_job(job)
    assert retry.issues == 0
    assert list(file_rows(job)) == ['blocked/saved-later', 'healthy']
    assert config_value(job, 'scan_done:volume') is not None


def test_overall_deadline_bounds_a_blocked_operation(tmp_path, monkeypatch):
    from macos_data_rescue import scan_worker
    job, _ = job_fixture(tmp_path)
    monkeypatch.setattr(scan_worker, 'ScanWorker', lambda timeout, deadline: ScanWorker(timeout, deadline, target=slow_worker))
    started = time.monotonic()
    result = scanner.scan_job(job, timeout=0.3, io_timeout=5)
    assert result.stopped == 'timeout'
    assert time.monotonic() - started < 3
    assert config_value(job, 'scan_done:volume') is None
