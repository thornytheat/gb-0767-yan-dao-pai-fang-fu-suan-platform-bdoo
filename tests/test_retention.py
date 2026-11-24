import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.archive.certificate import verify
from app.main import create_app
from app.monitor.source import sample


def make_shared_batch(client):
    files = sample('dirty')
    first = client.post('/api/runs', json={'files': files}).json['run_id']
    shared = files | {'reports': files['reports'] + '\r\n'}
    second = client.post('/api/runs', json={'files': shared}).json['run_id']
    return first, second


def test_cleaned_batch_keeps_only_hours(client):
    run_id = client.post('/api/runs', json={'sample': 'dirty'}).json['run_id']
    response = client.post(f'/api/runs/{run_id}/cleanup')
    assert response.status_code == 200
    stored = client.application.extensions['monitor_archive'].lookup(run_id)
    assert 'originals' not in stored
    assert set(stored) == {'manifest', 'results'}
    assert set(stored['results']) == {'hours'}
    assert client.get(f'/api/runs/{run_id}/hours').status_code == 200
    assert client.get(f'/api/runs/{run_id}/validity').status_code == 410


def test_shared_original_survives_until_last_release(client):
    first, second = make_shared_batch(client)
    first_report = client.post(f'/api/runs/{first}/cleanup').json
    assert len(first_report['retained_originals']) == 3
    assert len(first_report['deleted_originals']) == 1

    overview = client.get('/api/retention').json
    by_hash = {row['sha256']: row for row in overview['originals']}
    assert sum(row['references'] == 1 and row['stored'] for row in by_hash.values()) == 4
    assert client.get(f'/api/export/{second}').status_code == 200

    client.post(f'/api/runs/{second}/cleanup')
    final = client.get('/api/retention').json['originals']
    assert all(row['references'] == 0 and not row['stored'] for row in final)


def test_issue_and_cleanup_in_one_submission_remains_verifiable(client):
    run_id = client.post('/api/runs', json={'sample': 'dirty'}).json['run_id']
    response = client.post(f'/api/runs/{run_id}/cleanup', json={'issue': True})
    assert response.status_code == 200
    body = response.json
    certificate = body['certificate']
    assert body['cleaned'] is True
    assert verify(certificate)['valid'] is True
    assert client.post('/api/evidence/verify', json=certificate).json['valid'] is True
    assert client.get(f'/api/export/{run_id}').status_code == 410


def test_rerun_cleaned_batch_does_not_restore_originals(client, tmp_path):
    database = tmp_path / 'rerun.sqlite'
    app = create_app(database)
    local = app.test_client()
    files = sample('dirty')
    run_id = local.post('/api/runs', json={'files': files}).json['run_id']
    local.post(f'/api/runs/{run_id}/cleanup')
    before = sqlite3.connect(database).execute('SELECT COUNT(*) FROM originals').fetchone()[0]

    rerun = local.post('/api/runs', json={'files': files}).json
    assert rerun['reused'] is True
    assert rerun['cleaned'] is True
    after = sqlite3.connect(database).execute('SELECT COUNT(*) FROM originals').fetchone()[0]
    assert before == after == 0


def test_concurrent_cleanup_and_exports_never_issue_invalid_certificate(tmp_path):
    from app.archive.store import Archive

    path = tmp_path / 'concurrent.sqlite'
    app = create_app(path)
    client = app.test_client()
    run_id = client.post('/api/runs', json={'sample': 'dirty'}).json['run_id']

    def export():
        response = client.get(f'/api/export/{run_id}')
        return response

    def cleanup():
        return client.post(f'/api/runs/{run_id}/cleanup', json={'issue': True})

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda fn: fn(), [export, cleanup]))
    for response in outcomes:
        if response.status_code == 200 and 'sha256' in response.json:
            assert verify(response.json)['valid'] is True
        else:
            assert response.status_code in {410, 200}
    assert Archive(path).lookup(run_id).get('originals') is None
