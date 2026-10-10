import asyncio, json, time, signal
import httpx
from . import store
from .domain import Listing, classify, matches, total_eur
from .integrations import allegro, exchange_rate, notify, SourceError
from .browser_sources import browser_scan, BrowserBlocked, BrowserParseError

def ingest(item,rate,s,touch=True):
    category=classify(item); total=total_eur(item,rate); match=matches(item,category,total,s); now=time.time()
    with store.db() as c:
        old=c.execute('SELECT * FROM listings WHERE source=? AND external_id=?',(item.source,item.external_id)).fetchone()
        values=(item.title,item.description,item.url,item.image,item.currency,item.price,item.shipping,item.fees,total,category,item.used,now,int(match),rate)
        if old:
            if not touch: values=(*values[:11],old['last_seen'],*values[12:])
            ident=old['id']
            c.execute('UPDATE listings SET title=?,description=?,url=?,image=?,currency=?,price=?,shipping=?,fees=?,total_eur=?,category=?,used=?,last_seen=?,matched=?,fx_rate=? WHERE id=?',(*values,ident))
        else:
            ident=c.execute('INSERT INTO listings(title,description,url,image,currency,price,shipping,fees,total_eur,category,used,last_seen,matched,fx_rate,source,external_id,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(*values,item.source,item.external_id,now)).lastrowid
        changed=not old or any(old[k]!=getattr(item,k) for k in ('price','shipping','fees','currency')) or old['total_eur']!=total
        if changed: c.execute('INSERT INTO prices(listing_id,checked,price,total_eur,shipping,fees,fx_rate,currency) VALUES(?,?,?,?,?,?,?,?)',(ident,now,item.price,total,item.shipping,item.fees,rate,item.currency))
        event=None
        if match and not (old and old['ignored']):
            if not old or not old['matched']: event='new'
            elif old['total_eur']>0 and total<=old['total_eur']*(1-s['price_drop']/100): event='drop:'+str(total)
        if event and s['ntfy_enabled']:
            event_key=f'{ident}:{event}'
            title=('Price drop: ' if event.startswith('drop:') else 'New NVG deal: ')+item.title[:120]
            extra=' + unknown shipping/fees' if item.shipping is None or item.fees is None else ' incl. known shipping/fees'
            body=f'{item.title}\n€{total:.2f}{extra} · {item.source}\n{item.url}'
            c.execute('INSERT OR IGNORE INTO notifications(listing_id,event_key,title,body,url,created) VALUES(?,?,?,?,?,?)',(ident,event_key,title,body,item.url,now))
        if old and old['ignored'] or not match:
            c.execute("UPDATE notifications SET state='cancelled' WHERE listing_id=? AND state='pending'",(ident,))
    return ident

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
    count=0
    for item in items:
        if s[item.source+'_enabled']:
            ingest(item,rate,s); count+=1
    return count

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
            for item in items: ingest(item,rate or 1,s)
            set_source(source,'limited' if truncated else 'working',message,True)
            store.log(source,message)
        except BrowserBlocked as e:
            for item in e.items:ingest(item,rate or 1,s)
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
    # Re-evaluate saved records when filters change.
    with store.db() as c:
        rows=c.execute('SELECT * FROM listings').fetchall()
        for row in rows:
            item=Listing(**{k:row[k] for k in ('source','external_id','title','description','url','image','price','currency','shipping','fees','used')})
            if item.currency=='PLN' and rate is None: continue
            ingest(item,rate or 1,s,touch=False)
    store.runtime('last_scan',time.time()); store.runtime('scan_state','idle')
    await deliver(client)

async def run():
    store.init(); store.log('worker','Monitoring worker started')
    store.runtime('scan_state','idle'); last=0
    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
        while True:
            try:
                store.runtime('heartbeat',time.time()); s=store.settings()
                requested=store.runtime('scan_requested')
                if requested=='1' or (not s['paused'] and time.time()-last>=s['interval']*60):
                    store.runtime('scan_requested','0'); await scan(client); last=time.time()
                await deliver(client)
            except Exception as e:
                store.runtime('scan_state','error'); store.log('worker','Worker cycle failed: '+type(e).__name__,'error'); last=time.time()
            await asyncio.sleep(5)
if __name__=='__main__': asyncio.run(run())
