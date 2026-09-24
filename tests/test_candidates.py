import json
import shlex

from helpers import file_rows, init_and_scan, run_cli, write_file
from macos_data_rescue.candidates import duplicate_candidates
from macos_data_rescue.manifest import connect


def test_candidates_are_ranked_read_only_and_require_explicit_selection(tmp_path):
    source = tmp_path / 'source-home'
    for name, content in [('a/report.txt', b'123'), ('b/report.txt', b'456'),
                          ("c/quote's.txt", b'789'), ('d/large.txt', b'12345')]:
        write_file(source / name, content)
    job, _, dest = init_and_scan(tmp_path)
    with connect(job) as conn:
        conn.execute("update files set status = 'failed', error = 'read failed' where relative_path = 'a/report.txt'")
    conn.close()
    before = {key: dict(row) for key, row in file_rows(job).items()}
    source.rename(tmp_path / 'disconnected')
    result = run_cli('find-duplicates', '--job-dir', str(job), '--path', 'a/report.txt', '--format', 'json')
    payload = json.loads(result.stdout)
    assert payload['candidates'][0]['path'] == 'b/report.txt'
    assert payload['total_candidates'] == 2
    assert 'not proof' in payload['notice']
    assert shlex.split(payload['candidates'][1]['command'])[-1] == "c/quote's.txt"
    assert len(duplicate_candidates(job, 'a/report.txt', limit=1)['candidates']) == 1
    assert {key: dict(row) for key, row in file_rows(job).items()} == before
    assert not dest.exists()
    assert run_cli('find-duplicates', '--job-dir', str(job), '--path', 'd/large.txt', check=False).returncode == 1
