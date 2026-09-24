import os

import pytest

from helpers import config_value, file_rows, init_and_scan, run_cli, write_file
from macos_data_rescue import copier, manifest, scanner
from macos_data_rescue.errors import RescueError
from macos_data_rescue.locking import job_lock


def test_reinit_keeps_source_dest_and_creation_time(tmp_path):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, dest = init_and_scan(tmp_path, source)
    created = config_value(job, 'created_at')
    other = tmp_path / 'other'
    other.mkdir()
    for src, dst in ((other, dest), (source, other)):
        with pytest.raises(RescueError, match='cannot change'):
            manifest.init_manifest(job, src, dst, 'customer-home')
    manifest.init_manifest(job, source, dest, 'customer-home')
    assert config_value(job, 'created_at') == created
    assert config_value(job, 'source') == str(source.resolve())


@pytest.mark.parametrize('layout', ['source-in-dest', 'job-in-dest', 'dest-in-job'])
def test_init_rejects_overlapping_layout(tmp_path, layout):
    source, job, dest = (tmp_path / name for name in ('source', 'job', 'dest'))
    if layout == 'source-in-dest':
        source = dest / 'source'
    elif layout == 'job-in-dest':
        job = dest / 'job'
    else:
        dest = job / 'dest'
    source.mkdir(parents=True)
    with pytest.raises(RescueError):
        manifest.init_manifest(job, source, dest, 'customer-home')
    assert not (job / 'manifest.sqlite').exists()


def test_scan_error_never_marks_complete_and_next_resumes(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, _ = init_and_scan(tmp_path, source)
    original_walk = scanner.os.walk

    def failing_walk(root, **kwargs):
        yield from original_walk(root, **kwargs)
        kwargs['onerror'](PermissionError(13, 'access denied', str(root / 'private')))

    monkeypatch.setattr(scanner.os, 'walk', failing_walk)
    with pytest.raises(RescueError, match='scan incomplete'):
        scanner.scan_job(job, phase='visible-home', batch_size=1)
    assert config_value(job, 'scan_done:visible-home') is None
    assert config_value(job, 'scan_cursor:visible-home') == 'Desktop/a'
    assert file_rows(job)['Desktop/a']['status'] == 'pending'


def test_changed_scan_resets_attempts_and_keeps_copy_warning_if_unchanged(tmp_path):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, _ = init_and_scan(tmp_path, source)
    row = file_rows(job)['Desktop/a']
    manifest.mark_copying(job, row['id'])
    manifest.mark_result(job, row['id'], 'failed', warning='metadata warning')
    scanner.scan_job(job, phase='important')
    assert file_rows(job)['Desktop/a']['warning'] == 'metadata warning'
    write_file(source / 'Desktop/a', b'changed')
    scanner.scan_job(job, phase='important')
    row = file_rows(job)['Desktop/a']
    assert (row['status'], row['attempts'], row['warning']) == ('pending', 0, None)


def test_registered_temp_cleanup_preserves_unowned_and_replaced_files(tmp_path):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, dest = init_and_scan(tmp_path, source)
    dest.mkdir()
    own = dest / '.a.owned.rescue-tmp'
    unowned = dest / '.a.unowned.rescue-tmp'
    replaced = dest / '.a.replaced.rescue-tmp'
    for path in (own, unowned, replaced):
        path.write_bytes(b'keep unless owned')
    conn = manifest.connect(job)
    for path in (own, replaced):
        info = path.stat()
        conn.execute('insert into temporary_files values (?, ?, ?)', (str(path), info.st_dev, info.st_ino))
    conn.commit()
    conn.close()
    new = dest / 'replacement'
    new.write_bytes(b'new inode')
    new.replace(replaced)
    copier.cleanup_stale_temps(job, 'all', dest)
    assert not own.exists()
    assert unowned.exists() and replaced.read_bytes() == b'new inode'


def test_job_writer_lock_blocks_second_process_and_releases(tmp_path):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, _ = init_and_scan(tmp_path, source)
    with job_lock(job):
        result = run_cli('copy', '--job-dir', str(job), check=False)
        assert result.returncode == 1
        assert 'another scan/copy' in result.stderr
    assert 'copied=1' in run_cli('copy', '--job-dir', str(job)).stdout


def test_report_rejects_case_alias_inside_source(tmp_path):
    source = tmp_path / 'Source'
    write_file(source / 'Desktop/a', b'a')
    job, _, _ = init_and_scan(tmp_path, source)
    alias = tmp_path / 'SOURCE'
    if not alias.exists() or not os.path.samefile(alias, source):
        pytest.skip('requires a case-insensitive filesystem')
    result = run_cli('customer-report', '--job-dir', str(job), '--output', str(alias / 'report.md'), check=False)
    assert result.returncode == 1
    assert not (source / 'report.md').exists()


def test_application_phase_rejects_destination_inside_application_source(tmp_path):
    source = tmp_path / 'volume/Users/customer'
    source.mkdir(parents=True)
    apps = tmp_path / 'volume/Applications'
    apps.mkdir()
    job = tmp_path / 'job'
    manifest.init_manifest(job, source, apps / 'rescue', 'customer-home')
    manifest.record_approval(job, 'applications')
    with pytest.raises(RescueError, match='overlaps application source'):
        scanner.scan_job(job, phase='applications')
    assert not (apps / 'rescue').exists()
