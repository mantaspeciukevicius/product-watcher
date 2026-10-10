import pytest
@pytest.fixture(autouse=True)
def database(tmp_path,monkeypatch):
    from app import store
    monkeypatch.setattr(store,'DATA',tmp_path)
    monkeypatch.setenv('ADMIN_PASSWORD','test-password')
    monkeypatch.setenv('CSRF_TOKEN','test-csrf')
    monkeypatch.setenv('IMPORT_TOKEN','test-import')
    monkeypatch.setenv('ALLOWED_CIDRS','127.0.0.0/8,192.168.0.0/24')
    store.init(); store.save({'ntfy_topic':'test-only-topic'})
