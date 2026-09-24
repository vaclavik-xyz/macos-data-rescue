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
    from macos_data_rescue.scan_worker import ScanIssue
    original_files = scanner.iter_source_files

    def failing_files(root, **kwargs):
        yield from original_files(root, **kwargs)
        yield ScanIssue(str(root / 'private'), kwargs['phase'], 'access denied')

    monkeypatch.setattr(scanner, 'iter_source_files', failing_files)
    summary = scanner.scan_job(job, phase='visible-home', batch_size=1)
    assert summary.issues == 1
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


def test_temp_registration_stat_failure_is_per_file(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, dest = init_and_scan(tmp_path, source)
    original_lstat = type(dest).lstat

    def fail_temp_stat(path):
        if path.name.endswith('.rescue-tmp'):
            raise OSError('destination stat failure')
        return original_lstat(path)

    monkeypatch.setattr(type(dest), 'lstat', fail_temp_stat)
    result = copier.copy_job(job, phase='all', timeout=2)
    assert result.failed == 1
    assert file_rows(job)['Desktop/a']['status'] == 'failed'


def test_failed_temp_cleanup_retains_registration(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, dest = init_and_scan(tmp_path, source)
    dest.mkdir()
    temp = dest / '.rescue.orphan.rescue-tmp'
    temp.write_bytes(b'orphan')
    info = temp.stat()
    conn = manifest.connect(job)
    conn.execute('insert into temporary_files values (?, ?, ?)', (str(temp), info.st_dev, info.st_ino))
    conn.commit()
    monkeypatch.setattr(copier, 'cleanup_path', lambda path: None)
    copier.cleanup_stale_temps(job, 'all', dest)
    assert conn.execute('select count(*) from temporary_files').fetchone()[0] == 1
    conn.close()


def test_resume_replaces_internal_destination_symlink(tmp_path):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, dest = init_and_scan(tmp_path, source)
    run_cli('copy', '--job-dir', str(job))
    target = dest / 'Desktop/a'
    info = target.stat()
    alternate = dest / 'alternate'
    alternate.write_bytes(b'z')
    os.utime(alternate, ns=(info.st_atime_ns, info.st_mtime_ns))
    target.unlink()
    target.symlink_to(alternate)
    run_cli('resume', '--job-dir', str(job))
    assert not target.is_symlink()
    assert target.read_bytes() == b'a'
    assert alternate.read_bytes() == b'z'


def test_selected_scan_root_access_error_is_not_an_empty_scope(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    write_file(source / 'Desktop/a', b'a')
    job, _, _ = init_and_scan(tmp_path, source)
    from macos_data_rescue.scan_worker import ScanWorker
    original_request = ScanWorker.request

    def denied_root(worker, operation, path, parts=()):
        if path == source / 'Desktop':
            return {'error': 'cannot stat selected root'}
        return original_request(worker, operation, path, parts)

    monkeypatch.setattr(ScanWorker, 'request', denied_root)
    result = scanner.scan_job(job, phase='important')
    assert result.issues == 1
    assert 'cannot stat selected root' in manifest.scan_issues(job)[0]['error']
    assert config_value(job, 'scan_done:important') is None


def test_customer_report_cannot_overwrite_application_source(tmp_path):
    source = tmp_path / 'volume/Users/customer'
    write_file(source / 'Desktop/a', b'a')
    app_file = tmp_path / 'volume/Applications/Test.app/Contents/data'
    write_file(app_file, b'app content')
    job, _, _ = init_and_scan(tmp_path, source)
    result = run_cli('customer-report', '--job-dir', str(job), '--output', str(app_file), check=False)
    assert result.returncode == 1
    assert app_file.read_bytes() == b'app content'
