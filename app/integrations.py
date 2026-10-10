import time, json, httpx
from . import store
from .domain import Listing
class SourceError(Exception): pass
async def exchange_rate(client):
    cached=store.runtime('fx_rate'); checked=float(store.runtime('fx_checked') or 0)
    if cached and time.time()-checked<86400: return float(cached)
    try:
        r=await client.get('https://api.nbp.pl/api/exchangerates/rates/a/eur/',params={'format':'json'})
        r.raise_for_status(); data=r.json()['rates'][-1]
        rate=float(data['mid'])
        if not 1<rate<20: raise ValueError('Invalid exchange rate')
        store.runtime('fx_rate',rate); store.runtime('fx_checked',time.time()); store.runtime('fx_date',data['effectiveDate'])
        return rate
    except Exception:
        if cached and time.time()-checked<7*86400:
            store.log('currency','NBP unavailable; using cached rate (under 7 days old)','warning'); return float(cached)
        raise SourceError('No recent NBP PLN/EUR rate; PLN imports and scans are deferred')
async def allegro(client,s,rate):
    token=s['allegro_access_token']
    if not token and s['allegro_client_id'] and s['allegro_client_secret']:
        r=await client.post('https://allegro.pl/auth/oauth/token',auth=(s['allegro_client_id'],s['allegro_client_secret']),data={'grant_type':'client_credentials'})
        if r.status_code!=200: raise SourceError('Allegro OAuth rejected credentials (HTTP '+str(r.status_code)+')')
        token=r.json()['access_token']
    if not token: raise SourceError('Credentials required; offers/listing also needs an approved Allegro application')
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.allegro.public.v1+json'}
    result={}; truncated=False
    for keyword in s['keywords']:
        # Fetch all pages within the endpoint limit; do not silently claim exhaustive coverage.
        for offset in range(0,600,100):
            r=await client.get('https://api.allegro.pl/offers/listing',headers=headers,params={'phrase':keyword,'price.to':round(s['max_price']*rate,2),'sellingMode.format':'BUY_NOW','sort':'-startTime','limit':100,'offset':offset})
            if r.status_code in (401,403): raise SourceError('Allegro access denied (HTTP '+str(r.status_code)+'); verified offers/listing permission is required')
            if r.status_code==429: raise SourceError('Allegro rate limit; waiting until the next interval')
            r.raise_for_status(); data=r.json(); entries=data.get('items',{}).get('promoted',[])+data.get('items',{}).get('regular',[])
            for x in entries:
                amount=x.get('sellingMode',{}).get('price')
                if not amount: continue
                params=x.get('parameters',[]); condition=next((p for p in params if str(p.get('id'))=='11323'),None)
                used=None
                if condition:
                    values=' '.join(condition.get('values',[])).lower()
                    used=not ('now' in values or 'new' in values)
                shipping=x.get('delivery',{}).get('lowestPrice')
                # lowestPrice is a lower bound, not necessarily delivery to Lithuania.
                item=Listing(source='allegro',external_id=str(x['id']),title=x['name'],url='https://allegro.pl/oferta/'+str(x['id']),image=(x.get('images') or [{}])[0].get('url',''),price=float(amount['amount']),currency=amount['currency'],shipping=float(shipping['amount']) if shipping and shipping.get('currency')==amount['currency'] else None,used=used)
                result[item.external_id]=item
            if len(data.get('items',{}).get('regular',[]))<100: break
            if offset==500: truncated=True
    return list(result.values()),truncated
async def notify(client,n,s):
    headers={}
    if s['ntfy_token']: headers['Authorization']='Bearer '+s['ntfy_token']
    body={'topic':s['ntfy_topic'],'title':n['title'],'message':n['body'],'tags':['night_with_stars'],'priority':3}
    if n['url']: body['click']=n['url']; body['actions']=[{'action':'view','label':'Open listing','url':n['url']}]
    r=await client.post(s['ntfy_url'],headers=headers,json=body)
    r.raise_for_status()
    return r.json()
