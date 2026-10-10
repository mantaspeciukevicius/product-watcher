import os, secrets, ipaddress, time, json, datetime, re
from urllib.parse import quote
from pathlib import Path
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from . import store, feedback
from .domain import Listing
from .integrations import notify

@asynccontextmanager
async def lifespan(app):
    store.init()
    if not os.getenv('ADMIN_PASSWORD'): raise RuntimeError('ADMIN_PASSWORD must be set')
    yield
app=FastAPI(title='Product Watcher',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
security=HTTPBasic()
templates=Jinja2Templates(directory=str(Path(__file__).parent/'templates'))
app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='static')

def auth(credentials:HTTPBasicCredentials=Depends(security)):
    user=secrets.compare_digest(credentials.username.encode(),os.getenv('ADMIN_USER','admin').encode())
    password=secrets.compare_digest(credentials.password.encode(),os.getenv('ADMIN_PASSWORD','').encode())
    if not (user and password): raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Basic realm="Product Watcher"'})

@app.middleware('http')
async def local_only(request,call_next):
    try: ip=ipaddress.ip_address(request.client.host)
    except ValueError: return JSONResponse({'detail':'LAN only'},403)
    allowed=[ipaddress.ip_network(x.strip()) for x in os.getenv('ALLOWED_CIDRS','127.0.0.0/8,192.168.0.0/24').split(',')]
    if not any(ip in net for net in allowed): return JSONResponse({'detail':'LAN only'},403)
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('sec-fetch-site')=='cross-site': return JSONResponse({'detail':'Cross-site request rejected'},403)
        token=request.headers.get('x-csrf-token')
        if request.url.path!='/api/import':
            if not token:
                content=request.headers.get('content-type','')
                if 'application/x-www-form-urlencoded' in content or 'multipart/form-data' in content:
                    await request.body()
                    form=await request.form(); token=form.get('csrf')
            if not token or not secrets.compare_digest(str(token),os.getenv('CSRF_TOKEN','')): return JSONResponse({'detail':'Invalid CSRF token'},403)
        # API imports use their own bearer token, never cookie/browser authentication.
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'; response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
    response.headers['Cache-Control']='no-store'
    return response

def date(value): return datetime.datetime.fromtimestamp(value).astimezone().strftime('%d %b %H:%M:%S') if value else 'Never'
templates.env.filters['date']=date

def context(request,**extra):
    s=store.settings()
    with store.db() as c:
        sources=[dict(r) for r in c.execute('SELECT * FROM sources')]
        counts=dict(c.execute('SELECT matched,COUNT(*) FROM listings GROUP BY matched').fetchall())
    heartbeat=float(store.runtime('heartbeat') or 0)
    return dict(request=request,s=s,sources=sources,csrf=os.getenv('CSRF_TOKEN'),worker_ok=time.time()-heartbeat<180,heartbeat=heartbeat,last_scan=float(store.runtime('last_scan') or 0),scan_seconds=store.runtime('last_scan_seconds'),reevaluated=store.runtime('last_reevaluated'),scan_state=store.runtime('scan_state') or 'idle',count=counts.get(1,0),fx_rate=store.runtime('fx_rate'),fx_date=store.runtime('fx_date'),**extra)

@app.get('/health')
def health():
    with store.db() as c: c.execute('SELECT 1').fetchone()
    return {'status':'ok','worker_alive':time.time()-float(store.runtime('heartbeat') or 0)<180}
@app.get('/',response_class=HTMLResponse,dependencies=[Depends(auth)])
def dashboard(request:Request):
    with store.db() as c:
        listings=c.execute('SELECT * FROM listings WHERE matched=1 AND ignored=0 ORDER BY first_seen DESC LIMIT 6').fetchall()
        logs=c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 15').fetchall()
        notifications=c.execute('SELECT * FROM notifications ORDER BY id DESC LIMIT 10').fetchall()
    return templates.TemplateResponse(request=request,name='dashboard.html',context=context(request,listings=listings,logs=logs,notifications=notifications,page='dashboard'))
@app.get('/status',response_class=HTMLResponse,dependencies=[Depends(auth)])
def status(request:Request): return templates.TemplateResponse(request=request,name='status.html',context=context(request))
@app.get('/listings',response_class=HTMLResponse,dependencies=[Depends(auth)])
def listing_page(request:Request,view:str='matching',q:str='',source:str='',sort:str='newest'):
    clauses=[]; args=[]
    if view=='irrelevant': clauses=["(irrelevant=1 OR learned_reason!='')"]
    elif view=='favorites': clauses=['favorite=1','ignored=0']
    elif view=='ignored': clauses=['ignored=1']
    elif view!='all': clauses=['matched=1','ignored=0']
    if q: clauses.append('(title LIKE ? OR description LIKE ?)'); args.extend(['%'+q[:200]+'%']*2)
    if source in ('vinted','skelbiu','allegro'): clauses.append('source=?'); args.append(source)
    orders={'newest':'first_seen DESC, id DESC','oldest':'first_seen ASC, id ASC','price_low':'total_eur ASC, first_seen DESC, id DESC','priority':"favorite DESC, CASE category WHEN 'gen2plus' THEN 0 WHEN 'gen2' THEN 1 WHEN 'analog_unknown' THEN 2 ELSE 3 END, first_seen DESC, id DESC"}
    if sort not in orders: sort='newest'
    with store.db() as c: listings=c.execute('SELECT * FROM listings'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY '+orders[sort]+' LIMIT 250',args).fetchall()
    return templates.TemplateResponse(request=request,name='listings.html',context=context(request,listings=listings,view=view,q=q,source=source,sort=sort,page='listings'))

@app.get('/listing/{ident}',response_class=HTMLResponse,dependencies=[Depends(auth)])
def detail(request:Request,ident:int):
    with store.db() as c:
        listing=c.execute('SELECT * FROM listings WHERE id=?',(ident,)).fetchone()
        prices=c.execute('SELECT * FROM prices WHERE listing_id=? ORDER BY id DESC LIMIT 200',(ident,)).fetchall()
    if not listing: raise HTTPException(404)
    return templates.TemplateResponse(request=request,name='detail.html',context=context(request,listing=listing,prices=prices,page='listings'))
@app.post('/listing/{ident}/{action}',dependencies=[Depends(auth)])
def action(ident:int,action:str,request:Request):
    if action not in ('favorite','ignored','irrelevant'): raise HTTPException(404)
    settings=store.settings()
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT id FROM listings WHERE id=?',(ident,)).fetchone(): raise HTTPException(404)
        c.execute(f'UPDATE listings SET {action}=1-{action} WHERE id=?',(ident,))
        if action=='irrelevant': feedback.refresh(c,settings)
        if action=='ignored': c.execute("UPDATE notifications SET state='cancelled' WHERE listing_id=? AND state='pending' AND EXISTS(SELECT 1 FROM listings WHERE id=? AND ignored=1)",(ident,ident))
        listing=c.execute('SELECT * FROM listings WHERE id=?',(ident,)).fetchone()
    if request.headers.get('hx-request') and action=='irrelevant': return HTMLResponse('',headers={'HX-Refresh':'true'})
    if request.headers.get('hx-request'): return templates.TemplateResponse(request=request,name='card.html',context=context(request,listing=listing))
    return RedirectResponse('/listing/'+str(ident),303)
@app.post('/scan',dependencies=[Depends(auth)])
def scan_now(request:Request):
    store.runtime('scan_requested','1')
    if request.headers.get('hx-request'): return HTMLResponse('<span class="success">Scan queued. Status updates automatically.</span>')
    return RedirectResponse('/',303)
@app.post('/pause',dependencies=[Depends(auth)])
def pause():
    s=store.settings(); store.save({'paused':not s['paused']}); return RedirectResponse('/',303)
@app.get('/settings',response_class=HTMLResponse,dependencies=[Depends(auth)])
def settings_page(request:Request,saved:bool=False):
    return templates.TemplateResponse(request=request,name='settings.html',context=context(request,page='settings',saved=saved))

@app.post('/settings',dependencies=[Depends(auth)])
async def save_settings(request:Request):
    f=await request.form(); old=store.settings()
    try:
        maximum=float(f.get('max_price',400)); interval=int(f.get('interval',15)); drop=float(f.get('price_drop',10))
        if not 1<=maximum<=100000 or not 15<=interval<=1440 or not 1<=drop<=90: raise ValueError('Price, interval or drop threshold out of range')
        keywords=[x.strip() for x in str(f.get('keywords','')).splitlines() if x.strip()]
        if not keywords or len(keywords)>30 or any(len(x)>100 for x in keywords): raise ValueError('Enter 1–30 keywords, up to 100 characters each')
        categories=[k for k in ('gen2','gen2plus','analog_unknown','nvg30','nvg50') if f.get(k)]
        url=str(f.get('ntfy_url','https://ntfy.sh')).rstrip('/')
        # Only outbound HTTPS ntfy destinations are allowed; do not allow loopback/private SSRF.
        import socket
        from urllib.parse import urlparse
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname: raise ValueError('ntfy requires an HTTPS server URL')
        addresses=socket.getaddrinfo(parsed.hostname,parsed.port or 443)
        if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses): raise ValueError('ntfy host must resolve to public HTTPS addresses')
        topic=str(f.get('ntfy_topic',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',topic): raise ValueError('Invalid ntfy topic')
        values=dict(max_price=maximum,interval=interval,price_drop=drop,keywords=keywords,categories=categories,ntfy_url=url,ntfy_topic=topic,ntfy_enabled=bool(f.get('ntfy_enabled')))
        for name in ('vinted','skelbiu','allegro'): values[name+'_enabled']=bool(f.get(name+'_enabled'))
        for secret in ('ntfy_token','allegro_client_secret','allegro_access_token'):
            if f.get('clear_'+secret): values[secret]=''
            elif f.get(secret): values[secret]=str(f[secret]).strip()
        values['allegro_client_id']=str(f.get('allegro_client_id','')).strip()
        store.save(values); store.runtime('scan_requested','1')
    except (ValueError,OSError) as e: raise HTTPException(400,str(e))
    return RedirectResponse('/settings?saved=true',303)
@app.post('/notification/test',dependencies=[Depends(auth)])
async def test_notification():
    s=store.settings()
    n={'title':'Product Watcher test','body':'Your Product Watcher on your self-hosted server is ready. Android delivery requires subscribing to this topic.','url':''}
    with store.db() as c:
        ident=c.execute("INSERT INTO notifications(event_key,title,body,url,created,state) VALUES(?,?,?,?,?,'sending')",('test:'+secrets.token_hex(12),n['title'],n['body'],'',time.time())).lastrowid
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client: await notify(client,n,s)
        with store.db() as c: c.execute("UPDATE notifications SET state='sent',sent=? WHERE id=?",(time.time(),ident))
        return HTMLResponse('<span class="success">Test accepted by ntfy. Check your subscribed Android phone.</span>')
    except Exception as e:
        with store.db() as c: c.execute("UPDATE notifications SET state='failed',error=? WHERE id=?",(type(e).__name__,ident))
        raise HTTPException(502,'ntfy delivery failed: '+type(e).__name__)
@app.get('/import',response_class=HTMLResponse,dependencies=[Depends(auth)])
def import_page(request:Request): return templates.TemplateResponse(request=request,name='import.html',context=context(request,page='import',import_token=os.getenv('IMPORT_TOKEN')))
async def queue_import(raw):
    if not isinstance(raw,list) or len(raw)>1000: raise HTTPException(400,'Expected an array of at most 1000 listings')
    try: items=[Listing.model_validate(x).model_dump() for x in raw]
    except Exception as e: raise HTTPException(400,'Invalid listing data: '+str(e)[:500])
    name=secrets.token_hex(12)
    temporary=store.DATA/'inbox'/(name+'.tmp'); temporary.write_text(json.dumps(items)); temporary.rename(temporary.with_suffix('.json'))
    store.runtime('scan_requested','1'); return {'queued':len(items)}
@app.post('/api/import')
async def api_import(request:Request):
    token=request.headers.get('authorization','').removeprefix('Bearer ')
    if not secrets.compare_digest(token,os.getenv('IMPORT_TOKEN','')) or not token: raise HTTPException(401,'Import bearer token required')
    body=await request.body()
    if len(body)>2_000_000: raise HTTPException(413)
    try: raw=json.loads(body)
    except ValueError: raise HTTPException(400,'Invalid JSON')
    return await queue_import(raw)
@app.post('/import',dependencies=[Depends(auth)])
async def manual_import(request:Request):
    f=await request.form()
    if f.get('json_data'):
        try: raw=json.loads(str(f['json_data']))
        except ValueError: raise HTTPException(400,'Invalid JSON')
    else:
        try:
            item=Listing(source=f['source'],external_id=f['external_id'],title=f['title'],description=f.get('description',''),url=f['url'],image=f.get('image',''),price=float(f['price']),currency=f['currency'],shipping=float(f['shipping']) if f.get('shipping') else None,fees=float(f['fees']) if f.get('fees') else None,used={'yes':True,'no':False}.get(f.get('used')))
            raw=[item.model_dump()]
        except Exception as e: raise HTTPException(400,'Invalid listing: '+str(e)[:500])
    result=await queue_import(raw)
    return HTMLResponse('<span class="success">Queued '+str(result['queued'])+' listings. The worker will process them shortly.</span>')
