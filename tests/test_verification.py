import hashlib
import os

from macos_data_rescue.manifest import connect
from macos_data_rescue.verification import hash_destination, verify_job
from helpers import file_rows, init_and_scan, run_cli, write_file


def copied_job(tmp_path, content=b'original'):
    write_file(tmp_path / 'source-home/Documents/file.txt', content)
    job, source, dest = init_and_scan(tmp_path)
    run_cli('copy', '--job-dir', str(job))
    return job, source, dest


def test_verify_with_disconnected_source_and_same_size_corruption(tmp_path):
    job, source, dest = copied_job(tmp_path)
    row = file_rows(job)['Documents/file.txt']
    assert row['sha256'] == hashlib.sha256(b'original').hexdigest()
    source.rename(tmp_path / 'disconnected')
    assert verify_job(job).verified == 1
    target = dest / 'Documents/file.txt'
    stamp = target.stat().st_mtime_ns
    target.write_bytes(b'corrupt!')
    os.utime(target, ns=(stamp, stamp))
    result = run_cli('verify', '--job-dir', str(job), check=False)
    assert result.returncode == 1 and 'failed=1' in result.stdout
    assert file_rows(job)['Documents/file.txt']['verification_status'] == 'failed'
    assert 'verification: failed' in run_cli('report', '--job-dir', str(job)).stdout


def test_verify_legacy_never_creates_digest_from_destination(tmp_path):
    job, _, _ = copied_job(tmp_path)
    with connect(job) as conn:
        conn.execute('update files set sha256 = null')
    conn.close()
    assert verify_job(job).unverifiable == 1
    assert file_rows(job)['Documents/file.txt']['sha256'] is None


def test_empty_and_recopy_clear_old_verification(tmp_path):
    job, _, dest = copied_job(tmp_path, b'')
    assert verify_job(job).verified == 1
    (dest / 'Documents/file.txt').unlink()
    run_cli('resume', '--job-dir', str(job))
    assert file_rows(job)['Documents/file.txt']['verification_status'] is None
    assert verify_job(job).verified == 1


def test_destination_symlinks_and_special_files_are_rejected(tmp_path):
    root = tmp_path / 'dest'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    write_file(outside / 'file', b'secret')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    assert 'error' in hash_destination(root, 'link/file')
    (root / 'file').symlink_to(outside / 'file')
    assert 'error' in hash_destination(root, 'file')
    os.mkfifo(root / 'fifo')
    assert 'error' in hash_destination(root, 'fifo')
    assert 'error' in hash_destination(root, '../outside/file')


def test_verification_timeout_is_unknown_and_next_file_is_checked(tmp_path, monkeypatch):
    from macos_data_rescue.copier import CopyWorker
    job, _, _ = copied_job(tmp_path)
    monkeypatch.setattr(CopyWorker, 'verify_one', lambda *args: dict(status='timed_out', error='read timed out'))
    summary = verify_job(job)
    assert summary.unverifiable == 1 and summary.failed == 0
    assert file_rows(job)['Documents/file.txt']['verification_status'] == 'unverifiable'


def test_verify_restarts_worker_if_send_fails(monkeypatch, tmp_path):
    from macos_data_rescue.copier import CopyWorker
    worker = CopyWorker()
    class BrokenConnection:
        def send(self, value):
            raise BrokenPipeError('worker died')
        def close(self):
            pass
    calls = []
    def start():
        calls.append(1)
        worker._conn = BrokenConnection()
    monkeypatch.setattr(worker, '_ensure_worker', start)
    result = worker.verify_one(tmp_path, 'a', 1)
    assert result['status'] == 'failed' and len(calls) == 2
    worker.close()


def test_resume_repairs_a_confirmed_digest_mismatch(tmp_path):
    job, _, dest = copied_job(tmp_path)
    target = dest / 'Documents/file.txt'
    stamp = target.stat().st_mtime_ns
    target.write_bytes(b'corrupt!')
    os.utime(target, ns=(stamp, stamp))
    assert verify_job(job).failed == 1
    assert "destination integrity failed" in run_cli("next", "--job-dir", str(job)).stdout
    assert 'copied=1' in run_cli('resume', '--job-dir', str(job)).stdout
    assert target.read_bytes() == b'original'
    assert verify_job(job).verified == 1
