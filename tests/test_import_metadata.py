import io
import json
import zipfile

from fastapi.testclient import TestClient
import pytest

from app import main, store, worker
from test_tracker import sample
from test_extension_import_api import inbox_file, process_imports


def test_search_import_keeps_existing_detail_metadata_and_current_costs():
    detailed=sample(title='Night vision monocular',description='Used analog Gen 2 intensifier',
                    image='https://images.example/item.jpg',shipping=10,fees=5,used=True)
    ident=worker.ingest(detailed,1,store.settings())
    inbox_file('search-card',[detailed.model_copy(update={'description':'','image':'','used':None,
                                                         'price':200,'shipping':None,'fees':None})])
    process_imports()
    with store.db() as c:
        row=c.execute('SELECT * FROM listings WHERE id=?',(ident,)).fetchone()
        assert row['matched']==1 and row['category']=='gen2'
        assert row['description']==detailed.description and row['image']==detailed.image
        assert row['used']==1 and row['price']==200
        assert row['shipping'] is None and row['fees'] is None and row['total_eur']==200
        assert c.execute('SELECT COUNT(*) FROM notifications WHERE listing_id=?',(ident,)).fetchone()[0]==2


def test_changed_title_and_explicit_condition_replace_import_metadata():
    detailed=sample(title='Night vision monocular',description='Used analog Gen 2 intensifier',used=True)
    ident=worker.ingest(detailed,1,store.settings())
    inbox_file('renamed',[detailed.model_copy(update={'title':'Replacement camera','description':'','used':False})])
    process_imports()
    with store.db() as c:
        row=c.execute('SELECT * FROM listings WHERE id=?',(ident,)).fetchone()
        assert row['description']=='' and row['used']==0 and row['matched']==0


@pytest.mark.parametrize('setting,value',[('IMPORT_TOKEN',''),('LAN_IP','not-an-ip'),('LAN_IP','8.8.8.8'),('PORT','bad')])
def test_unconfigured_pairing_fails_cleanly(monkeypatch,setting,value):
    monkeypatch.setenv(setting,value)
    with TestClient(main.app,client=('127.0.0.1',123)) as client:
        assert client.get('/browser-extension/download',auth=('admin','test-password')).status_code==503


def test_download_packages_only_intended_files_and_skips_symlinks(tmp_path,monkeypatch):
    folder=tmp_path/'browser-extension';folder.mkdir()
    (folder/'manifest.json').write_text(json.dumps({'manifest_version':3}))
    (folder/'README.md').write_text('Public documentation')
    secret=tmp_path/'private.env';secret.write_text('PRIVATE_TEST_SECRET')
    (folder/'background.js').symlink_to(secret)
    (folder/'backup.env').write_text('PRIVATE_TEST_SECRET')
    (folder/'paired-config.json').write_text('PRIVATE_TEST_SECRET')
    monkeypatch.setattr(main,'__file__',str(tmp_path/'app/main.py'))
    with TestClient(main.app,client=('127.0.0.1',123)) as client:
        response=client.get('/browser-extension/download',auth=('admin','test-password'))
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist())=={'product-watcher-extension/manifest.json',
                                        'product-watcher-extension/README.md',
                                        'product-watcher-extension/paired-config.json'}
        assert all(b'PRIVATE_TEST_SECRET' not in archive.read(name) for name in archive.namelist())
