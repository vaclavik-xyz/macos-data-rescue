import errno
import json
import os
import stat
from types import SimpleNamespace

import pytest

from helpers import file_rows, run_cli, write_file
from macos_data_rescue import copier, manifest, reporting
from test_volume import volume_job


def failed_pair(tmp_path):
    source, dest, job = volume_job(tmp_path)
    write_file(source / 'original/photo', b'photo')
    write_file(source / 'duplicate/photo', b'photo')
    os.utime(source / 'duplicate/photo', ns=(1_000_000_000, 1_000_000_000))
    run_cli('scan', '--job-dir', str(job))
    row = file_rows(job)['original/photo']
    manifest.mark_result(job, row['id'], manifest.UNREADABLE_COMPRESSED, error='original ENOTSUP')
    return source, dest, job


def test_fallback_recovers_one_row_records_provenance_and_resumes(tmp_path):
    source, dest, job = failed_pair(tmp_path)
    source_before = (source / 'original/photo').read_bytes()
    result = run_cli('copy', '--job-dir', str(job), '--path', 'original/photo', '--fallback-from', 'duplicate/photo')
    assert 'copied_from_fallback=1' in result.stdout
    rows = file_rows(job)
    row = rows['original/photo']
    assert row['status'] == 'copied_from_fallback'
    assert row['fallback_source_path'] == 'duplicate/photo'
    assert row['fallback_original_error'] == 'original ENOTSUP'
    assert row['copied_bytes'] == 5
    assert rows['duplicate/photo']['status'] == 'pending'
    assert (dest / 'original/photo').read_bytes() == b'photo'
    assert (source / 'original/photo').read_bytes() == source_before
    run_cli('copy', '--job-dir', str(job))
    assert 'processed=0' in run_cli('resume', '--job-dir', str(job)).stdout
    # Repairing a missing destination must still use the registered fallback.
    (source / 'original/photo').unlink()
    (dest / 'original/photo').unlink()
    assert 'copied_from_fallback=1' in run_cli('resume', '--job-dir', str(job)).stdout
    assert (dest / 'original/photo').read_bytes() == b'photo'
    payload = json.loads(run_cli('report', '--job-dir', str(job), '--format', 'json').stdout)
    recovered = next(r for r in payload['files'] if r['relative_path'] == 'original/photo')
    assert recovered['fallback_source_path'] == 'duplicate/photo'
    assert 'duplicate/photo' in run_cli('report', '--job-dir', str(job)).stdout
    customer = reporting.customer_markdown_report(job)
    assert '| Copied files | 2 |' in customer
    assert '| Unreadable compressed files | 0 |' in customer
    assert 'unresolved files recorded' not in customer
    assert reporting.recovered_top_level_breakdown(manifest.all_files(job)) == [('duplicate', 1, 5), ('original', 1, 5)]
    assert reporting.customer_pdf_bytes(job).startswith(b'%PDF-')


def test_fallback_failure_preserves_destination_and_mapping_for_retry(tmp_path):
    source, dest, job = failed_pair(tmp_path)
    write_file(dest / 'original/photo', b'previous destination')
    write_file(source / 'duplicate/photo', b'too short or long')
    result = run_cli('copy', '--job-dir', str(job), '--path', 'original/photo', '--fallback-from', 'duplicate/photo')
    assert 'failed=1' in result.stdout
    row = file_rows(job)['original/photo']
    assert row['status'] == 'failed'
    assert row['fallback_original_error'] == 'original ENOTSUP'
    assert 'incomplete copy' in row['error']
    assert (dest / 'original/photo').read_bytes() == b'previous destination'
    write_file(source / 'duplicate/photo', b'photo')
    run_cli('resume', '--job-dir', str(job))
    assert file_rows(job)['original/photo']['status'] == 'copied_from_fallback'


@pytest.mark.parametrize('fallback', ['../outside', '/tmp/outside', 'link', 'original/photo'])
def test_fallback_rejects_unsafe_paths_without_touching_destination(tmp_path, fallback):
    source, dest, job = failed_pair(tmp_path)
    (source / 'link').symlink_to(source / 'duplicate/photo')
    result = run_cli('copy', '--job-dir', str(job), '--path', 'original/photo', '--fallback-from', fallback, check=False)
    assert result.returncode == 1
    assert not (dest / 'original/photo').exists()
    assert file_rows(job)['original/photo']['status'] == manifest.UNREADABLE_COMPRESSED


def test_fallback_requires_pair_of_options_and_failed_row(tmp_path):
    _, _, job = failed_pair(tmp_path)
    for args in [('--path', 'original/photo'), ('--fallback-from', 'duplicate/photo'),
                 ('--path', 'duplicate/photo', '--fallback-from', 'original/photo')]:
        assert run_cli('copy', '--job-dir', str(job), *args, check=False).returncode == 1


def test_compressed_diagnosis_requires_read_error_flag_and_missing_metadata(tmp_path, monkeypatch):
    calls = []

    class Ops:
        def get(self, path, name, *, show_compression=False):
            calls.append((name, show_compression))
            raise OSError(errno.ENODATA, 'missing')

    monkeypatch.setattr(copier, 'MacOSXattrOps', Ops)
    info = SimpleNamespace(st_flags=copier.UF_COMPRESSED)
    path = tmp_path / 'photo'
    assert copier.compressed_read_failure(path, info, OSError(errno.EIO, 'I/O')) is None
    assert copier.compressed_read_failure(path, SimpleNamespace(st_flags=0), OSError(errno.ENOTSUP, 'unsupported')) is None
    reason = copier.compressed_read_failure(path, info, OSError(errno.ENOTSUP, 'unsupported'))
    assert 'not addressable' in reason and 'ENOTSUP' in reason
    assert calls == [('com.apple.decmpfs', True)]
    monkeypatch.setattr(Ops, 'get', lambda *args, **kwargs: b'valid header')
    assert copier.compressed_read_failure(path, info, OSError(errno.ENOTSUP, 'unsupported')) is None


def test_compressed_worker_failure_never_publishes_zero_filled_output(tmp_path, monkeypatch):
    source = tmp_path / 'photo'
    dest = tmp_path / 'recovered'
    temp = tmp_path / 'temp'
    source.write_bytes(b'photo')
    dest.write_bytes(b'previous')
    temp.write_bytes(b'')
    info = SimpleNamespace(st_flags=copier.UF_COMPRESSED, st_mode=stat.S_IFREG, st_mtime_ns=0)
    original_stat, original_open = type(source).stat, type(source).open

    def fake_stat(path, *args, **kwargs):
        return info if path == source else original_stat(path, *args, **kwargs)

    def fake_open(path, *args, **kwargs):
        if path == source:
            raise OSError(errno.ENOTSUP, 'unsupported read')
        return original_open(path, *args, **kwargs)

    class Ops:
        def get(self, *args, **kwargs):
            raise OSError(errno.ENODATA, 'missing')

    monkeypatch.setattr(type(source), 'stat', fake_stat)
    monkeypatch.setattr(type(source), 'open', fake_open)
    monkeypatch.setattr(copier, 'MacOSXattrOps', Ops)
    result = copier._copy_one_in_worker(source, dest, temp, 5)
    assert result['status'] == manifest.UNREADABLE_COMPRESSED
    assert result['copied_bytes'] == 0
    assert dest.read_bytes() == b'previous'
    assert not temp.exists()


def test_compressed_status_is_distinct_in_reports_and_next(tmp_path):
    _, _, job = failed_pair(tmp_path)
    assert 'unreadable-compressed-flag=1' in run_cli('status', '--job-dir', str(job)).stdout
    report = reporting.customer_markdown_report(job)
    assert '| Unreadable compressed files | 1 |' in report
    assert 'not addressable' in report
    assert 'unresolved files' in report
    assert b'ENOTSUP' in reporting.customer_pdf_bytes(job)
    assert 'need manual review' in run_cli('next', '--job-dir', str(job)).stdout


def test_maximum_length_filename_can_be_copied(tmp_path):
    source, dest, job = volume_job(tmp_path)
    name = 'a' * 255
    write_file(source / name, b'content')
    run_cli('scan', '--job-dir', str(job))
    run_cli('copy', '--job-dir', str(job))
    assert (dest / name).read_bytes() == b'content'


def test_healthy_macos_compressed_file_is_readable_and_metadata_visible(tmp_path):
    import struct
    import sys
    import zlib

    if sys.platform != 'darwin' or not hasattr(os, 'chflags'):
        pytest.skip('requires macOS compression support')
    source = tmp_path / 'healthy-compressed'
    source.touch()
    content = b'healthy compressed fixture\n' * 20
    ops = copier.MacOSXattrOps()
    header = struct.pack('<IIQ', 0x636D7066, 3, len(content)) + zlib.compress(content)
    try:
        ops.set(source, 'com.apple.decmpfs', header)
        os.chflags(source, copier.UF_COMPRESSED)
    except OSError as exc:
        pytest.skip(f'fixture filesystem does not support compression: {exc}')
    assert source.stat().st_flags & copier.UF_COMPRESSED
    assert source.read_bytes() == content
    assert ops.get(source, 'com.apple.decmpfs', show_compression=True) == header
    dest, temp = tmp_path / 'dest', tmp_path / 'temp'
    result = copier._copy_one_in_worker(source, dest, temp, len(content))
    assert result['status'] == 'copied'
    assert dest.read_bytes() == content
    assert not dest.stat().st_flags & copier.UF_COMPRESSED
