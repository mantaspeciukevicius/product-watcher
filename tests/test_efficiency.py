import asyncio,time,contextlib
import pytest
from app import store
from app.worker import ingest_many,reevaluate_history,heartbeat,run
from test_tracker import sample

def test_batch_ingestion_uses_short_atomic_transactions(monkeypatch):
    original=store.db; opens=[];statements=[]
    @contextlib.contextmanager
    def traced():
        opens.append(1)
        with original() as c:
            c.set_trace_callback(statements.append)
            yield c
    settings=store.settings()
    monkeypatch.setattr(store,'db',traced)
    ids=ingest_many([sample(external_id=str(i)) for i in range(125)],4.2,settings)
    assert len(ids)==len(set(ids))==125
    assert len(opens)==3
    assert sum(s=='BEGIN IMMEDIATE' for s in statements)==3
    assert sum(s.startswith('SELECT source,external_id,title') for s in statements)==3

def test_history_pass_skips_unchanged_filters_and_preserves_history():
    settings=store.settings();ids=ingest_many([sample(),sample(external_id='2',currency='PLN',price=1000)],4.2,settings)
    assert reevaluate_history(settings,4.2)==2
    with store.db() as c:
        before=[tuple(r) for r in c.execute('SELECT first_seen,last_seen FROM listings ORDER BY id')]
        prices=c.execute('SELECT COUNT(*) FROM prices').fetchone()[0]
    assert reevaluate_history(settings,4.2)==0
    settings['max_price']=100
    assert reevaluate_history(settings,4.2)==2
    with store.db() as c:
        assert [tuple(r) for r in c.execute('SELECT first_seen,last_seen FROM listings ORDER BY id')]==before
        assert c.execute('SELECT COUNT(*) FROM prices').fetchone()[0]==prices
        assert c.execute('SELECT SUM(matched) FROM listings').fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM notifications WHERE state='pending'").fetchone()[0]==0
    assert reevaluate_history(settings,4.3)==2

def test_heartbeat_runs_during_async_scan_work():
    async def check():
        task=asyncio.create_task(heartbeat(.01))
        try:
            await asyncio.sleep(.025)
            a=float(store.runtime('heartbeat'))
            await asyncio.sleep(.025)
            assert float(store.runtime('heartbeat'))>a
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(check())

def test_worker_restart_honors_saved_schedule(monkeypatch):
    store.save({'ntfy_enabled':False})
    store.runtime('last_scan',time.time())
    scans=[]
    async def unexpected_scan(client): scans.append(1)
    async def stop(seconds): raise asyncio.CancelledError
    monkeypatch.setattr('app.worker.scan',unexpected_scan)
    monkeypatch.setattr('app.worker.asyncio.sleep',stop)
    with pytest.raises(asyncio.CancelledError):asyncio.run(run())
    assert scans==[]

def test_saved_pln_filters_update_during_rate_outage_without_new_alerts():
    settings=store.settings();settings['max_price']=100
    ingest_many([sample(currency='PLN',price=1000)],4.2,settings)
    settings['max_price']=400
    assert reevaluate_history(settings,None)==1
    with store.db() as c:
        row=c.execute('SELECT total_eur,matched,fx_rate FROM listings').fetchone()
        assert tuple(row)==(239.29,1,4.2)
        assert c.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==0
    settings['max_price']=100
    reevaluate_history(settings,None)
    with store.db() as c: assert c.execute('SELECT matched FROM listings').fetchone()[0]==0

def test_notification_test_is_not_also_sent_by_worker(monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from app.main import app
    from app.worker import deliver
    calls=[]
    async def send(client,n,s):
        with store.db() as c: assert c.execute('SELECT state FROM notifications').fetchone()[0]=='sending'
        def unexpected_network(req): raise AssertionError('Worker tried to send a manual test')
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_network)) as worker_client:
            await deliver(worker_client)
        calls.append(n)
    monkeypatch.setattr('app.main.notify',send)
    with TestClient(app,client=('127.0.0.1',123)) as client:
        client.auth=('admin','test-password')
        result=client.post('/notification/test',headers={'X-CSRF-Token':'test-csrf'})
        assert result.status_code==200
    assert len(calls)==1
    with store.db() as c: assert c.execute('SELECT state FROM notifications').fetchone()[0]=='sent'
