from fastapi.testclient import TestClient
from app import store
from app.main import app
from app.worker import ingest
from test_tracker import sample

def client():
    c=TestClient(app,client=('127.0.0.1',123)); c.auth=('admin','test-password');return c

def test_discovery_sorting_and_priority():
    s=store.settings()
    old=ingest(sample(external_id='old'),4.2,s)
    new=ingest(sample(external_id='new',title='NVG30 night vision'),4.2,s)
    with store.db() as db:
        db.execute('UPDATE listings SET first_seen=100,favorite=1 WHERE id=?',(old,))
        db.execute('UPDATE listings SET first_seen=200 WHERE id=?',(new,))
    with client() as c:
        for query,first,second in [('',new,old),('&sort=oldest',old,new),('&sort=priority',old,new),('&sort=bogus',new,old)]:
            text=c.get('/listings?view=all'+query).text
            assert text.index(f'id="listing-{first}"')<text.index(f'id="listing-{second}"')
        assert 'Found 01 Jan' in text

def test_feedback_generalizes_persists_and_undoes():
    s=store.settings()
    original=ingest(sample(title='Baby monitor night vision analog Gen 2'),4.2,s)
    sibling=ingest(sample(external_id='other',title='Infant nursery camera night vision Gen 2'),4.2,s)
    good=ingest(sample(external_id='good',title='PVS-14 night vision Gen 2'),4.2,s)
    with client() as c:
        assert c.post(f'/listing/{original}/irrelevant').status_code==403
        r=c.post(f'/listing/{original}/irrelevant',headers={'X-CSRF-Token':'test-csrf','HX-Request':'true'})
        assert r.status_code==200 and r.headers['HX-Refresh']=='true'
        with store.db() as db:
            assert db.execute('SELECT matched FROM listings WHERE id=?',(sibling,)).fetchone()[0]==0
            assert db.execute('SELECT matched FROM listings WHERE id=?',(good,)).fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM notifications WHERE state='pending' AND listing_id IN (?,?)",(original,sibling)).fetchone()[0]==0
        future=ingest(sample(external_id='future',title='Kudikio night vision Gen 2 camera'),4.2,s)
        store.init()
        ingest(sample(title='Baby monitor night vision analog Gen 2',price=200),4.2,s)
        with store.db() as db:
            assert db.execute('SELECT irrelevant,matched FROM listings WHERE id=?',(original,)).fetchone()[:]==(1,0)
            assert db.execute('SELECT matched FROM listings WHERE id=?',(future,)).fetchone()[0]==0
        assert 'Baby monitor' in c.get('/listings?view=irrelevant').text
        c.post(f'/listing/{original}/irrelevant',headers={'X-CSRF-Token':'test-csrf'})
        with store.db() as db:
            assert db.execute('SELECT matched FROM listings WHERE id=?',(sibling,)).fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM notifications WHERE state='pending' AND listing_id IN (?,?)",(original,sibling)).fetchone()[0]==0

def test_similar_titles_and_ignore_does_not_learn():
    s=store.settings()
    bad=ingest(sample(title='Night vision Acme smart wireless indoor monitor'),4.2,s)
    with client() as c:
        c.post(f'/listing/{bad}/irrelevant',headers={'X-CSRF-Token':'test-csrf'})
        repeat=ingest(sample(external_id='repeat',title='Night vision Acme smart wireless indoor monitor black'),4.2,s)
        with store.db() as db: assert db.execute('SELECT matched FROM listings WHERE id=?',(repeat,)).fetchone()[0]==0
        ignored=ingest(sample(external_id='ignore'),4.2,s)
        c.post(f'/listing/{ignored}/ignored',headers={'X-CSRF-Token':'test-csrf'})
        other=ingest(sample(external_id='normal'),4.2,s)
        with store.db() as db: assert db.execute('SELECT matched FROM listings WHERE id=?',(other,)).fetchone()[0]==1
