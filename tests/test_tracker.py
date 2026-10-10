import asyncio,json,time
from pathlib import Path
import httpx,pytest
from fastapi.testclient import TestClient
from app import store
from app.domain import Listing,classify,matches,total_eur
from app.worker import ingest,scan,deliver
from app.integrations import allegro,exchange_rate,SourceError
from app.main import app

def sample(**changes):
    values=dict(source='vinted',external_id='1',title='Used night vision analog Gen 2+ monocular',url='https://www.vinted.lt/items/1',price=300,currency='EUR',shipping=5,fees=0,used=True)
    values.update(changes); return Listing(**values)

@pytest.mark.parametrize('title,category',[
 ('Naktinio matymo Gen 2+','gen2plus'),('Noktowizor generacji 2','gen2'),('Noktowizor generacja II+','gen2plus'),('Night vision NVG50 digital','nvg50'),('Digital NVG30','nvg30'),('PVS-14 analog night vision','analog_unknown'),('Digital night vision binoculars','excluded'),('FPV goggles Gen 2','excluded'),('Night vision thermal camera','excluded'),('Night vision Gen I','excluded'),('Noktowizor 1 generacji','excluded'),('PNV-57E night vision','excluded'),('Gen 3 night vision','other'),('Night vision replica Gen2','excluded')])
def test_classification(title,category): assert classify(sample(title=title))==category

def test_total_and_threshold():
    x=sample(price=1600,currency='PLN',shipping=80,fees=0)
    assert total_eur(x,4.2)==400
    assert not matches(x,'gen2plus',400,store.settings())
    assert matches(sample(),'gen2plus',305,store.settings())
    assert not matches(sample(used=False),'gen2plus',305,store.settings())

@pytest.mark.parametrize('change',[{'url':'http://www.vinted.lt/items/1'},{'url':'https://evil.example/x'},{'price':float('nan')},{'currency':'USD'},{'source':'evil'},{'image':'javascript:alert(1)'}])
def test_import_validation(change):
    with pytest.raises(ValueError): sample(**change)

def test_dedup_price_history_favorites_ignore():
    s=store.settings(); ident=ingest(sample(),4.2,s)
    ingest(sample(),4.2,s)
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM prices').fetchone()[0]==1
        c.execute('UPDATE listings SET favorite=1 WHERE id=?',(ident,))
    ingest(sample(price=250),4.2,s)
    ingest(sample(price=250),4.2,s)
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM prices').fetchone()[0]==2
        assert c.execute('SELECT favorite FROM listings').fetchone()[0]==1
        c.execute('UPDATE listings SET ignored=1 WHERE id=?',(ident,))
    ingest(sample(price=200),4.2,s)
    with store.db() as c: assert c.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==2
    store.init()
    with store.db() as c: assert c.execute('SELECT ignored FROM listings').fetchone()[0]==1

def test_filter_reevaluation_preserves_seen_time():
    s=store.settings(); ident=ingest(sample(),4.2,s)
    with store.db() as c: old=c.execute('SELECT last_seen FROM listings').fetchone()[0]
    s['max_price']=250; ingest(sample(),4.2,s,touch=False)
    with store.db() as c:
        r=c.execute('SELECT * FROM listings').fetchone(); assert r['matched']==0; assert r['last_seen']==old
        assert c.execute("SELECT COUNT(*) FROM notifications WHERE state='pending'").fetchone()[0]==0

def test_mock_sources():
    data=json.loads((Path(__file__).parent/'mock_marketplaces.json').read_text())
    for raw in data: ingest(Listing(**raw),4.2,store.settings())
    with store.db() as c: assert c.execute('SELECT COUNT(*) FROM listings WHERE matched=1').fetchone()[0]==3

def test_currency_mock_and_cache():
    async def go():
        calls=[]
        def handler(req):
            calls.append(req); return httpx.Response(200,json={'rates':[{'mid':4.2,'effectiveDate':'2026-10-09'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await exchange_rate(client)==4.2; assert await exchange_rate(client)==4.2
        assert len(calls)==1
    asyncio.run(go())

def test_api_and_worker_pipeline(monkeypatch):
    store.save({'allegro_access_token':'mock-token','keywords':['night vision']})
    from app.browser_sources import BrowserResult
    async def mock_browser(source,s,rate):return BrowserResult(items=[],searches=1,partial=True,note='Mock browser search')
    monkeypatch.setattr('app.worker.browser_scan',mock_browser)
    calls=[]
    def handler(req):
        calls.append(str(req.url))
        if 'api.nbp.pl' in str(req.url): return httpx.Response(200,json={'rates':[{'mid':4.2,'effectiveDate':'2026-10-09'}]})
        if 'api.allegro.pl' in str(req.url):
            assert req.headers['authorization']=='Bearer mock-token'
            return httpx.Response(200,json={'items':{'regular':[{'id':'123','name':'Used night vision Gen 2','sellingMode':{'price':{'amount':'1000','currency':'PLN'}},'images':[],'parameters':[{'id':'11323','values':['Używany']}],'delivery':{'lowestPrice':{'amount':'30','currency':'PLN'}}}],'promoted':[]}})
        if str(req.url)=='https://ntfy.sh':
            payload=json.loads(req.content); assert '€245.24' in payload['message']; return httpx.Response(200,json={'id':'mock-notification'})
        raise AssertionError('Unexpected network request')
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client: await scan(client); await scan(client)
    asyncio.run(go())
    with store.db() as c:
        assert c.execute("SELECT status FROM sources WHERE name='allegro'").fetchone()[0]=='working'
        assert c.execute("SELECT status FROM sources WHERE name='vinted'").fetchone()[0]=='limited'
        assert c.execute('SELECT COUNT(*) FROM listings').fetchone()[0]==1
        assert c.execute('SELECT state FROM notifications').fetchone()[0]=='sent'
    assert len([c for c in calls if c=='https://ntfy.sh'])==1

def test_api_permission_denied():
    async def go():
        s=store.settings();s['allegro_access_token']='mock'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(403))) as client:
            with pytest.raises(SourceError,match='access denied'): await allegro(client,s,4.2)
    asyncio.run(go())

def test_notification_retry():
    ingest(sample(),4.2,store.settings())
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(503))) as client: await deliver(client)
    asyncio.run(go())
    with store.db() as c:
        row=c.execute('SELECT * FROM notifications').fetchone(); assert row['state']=='pending'; assert row['attempts']==1; assert row['next_try']>time.time()

def test_web_auth_lan_csrf_and_templates():
    with TestClient(app,client=('192.168.0.10',123)) as client:
        assert client.get('/').status_code==401
        client.auth=('admin','test-password')
        for path in ('/','/settings','/listings','/import','/status'): assert client.get(path).status_code==200
        assert client.post('/scan').status_code==403
        assert client.post('/scan',headers={'X-CSRF-Token':'test-csrf'},follow_redirects=False).status_code==303
        assert store.runtime('scan_requested')=='1'
        assert client.post('/pause',data={'csrf':'test-csrf'},follow_redirects=False).status_code==303
        assert store.settings()['paused'] is True
        assert client.post('/scan',headers={'X-CSRF-Token':'test-csrf','Sec-Fetch-Site':'cross-site'}).status_code==403
        assert client.post('/api/import',json=[sample().model_dump()]).status_code==401
        client.auth=None
        assert client.post('/api/import',headers={'Authorization':'Bearer test-import'},json=[sample().model_dump()]).status_code==200
    with TestClient(app,client=('8.8.8.8',123)) as client:
        assert client.get('/health').status_code==403
        assert client.get('/health',headers={'X-Forwarded-For':'192.168.0.1'}).status_code==403

def test_settings_and_form_import():
    with TestClient(app,client=('127.0.0.1',123)) as client:
        client.auth=('admin','test-password')
        result=client.post('/import',headers={'X-CSRF-Token':'test-csrf'},data={'source':'vinted','external_id':'form1','title':'Night vision Gen2','url':'https://www.vinted.lt/items/form1','price':'250','currency':'EUR','used':'yes'})
        assert result.status_code==200
        assert len(list((store.DATA/'inbox').glob('*.json')))==1
