import errno
import json

from helpers import file_rows, init_and_scan, run_cli, write_file
from macos_data_rescue import copier
from macos_data_rescue.activity import Activity, activity_snapshot
from macos_data_rescue.manifest import connect


def fixture(tmp_path):
    for name in ('a', 'b', 'c'):
        write_file(tmp_path / 'source-home' / name, b'hello')
    return init_and_scan(tmp_path)


def test_full_destination_pauses_and_preserves_retry_budget(tmp_path, monkeypatch):
    job, _, dest = fixture(tmp_path)
    original = copier.copy_one_with_timeout
    calls = []
    def full(source, target, timeout, **kwargs):
        calls.append(source.name)
        return dict(status='failed', error='ENOSPC', pause_reason=copier.storage_error_reason(OSError(errno.ENOSPC, 'full')))
    monkeypatch.setattr(copier, 'copy_one_with_timeout', full)
    result = copier.copy_job(job, phase='all', timeout=2)
    assert result.paused and calls == ['a']
    assert all(row['status'] == 'pending' and row['attempts'] == 0 for row in file_rows(job).values())
    assert 'state=paused' in run_cli('status', '--job-dir', str(job)).stdout
    assert 'state: paused' in run_cli('next', '--job-dir', str(job)).stdout
    monkeypatch.setattr(copier, 'copy_one_with_timeout', original)
    assert copier.copy_job(job, phase='all', timeout=2).copied == 3
    assert (dest / 'c').read_bytes() == b'hello'


def test_disconnected_source_pauses_and_resumes(tmp_path):
    job, source, _ = fixture(tmp_path)
    disconnected = source.with_name('disconnected')
    source.rename(disconnected)
    result = run_cli('copy', '--job-dir', str(job), check=False)
    assert result.returncode == 3 and 'paused=' in result.stdout
    assert all(row['attempts'] == 0 for row in file_rows(job).values())
    disconnected.rename(source)
    assert 'copied=3' in run_cli('resume', '--job-dir', str(job)).stdout


def test_destination_mount_is_not_recreated(tmp_path):
    mounted = tmp_path / 'external'
    mounted.mkdir()
    source = tmp_path / 'source'
    write_file(source / 'a', b'a')
    job = tmp_path / 'job'
    run_cli('init', '--job-dir', str(job), '--source', str(source), '--dest', str(mounted / 'recovered'), '--profile', 'volume')
    run_cli('scan', '--job-dir', str(job))
    mounted.rename(tmp_path / 'unmounted')
    assert run_cli('copy', '--job-dir', str(job), check=False).returncode == 3
    assert not mounted.exists()


def test_progress_and_interrupted_watch_are_read_only(tmp_path):
    job, _, _ = fixture(tmp_path)
    conn = connect(job)
    activity = Activity(conn, 'copy', 'all')
    activity.file('a')
    activity.progress(3)
    conn.close()
    snapshot = activity_snapshot(job)
    assert snapshot['state'] == 'interrupted'
    assert snapshot['current_bytes'] == 3
    assert snapshot['remaining_files'] == 3
    result = run_cli('status', '--job-dir', str(job), '--watch', '--interval', '0.01', '--count', '2')
    assert result.stdout.count('state=interrupted') == 2
    assert all(row['attempts'] == 0 for row in file_rows(job).values())
    with connect(job) as conn:
        persisted = json.loads(conn.execute("select value from config where key = 'activity'").fetchone()[0])
    conn.close()
    assert persisted['state'] == 'running'
