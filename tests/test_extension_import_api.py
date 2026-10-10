"""Browser imports are authenticated LAN requests, independent of marketplace scans."""
import asyncio
import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app import store, worker
from app.main import app
from test_tracker import sample


CHROME_ORIGIN = 'chrome-extension://' + 'a' * 32
FIREFOX_ORIGIN = 'moz-extension://b917b693-13f7-4cb6-aa68-2bcd7fddf14f'
BEARER = {'Authorization': 'Bearer test-import'}


def request_headers(origin=None, cross_site=False):
    headers = dict(BEARER)
    if origin is not None:
        headers['Origin'] = origin
    if cross_site:
        headers['Sec-Fetch-Site'] = 'cross-site'
    return headers


def inbox_file(name, items):
    path = store.DATA / 'inbox' / (name + '.json')
    path.write_text(json.dumps([item.model_dump() for item in items]))
    return path


def process_imports(settings=None, rate=1, rate_checked=True):
    async def check():
        def unexpected_request(request):
            raise AssertionError('Import processing unexpectedly requested ' + str(request.url))
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            await worker.process_imports(client, s=settings, rate=rate, rate_checked=rate_checked)
    asyncio.run(check())


@pytest.mark.parametrize('origin', [CHROME_ORIGIN, FIREFOX_ORIGIN])
def test_paired_extension_can_check_and_import_cross_site(origin):
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        headers = request_headers(origin, cross_site=True)
        response = client.get('/api/import/status', headers=headers)
        assert response.status_code == 200
        assert response.json()['ready'] is True
        assert response.headers['access-control-allow-origin'] == origin
        response = client.post('/api/import', headers=headers, json=[sample().model_dump()])
        assert response.status_code == 200
        assert response.json() == {'queued': 1}
        assert response.headers['access-control-allow-origin'] == origin
    assert len(list((store.DATA / 'inbox').glob('*.json'))) == 1
    assert store.runtime('import_requested') == '1'
    assert store.runtime('scan_requested') != '1'


@pytest.mark.parametrize('origin', [CHROME_ORIGIN, FIREFOX_ORIGIN])
@pytest.mark.parametrize('path,method', [('/api/import', 'POST'), ('/api/import/status', 'GET')])
def test_extension_preflight_allows_required_headers_and_private_network(origin, path, method):
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        response = client.options(path, headers={
            'Origin': origin,
            'Access-Control-Request-Method': method,
            'Access-Control-Request-Headers': 'authorization, content-type',
            'Access-Control-Request-Private-Network': 'true',
        })
    assert response.status_code in (200, 204)
    assert response.headers['access-control-allow-origin'] == origin
    assert method in response.headers['access-control-allow-methods']
    allowed = response.headers['access-control-allow-headers'].lower()
    assert 'authorization' in allowed and 'content-type' in allowed
    assert response.headers['access-control-allow-private-network'] == 'true'
    assert 'true' != response.headers.get('access-control-allow-credentials')


def test_preflight_does_not_add_private_network_permission_without_request():
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        response = client.options('/api/import', headers={
            'Origin': CHROME_ORIGIN,
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization, content-type',
        })
    assert response.status_code in (200, 204)
    assert 'access-control-allow-private-network' not in response.headers


@pytest.mark.parametrize('origin', [
    'https://evil.example',
    'null',
    'chrome-extension://' + 'q' * 32,
    'chrome-extension://' + 'a' * 31,
    CHROME_ORIGIN + '.evil.example',
    CHROME_ORIGIN + '/path',
    'moz-extension://not-a-uuid',
    FIREFOX_ORIGIN + '/path',
])
def test_unknown_or_malformed_origins_rejected_even_with_valid_token(origin):
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        for path, method in (('/api/import/status', 'GET'), ('/api/import', 'POST')):
            kwargs = {'json': [sample().model_dump()]} if method == 'POST' else {}
            response = client.request(method, path, headers=request_headers(origin), **kwargs)
            assert response.status_code == 403
            assert 'access-control-allow-origin' not in response.headers
        response = client.options('/api/import', headers={
            'Origin': origin,
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization, content-type',
        })
        assert response.status_code == 403
    assert not list((store.DATA / 'inbox').glob('*.json'))


@pytest.mark.parametrize('authorization', [None, 'Bearer incorrect', 'Basic YWRtaW46dGVzdC1wYXNzd29yZA=='])
def test_import_endpoints_require_bearer_token_not_dashboard_authentication(authorization):
    headers = {'Origin': CHROME_ORIGIN}
    if authorization:
        headers['Authorization'] = authorization
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        assert client.get('/api/import/status', headers=headers).status_code == 401
        assert client.post('/api/import', headers=headers, json=[sample().model_dump()]).status_code == 401


def test_invalid_extension_pairing_returns_readable_authentication_error():
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        response = client.post('/api/import', headers={
            'Origin': CHROME_ORIGIN,
            'Sec-Fetch-Site': 'cross-site',
            'Authorization': 'Bearer wrong-pairing-token',
        }, json=[sample().model_dump()])
    assert response.status_code == 401
    assert response.headers['access-control-allow-origin'] == CHROME_ORIGIN
    assert not list((store.DATA / 'inbox').glob('*.json'))


def test_machine_import_without_origin_still_works():
    with TestClient(app, client=('127.0.0.1', 123)) as client:
        assert client.get('/api/import/status', headers=BEARER).status_code == 200
        response = client.post('/api/import', headers=BEARER, json=[sample().model_dump()])
        assert response.status_code == 200
        assert 'access-control-allow-origin' not in response.headers
    assert store.runtime('import_requested') == '1'
    assert store.runtime('scan_requested') != '1'


def test_extension_does_not_bypass_lan_restriction():
    with TestClient(app, client=('8.8.8.8', 123)) as client:
        headers = request_headers(CHROME_ORIGIN)
        assert client.get('/api/import/status', headers=headers).status_code == 403
        assert client.post('/api/import', headers=headers, json=[sample().model_dump()]).status_code == 403
        assert client.options('/api/import', headers={
            'Origin': CHROME_ORIGIN,
            'Access-Control-Request-Method': 'POST',
        }).status_code == 403
        assert client.get('/browser-extension/download', auth=('admin', 'test-password')).status_code == 403


def test_extension_cross_site_exception_does_not_weaken_dashboard_csrf():
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        headers = request_headers(CHROME_ORIGIN, cross_site=True)
        headers['X-CSRF-Token'] = 'test-csrf'
        assert client.post('/scan', headers=headers, auth=('admin', 'test-password')).status_code == 403
        assert client.post('/scan', auth=('admin', 'test-password')).status_code == 403
        assert client.post('/api/import', headers={**BEARER, 'Sec-Fetch-Site': 'cross-site'}, json=[]).status_code == 403
    assert store.runtime('scan_requested') != '1'


def test_paired_zip_requires_auth_and_contains_only_pairing_secret(monkeypatch):
    monkeypatch.setenv('LAN_IP', '192.168.0.22')
    monkeypatch.setenv('PORT', '8765')
    with TestClient(app, client=('192.168.0.10', 123)) as client:
        assert client.get('/browser-extension/download').status_code == 401
        response = client.get('/browser-extension/download', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert 'attachment' in response.headers['content-disposition']
    assert 'no-store' in response.headers['cache-control']
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert 'product-watcher-extension/manifest.json' in names
        assert 'product-watcher-extension/paired-config.json' in names
        assert all(name.startswith('product-watcher-extension/') and '..' not in name.split('/') for name in names)
        config = json.loads(archive.read('product-watcher-extension/paired-config.json'))
        assert config == {'dashboardUrl': 'http://192.168.0.22:8765', 'importToken': 'test-import'}
        assert isinstance(json.loads(archive.read('product-watcher-extension/manifest.json')), dict)
        for name in names:
            if name.endswith('/'):
                continue
            content = archive.read(name)
            assert b'test-password' not in content
            assert b'test-csrf' not in content
            if name != 'product-watcher-extension/paired-config.json':
                assert b'test-import' not in content


def test_paired_zip_reflects_current_token_and_dashboard_address(monkeypatch):
    monkeypatch.setenv('IMPORT_TOKEN', 'rotated-test-token')
    monkeypatch.setenv('LAN_IP', '192.168.0.22')
    monkeypatch.setenv('PORT', '8765')
    with TestClient(app, base_url='http://attacker-controlled.example:1234', client=('192.168.0.10', 123)) as client:
        response = client.get('/browser-extension/download', auth=('admin', 'test-password'))
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        config = json.loads(archive.read('product-watcher-extension/paired-config.json'))
    assert config == {'dashboardUrl': 'http://192.168.0.22:8765', 'importToken': 'rotated-test-token'}


def test_manual_import_queues_processing_without_requesting_marketplace_scan():
    with TestClient(app, client=('127.0.0.1', 123)) as client:
        response = client.post('/import', auth=('admin', 'test-password'),
                               headers={'X-CSRF-Token': 'test-csrf'},
                               data={'json_data': json.dumps([sample().model_dump()])})
    assert response.status_code == 200
    assert store.runtime('import_requested') == '1'
    assert store.runtime('scan_requested') != '1'


def test_import_processing_works_while_monitoring_is_paused_without_browser_calls(monkeypatch):
    store.save({'paused': True, 'ntfy_enabled': False})
    path = inbox_file('paused', [sample()])
    async def unexpected_browser(*args, **kwargs):
        raise AssertionError('Import processing initiated a marketplace browser scan')
    monkeypatch.setattr(worker, 'browser_scan', unexpected_browser)
    monkeypatch.setattr(worker, 'allegro', unexpected_browser)
    process_imports()
    assert not path.exists()
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 1
        assert connection.execute('SELECT matched FROM listings').fetchone()[0] == 1
    assert store.settings()['paused'] is True
    assert store.runtime('scan_requested') != '1'
    assert store.runtime('import_pending') == '0'
    assert store.runtime('import_last_count') == '1'


def test_worker_poll_processes_import_request_while_monitoring_is_paused(monkeypatch):
    store.save({'paused': True, 'ntfy_enabled': False})
    inbox_file('worker-paused', [sample()])
    store.runtime('import_requested', '1')
    async def unexpected_scan(*args, **kwargs):
        raise AssertionError('Paused worker initiated a marketplace scan')
    real_sleep = asyncio.sleep
    async def stop_after_poll(seconds):
        await real_sleep(0)  # Give the independent importer task one worker poll.
        raise asyncio.CancelledError
    monkeypatch.setattr(worker, 'scan', unexpected_scan)
    monkeypatch.setattr(worker.asyncio, 'sleep', stop_after_poll)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run())
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 1
    assert store.runtime('import_pending') == '0'
    assert store.runtime('scan_requested') != '1'


def test_disabled_pln_source_does_not_block_enabled_eur_import():
    store.save({'allegro_enabled': False, 'ntfy_enabled': False})
    path = inbox_file('disabled-pln', [sample(), sample(source='allegro', external_id='2',
                      url='https://allegro.pl/oferta/2', currency='PLN', price=1000)])
    process_imports(rate=None)
    assert not path.exists()
    with store.db() as connection:
        rows = connection.execute('SELECT source,external_id FROM listings').fetchall()
        assert [tuple(row) for row in rows] == [('vinted', '1')]
    assert store.runtime('import_pending') == '0'


def test_mixed_batch_waits_for_pln_conversion_then_imports_atomically():
    store.save({'ntfy_enabled': False})
    path = inbox_file('needs-rate', [sample(), sample(source='allegro', external_id='2',
                      url='https://allegro.pl/oferta/2', currency='PLN', price=1000, shipping=0, fees=0)])
    process_imports(rate=None)
    assert path.exists()
    assert store.runtime('import_pending') == '1'
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 0
    process_imports(rate=4.2)
    assert not path.exists()
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 2
        row = connection.execute("SELECT total_eur,fx_rate FROM listings WHERE source='allegro'").fetchone()
        assert tuple(row) == (238.1, 4.2)
    assert store.runtime('import_pending') == '0'
    assert store.runtime('import_last_count') == '2'


def test_corrupt_import_is_quarantined_without_blocking_next_file():
    store.save({'ntfy_enabled': False})
    invalid = store.DATA / 'inbox' / 'a-invalid.json'
    invalid.write_text('{invalid json')
    valid = inbox_file('b-valid', [sample()])
    process_imports()
    assert not invalid.exists()
    assert not valid.exists()
    assert any(path.name.startswith('a-invalid') for path in (store.DATA / 'inbox').iterdir())
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM logs WHERE source='import' AND level='error'").fetchone()[0] >= 1
    assert store.runtime('import_pending') == '0'


def test_reimport_deduplicates_listings_history_and_notifications():
    inbox_file('first', [sample()])
    process_imports()
    inbox_file('again', [sample()])
    process_imports()
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM prices').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1
    inbox_file('price-drop', [sample(price=200)])
    process_imports()
    with store.db() as connection:
        assert connection.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM prices').fetchone()[0] == 2
        assert connection.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 2
