import asyncio, json, time, hashlib
from contextlib import suppress
import httpx
from . import store, feedback
from .domain import Listing, classify, matches, total_eur
from .integrations import allegro, exchange_rate, notify, SourceError
from .browser_sources import browser_scan, BrowserBlocked, BrowserParseError

def _ingest(c,item,rate,s,learned,touch=True,allow_events=True):
    category=classify(item); total=total_eur(item,rate); match=matches(item,category,total,s); now=time.time()
    old=c.execute('SELECT * FROM listings WHERE source=? AND external_id=?',(item.source,item.external_id)).fetchone()
    why=feedback.reason(item,learned)
    match=match and not why
    values=(item.title,item.description,item.url,item.image,item.currency,item.price,item.shipping,item.fees,total,category,item.used,now,int(match),rate,why)
    if old:
        if not touch: values=(*values[:11],old['last_seen'],*values[12:])
        ident=old['id']
        if touch or any(old[key]!=value for key,value in zip(('title','description','url','image','currency','price','shipping','fees','total_eur','category','used','last_seen','matched','fx_rate','learned_reason'),values)):
            c.execute('UPDATE listings SET title=?,description=?,url=?,image=?,currency=?,price=?,shipping=?,fees=?,total_eur=?,category=?,used=?,last_seen=?,matched=?,fx_rate=?,learned_reason=? WHERE id=?',(*values,ident))
    else:
        ident=c.execute('INSERT INTO listings(title,description,url,image,currency,price,shipping,fees,total_eur,category,used,last_seen,matched,fx_rate,learned_reason,source,external_id,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(*values,item.source,item.external_id,now)).lastrowid
    changed=not old or any(old[k]!=getattr(item,k) for k in ('price','shipping','fees','currency')) or old['total_eur']!=total
    if changed: c.execute('INSERT INTO prices(listing_id,checked,price,total_eur,shipping,fees,fx_rate,currency) VALUES(?,?,?,?,?,?,?,?)',(ident,now,item.price,total,item.shipping,item.fees,rate,item.currency))
    event=None
    if match and not (old and old['ignored']):
        if not old or not old['matched']: event='new'
        elif old['total_eur']>0 and total<=old['total_eur']*(1-s['price_drop']/100): event='drop:'+str(total)
    if event and allow_events and s['ntfy_enabled']:
        event_key=f'{ident}:{event}'
        title=('Price drop: ' if event.startswith('drop:') else 'New product match: ')+item.title[:120]
        extra=' + unknown shipping/fees' if item.shipping is None or item.fees is None else ' incl. known shipping/fees'
        body=f'{item.title}\n€{total:.2f}{extra} · {item.source}\n{item.url}'
        c.execute('INSERT OR IGNORE INTO notifications(listing_id,event_key,title,body,url,created) VALUES(?,?,?,?,?,?)',(ident,event_key,title,body,item.url,now))
    if old and old['ignored'] or not match:
        c.execute("UPDATE notifications SET state='cancelled' WHERE listing_id=? AND state='pending'",(ident,))
    return ident

def ingest(item,rate,s,touch=True):
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        return _ingest(c,item,rate,s,feedback.prepare(feedback.examples(c)),touch)

def ingest_many(items,rate,s,touch=True,allow_events=True):
    """Short transactions keep history/alerts atomic while letting feedback writes through."""
    items=list(items); identifiers=[]
    for start in range(0,len(items),50):
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            learned=feedback.prepare(feedback.examples(c))
            for item in items[start:start+50]: identifiers.append(_ingest(c,item,rate,s,learned,touch,allow_events))
    return identifiers

def reevaluate_history(s,rate):
    filters={key:s[key] for key in ('max_price','keywords','categories','vinted_enabled','skelbiu_enabled','allegro_enabled')}
    signature=hashlib.sha256(json.dumps({'filters':filters,'rate':rate},sort_keys=True).encode()).hexdigest()
    if store.runtime('filter_signature')==signature: return 0
    with store.db() as c: rows=c.execute('SELECT * FROM listings').fetchall()
    groups={}
    for row in rows:
        item=Listing(**{k:row[k] for k in ('source','external_id','title','description','url','image','price','currency','shipping','fees','used')})
        effective_rate=rate if rate is not None else (row['fx_rate'] if item.currency=='PLN' else 1)
        if not effective_rate: continue
        # Refilter saved PLN totals during an outage, but defer new alerts until a recent rate exists.
        alerts=rate is not None or item.currency=='EUR'
        groups.setdefault((effective_rate,alerts),[]).append(item)
    for (effective_rate,alerts),items in groups.items():
        ingest_many(items,effective_rate,s,touch=False,allow_events=alerts)
    store.runtime('filter_signature',signature)
    return sum(len(items) for items in groups.values())

async def deliver(client):
    s=store.settings()
    if not s['ntfy_enabled'] or not s['ntfy_topic']: return
    with store.db() as c:
        pending=c.execute("SELECT n.* FROM notifications n LEFT JOIN listings l ON l.id=n.listing_id WHERE n.state='pending' AND n.next_try<=? AND (l.id IS NULL OR (l.ignored=0 AND l.matched=1)) ORDER BY n.id LIMIT 20",(time.time(),)).fetchall()
    for n in pending:
        try:
            await notify(client,n,s)
            with store.db() as c: c.execute("UPDATE notifications SET state='sent',sent=?,error=NULL WHERE id=?",(time.time(),n['id']))
        except Exception as e:
            # Network timeout after acceptance may produce a retry; ntfy has no idempotency contract.
            message='Notification delivery failed: '+type(e).__name__
            with store.db() as c: c.execute('UPDATE notifications SET attempts=attempts+1,next_try=?,error=? WHERE id=?',(time.time()+min(3600,30*2**min(n['attempts'],7)),message,n['id']))
            store.log('ntfy',message,'error')

def set_source(name,status,error='',success=False):
    with store.db() as c:
        c.execute('UPDATE sources SET status=?,last_check=?,error=?,last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE name=?',(status,time.time(),error,success,time.time(),name))

def import_file(path,s,rate):
    raw=json.loads(path.read_text())
    if not isinstance(raw,list) or len(raw)>1000: raise ValueError('Expected JSON array with at most 1000 listings')
    items=[Listing.model_validate(x) for x in raw]
    enabled=[item for item in items if s[item.source+'_enabled']]
    ingest_many(enabled,rate,s)
    return len(enabled)

async def scan(client):
    s=store.settings(); store.runtime('scan_started',time.time()); store.runtime('scan_state','running')
    try:
        rate=await exchange_rate(client)
    except SourceError as e:
        rate=None; store.log('currency',str(e),'error')
    for source in ('vinted','skelbiu','allegro'):
        if not s[source+'_enabled']:
            set_source(source,'disabled'); continue
        if source=='allegro' and not rate:
            set_source(source,'error','PLN conversion unavailable; scan deferred'); continue
        try:
            set_source(source,'checking','Starting source check')
            use_api=source=='allegro' and (s['allegro_access_token'] or (s['allegro_client_id'] and s['allegro_client_secret']))
            if use_api:
                items,truncated=await allegro(client,s,rate)
                message='Official API scan: '+str(len(items))+' offers'+('; API pagination limit reached' if truncated else '')
            else:
                result=await browser_scan(source,s,rate or 1)
                items=result.items;truncated=result.partial;message=result.note
            ingest_many(items,rate or 1,store.settings())
            set_source(source,'limited' if truncated else 'working',message,True)
            store.log(source,message)
        except BrowserBlocked as e:
            ingest_many(e.items,rate or 1,store.settings())
            set_source(source,'blocked',str(e)+(' · Saved '+str(len(e.items))+' listings read before the block' if e.items else ''),bool(e.items));store.log(source,str(e),'warning')
        except BrowserParseError as e:
            set_source(source,'parser_error',str(e));store.log(source,str(e),'error')
        except SourceError as e:
            set_source(source,'needs_access' if use_api else 'error',str(e));store.log(source,str(e),'warning')
        except Exception as e:
            message='Scan failed: '+type(e).__name__+': '+str(e)[:250];set_source(source,'error',message);store.log(source,message,'error')
    for path in sorted((store.DATA/'inbox').glob('*.json')):
        try:
            raw=json.loads(path.read_text())
            if rate is None and any(x.get('currency','EUR').upper()=='PLN' for x in raw):
                store.log('import',path.name+': waiting for PLN rate','warning'); continue
            count=import_file(path,s,rate or 1)
            path.rename(path.with_suffix('.processed'))
            store.log('import',f'Imported {count} records from {path.name}')
        except Exception as e:
            path.rename(path.with_suffix('.failed')); store.log('import',path.name+': invalid import ('+type(e).__name__+')','error')
    # Only changed filters/rates require a full-history pass.
    reevaluated=reevaluate_history(store.settings(),rate)
    store.runtime('last_reevaluated',reevaluated)
    store.runtime('last_scan_seconds',round(time.time()-float(store.runtime('scan_started')),2))
    store.runtime('last_scan',time.time()); store.runtime('scan_state','idle')
    await deliver(client)

async def heartbeat(interval=15):
    while True:
        store.runtime('heartbeat',time.time())
        await asyncio.sleep(interval)

async def run():
    store.init(); store.log('worker','Monitoring worker started')
    store.runtime('scan_state','idle')
    # Recreating a worker does not trigger an unnecessary marketplace scan.
    last=float(store.runtime('last_scan') or 0)
    pulse=asyncio.create_task(heartbeat())
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            while True:
                try:
                    s=store.settings(); requested=store.runtime('scan_requested')
                    if requested=='1' or (not s['paused'] and time.time()-last>=s['interval']*60):
                        store.runtime('scan_requested','0'); await scan(client); last=time.time()
                    await deliver(client)
                except Exception as e:
                    store.runtime('scan_state','error'); store.log('worker','Worker cycle failed: '+type(e).__name__,'error'); last=time.time()
                await asyncio.sleep(5)
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError): await pulse
if __name__=='__main__': asyncio.run(run())
