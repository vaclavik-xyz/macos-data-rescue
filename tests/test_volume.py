import json

import pytest

from helpers import config_value, file_rows, run_cli, write_file


def volume_job(tmp_path, *args):
    source, dest, job = (tmp_path / name for name in ('source', 'dest', 'job'))
    source.mkdir(exist_ok=True)
    run_cli('init', '--source', str(source), '--dest', str(dest), '--job-dir', str(job),
            '--profile', 'volume', *args)
    return source, dest, job


def test_volume_copies_full_tree_and_guides_without_home_gates(tmp_path):
    source, dest, job = volume_job(tmp_path)
    names = ['.Trash/photo', 'Library/Caches/item', 'node_modules/pkg/index', 'Backups.backupdb/backup', 'plain']
    for name in names:
        write_file(source / name, name.encode())
    (source / 'empty').mkdir()
    (source / 'link').symlink_to(source / 'plain')
    assert 'action=scan' in run_cli('next', '--job-dir', str(job)).stdout
    run_cli('scan', '--job-dir', str(job), '--limit', '2')
    assert 'action=copy' in run_cli('next', '--job-dir', str(job)).stdout
    run_cli('copy', '--job-dir', str(job))
    assert 'action=scan' in run_cli('next', '--job-dir', str(job)).stdout
    run_cli('scan', '--job-dir', str(job))
    run_cli('copy', '--job-dir', str(job), '--phase', 'volume')
    rows = file_rows(job)
    assert {r['phase'] for r in rows.values()} == {'volume'}
    assert rows['link']['status'] == 'skipped'
    for name in names:
        assert (dest / name).read_bytes() == name.encode()
    assert not (dest / 'empty').exists()
    assert 'volume-copy-complete' in run_cli('next', '--job-dir', str(job)).stdout
    assert 'processed=0' in run_cli('resume', '--job-dir', str(job)).stdout


def test_volume_excludes_are_explicit_persistent_and_auditable(tmp_path):
    source, dest, job = volume_job(tmp_path, '--exclude', '.Trash', '--exclude', 'Backups.backupdb*')
    for name in ['.Trash/photo', 'Backups.backupdb/backup', 'Backups.backupdb.old/backup', 'Library/Caches/keep']:
        write_file(source / name, b'data')
    run_cli('scan', '--job-dir', str(job))
    assert set(file_rows(job)) == {'Library/Caches/keep'}
    report = json.loads(run_cli('report', '--job-dir', str(job), '--format', 'json').stdout)
    assert report['job']['excludes'] == ['.Trash', 'Backups.backupdb*']
    result = run_cli('init', '--source', str(source), '--dest', str(dest), '--job-dir', str(job),
                     '--profile', 'volume', check=False)
    assert result.returncode == 1 and 'excludes cannot change' in result.stderr
    assert json.loads(config_value(job, 'excludes')) == ['.Trash', 'Backups.backupdb*']


@pytest.mark.parametrize('phase', ['visible-home', 'restore', 'volume'])
def test_volume_scan_rejects_phase_selection(tmp_path, phase):
    _, _, job = volume_job(tmp_path)
    result = run_cli('scan', '--job-dir', str(job), '--phase', phase, check=False)
    assert result.returncode == 1


def test_volume_customer_report_uses_generic_scope(tmp_path):
    source, _, job = volume_job(tmp_path)
    write_file(source / 'photo', b'image')
    run_cli('scan', '--job-dir', str(job))
    run_cli('copy', '--job-dir', str(job))
    run_cli('customer-report', '--job-dir', str(job), '--format', 'markdown')
    report = (tmp_path / 'recovery-report.md').read_text()
    assert 'selected source' in report
    assert 'home-folder' not in report


def test_exclude_trailing_slash_is_normalized(tmp_path):
    source, _, job = volume_job(tmp_path, '--exclude', 'Library/')
    write_file(source / 'Library/Mail/message', b'mail')
    write_file(source / 'photo', b'photo')
    run_cli('scan', '--job-dir', str(job))
    assert set(file_rows(job)) == {'photo'}
    assert json.loads(config_value(job, 'excludes')) == ['Library']
