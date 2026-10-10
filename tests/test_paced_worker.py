"""Worker integration checks without browser or marketplace network access."""
import asyncio
import contextlib
import json

import pytest

from app import pacing, store, worker
from app.browser_sources import BrowserBlocked, BrowserResult
from app.domain import Listing


def item(ident='101'):
    return Listing(source='vinted', external_id=ident, title='Used night vision Gen 2',
                   description='Analog image intensifier generation 2',
                   url=f'https://www.vinted.lt/items/{ident}', price=200, used=True)


async def rate(client):
    return 4.2


def test_worker_checks_one_keyword_per_source_and_waits_for_next_step(monkeypatch):
    store.save({'keywords':['night vision','NVG30','noktowizor'], 'interval':15, 'ntfy_enabled':False})
    clock=[1000.0]
    monkeypatch.setattr(worker.time,'time',lambda:clock[0])
    monkeypatch.setattr(worker,'exchange_rate',rate)
    calls=[]
    async def browser(source,s,effective_rate):
        calls.append((source,list(s['keywords']),s['_detail_budget']))
        return BrowserResult([], searches=1, details=2, attempts=2, partial=True, note='Mock step')
    monkeypatch.setattr(worker,'browser_scan',browser)
    assert asyncio.run(worker.scan(None,handle_imports=False)) is True
    assert calls==[(name,['night vision'],2) for name in pacing.SOURCES]
    for name in pacing.SOURCES:
        saved=json.loads(store.runtime(name+'_pacing'))
        assert saved['cursor']==1 and saved['rotation_attempts']==2 and saved['next_due']==1300
    clock[0]=1005
    assert asyncio.run(worker.scan(None,handle_imports=False)) is False
    assert len(calls)==3
    clock[0]=1300
    assert asyncio.run(worker.scan(None,handle_imports=False)) is True
    assert calls[3:]==[(name,['NVG30'],2) for name in pacing.SOURCES]


def test_pre_reserved_plan_is_not_reserved_twice_by_scan(monkeypatch):
    store.save({'keywords':['night vision','NVG30'], 'ntfy_enabled':False,
                'skelbiu_enabled':False,'allegro_enabled':False})
    monkeypatch.setattr(worker.time,'time',lambda:1000)
    plan=pacing.due_sources(store.settings(),now=1000)
    def duplicate_reservation(*args,**kwargs):raise AssertionError('Scan reserved an existing plan again')
    monkeypatch.setattr(pacing,'due_sources',duplicate_reservation)
    monkeypatch.setattr(worker,'exchange_rate',rate)
    calls=[]
    async def browser(source,s,effective_rate):
        calls.append((source,s['keywords']))
        return BrowserResult([],searches=1,note='Mock step')
    monkeypatch.setattr(worker,'browser_scan',browser)
    assert asyncio.run(worker.scan(None,handle_imports=False,plan=plan)) is True
    assert calls==[('vinted',['night vision'])]
    assert json.loads(store.runtime('vinted_pacing'))['cursor']==1


def test_import_loop_finishes_while_browser_scan_is_waiting(monkeypatch):
    store.save({'keywords':['night vision'], 'ntfy_enabled':False,
                'skelbiu_enabled':False,'allegro_enabled':False})
    monkeypatch.setattr(worker,'exchange_rate',rate)
    async def check():
        started=asyncio.Event();release=asyncio.Event();imported=asyncio.Event()
        async def browser(source,s,effective_rate):
            started.set()
            await release.wait()
            return BrowserResult([],searches=1,note='Mock slow browser')
        monkeypatch.setattr(worker,'browser_scan',browser)
        original=worker.process_imports
        async def process(client,*args,**kwargs):
            count=await original(client,*args,**kwargs)
            if count:imported.set()
            return count
        monkeypatch.setattr(worker,'process_imports',process)
        scan=asyncio.create_task(worker.scan(None,handle_imports=False))
        importer=None
        try:
            await asyncio.wait_for(started.wait(),1)
            path=store.DATA/'inbox'/'browser-upload.json'
            path.write_text(json.dumps([item().model_dump()]))
            importer=asyncio.create_task(worker.import_loop(None))
            await asyncio.wait_for(imported.wait(),1)
            assert path.with_suffix('.processed').exists()
            assert not scan.done()
            with store.db() as c:
                row=c.execute('SELECT external_id,matched FROM listings').fetchone()
                assert tuple(row)==('101',1)
            assert store.runtime('import_last_count')=='1'
            assert store.runtime('scan_state')=='running'
            release.set()
            assert await asyncio.wait_for(scan,1) is True
        finally:
            release.set()
            for task in (scan,importer):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):await task
    asyncio.run(check())


def test_blocked_scan_saves_partial_items_and_accounts_for_detail_attempt(monkeypatch):
    store.save({'keywords':['night vision','NVG30'], 'ntfy_enabled':False,
                'skelbiu_enabled':False,'allegro_enabled':False})
    monkeypatch.setattr(worker.time,'time',lambda:1000)
    monkeypatch.setattr(worker,'exchange_rate',rate)
    async def browser(source,s,effective_rate):
        store.runtime('vinted_browser_blocked_until',5000)
        raise BrowserBlocked('Mock access block',items=[item()],attempts=1,next_retry=5000)
    monkeypatch.setattr(worker,'browser_scan',browser)
    assert asyncio.run(worker.scan(None,handle_imports=False)) is True
    saved=json.loads(store.runtime('vinted_pacing'))
    assert saved['cursor']==1 and saved['rotation_attempts']==1 and saved['next_due']==5000
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM listings').fetchone()[0]==1
        assert c.execute("SELECT status FROM sources WHERE name='vinted'").fetchone()[0]=='blocked'
    assert pacing.due_sources(store.settings(),force=True,now=4999)==[]


def test_empty_import_polls_make_no_runtime_writes(monkeypatch):
    original=store.runtime;writes=[]
    def runtime(key,value=None):
        if value is not None:writes.append((key,value))
        return original(key,value)
    monkeypatch.setattr(store,'runtime',runtime)
    for _ in range(3):assert asyncio.run(worker.process_imports(None))==0
    assert writes==[]
    original('import_pending',2);original('import_requested','1')
    assert asyncio.run(worker.process_imports(None))==0
    assert writes==[('import_pending',0),('import_requested',0)]
    writes.clear()
    assert asyncio.run(worker.process_imports(None))==0
    assert writes==[]


@pytest.mark.parametrize('change,expected',[
    ({'skelbiu_enabled':False}, ['vinted','allegro']),
    ({'paused':True}, ['vinted']),
    ({'keywords':['NVG30']}, ['vinted']),
])
def test_controls_changed_during_first_source_stop_later_reserved_work(monkeypatch,change,expected):
    store.save({'keywords':['night vision'], 'paused':False, 'ntfy_enabled':False})
    monkeypatch.setattr(worker,'exchange_rate',rate)
    async def check():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def browser(source,s,effective_rate):
            calls.append(source)
            if source=='vinted':
                started.set()
                await release.wait()
            return BrowserResult([],searches=1,note='Mock scan')
        monkeypatch.setattr(worker,'browser_scan',browser)
        task=asyncio.create_task(worker.scan(None,handle_imports=False))
        try:
            await asyncio.wait_for(started.wait(),1)
            store.save(change)
            release.set()
            await asyncio.wait_for(task,1)
            assert calls==expected
            if change.get('skelbiu_enabled') is False:
                with store.db() as c:
                    assert c.execute("SELECT status FROM sources WHERE name='skelbiu'").fetchone()[0]=='disabled'
        finally:
            release.set();task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await task
    asyncio.run(check())
