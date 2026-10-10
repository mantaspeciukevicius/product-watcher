"""Read public, rendered marketplace pages with ordinary Playwright Chromium.
No private API calls, fingerprint modifications, CAPTCHA solving, proxies or access-block retries.
"""
import asyncio,hashlib,json,math,re,time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode,urlparse,urljoin,quote
from playwright.async_api import async_playwright,TimeoutError as BrowserTimeout
from . import store, feedback
from .domain import Listing,normalized,classify
from .integrations import SourceError

class BrowserBlocked(SourceError):
    def __init__(self,message,items=None,attempts=0,next_retry=None):
        super().__init__(message);self.items=items or [];self.attempts=attempts;self.next_retry=next_retry
class BrowserParseError(SourceError): pass
@dataclass
class BrowserResult:
    items:list
    searches:int=0
    details:int=0
    partial:bool=False
    note:str=''
    attempts:int=0

def _finite(value,default=0):
    try:
        number=float(value)
        return number if math.isfinite(number) else default
    except (TypeError,ValueError):return default

def retry_after_seconds(value,now=None):
    """Respect both HTTP Retry-After formats without guessing at invalid headers."""
    if not value:return 0
    now=time.time() if now is None else now
    numeric=_finite(value,-1)
    if numeric>=0:return numeric
    try:return max(0,parsedate_to_datetime(str(value)).timestamp()-now)
    except (ValueError,TypeError,OverflowError):return 0

def block_source(source,reason,retry_after=None,now=None):
    now=time.time() if now is None else now
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        def read(key):
            row=c.execute('SELECT value FROM runtime WHERE key=?',(key,)).fetchone()
            return row['value'] if row else None
        count=max(0,int(_finite(read(source+'_browser_block_count'))))+1
        delay=min(24*3600,3600*2**min(count-1,5))
        until=max(_finite(read(source+'_browser_blocked_until')),now+delay,now+retry_after_seconds(retry_after,now))
        for key,value in ((source+'_browser_block_count',count),(source+'_browser_blocked_until',until)):
            c.execute('INSERT OR REPLACE INTO runtime(key,value) VALUES(?,?)',(key,str(value)))
    return BrowserBlocked(reason+'; stopped without attempting bypass. Next attempt '+time.strftime('%Y-%m-%d %H:%M',time.localtime(until)),next_retry=until)

async def wait_request_slot(source,delay=None):
    """Persist the actual browser request gap across scans and worker restarts."""
    if delay is None:delay=store.settings().get('browser_delay',10)
    delay=max(3,min(60,_finite(delay,10)))
    while True:
        now=time.time()
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT value FROM runtime WHERE key=?',(source+'_browser_blocked_until',)).fetchone()
            until=_finite(row['value'] if row else None)
            if until>now:
                raise BrowserBlocked('Access challenge cooldown; next attempt '+time.strftime('%Y-%m-%d %H:%M',time.localtime(until)),next_retry=until)
            row=c.execute('SELECT value FROM runtime WHERE key=?',(source+'_last_browser_request',)).fetchone()
            last=_finite(row['value'] if row else None)
            wait=max(0,last+delay-now)
            if wait<=0:
                c.execute('INSERT OR REPLACE INTO runtime(key,value) VALUES(?,?)',(source+'_last_browser_request',str(now)))
                return
        store.runtime('heartbeat',now)
        await asyncio.sleep(min(wait,60))

def search_url(source,keyword,maximum,rate=1):
    if source=='vinted':return 'https://www.vinted.lt/catalog?'+urlencode({'search_text':keyword,'price_to':maximum,'order':'newest_first'})
    if source=='allegro':return 'https://allegro.pl/listing?'+urlencode({'string':keyword,'price_to':round(maximum*rate,2),'order':'d'})
    slug=re.sub(r'[^\w]+','-',normalized(keyword)).strip('-')
    return 'https://www.skelbiu.lt/paieska/'+quote(slug)+'/?'+urlencode({'cost_max':maximum,'orderBy':1})

def external_id(source,url):
    pattern={'vinted':r'/items/(\d+)','skelbiu':r'-(\d+)\.html','allegro':r'/oferta/(?:[^/?]*-)?(\d+)(?:[/?]|$)'}[source]
    m=re.search(pattern,url)
    return m.group(1) if m else None

def money(text,currency):
    symbol=r'(?:€|EUR)' if currency=='EUR' else r'(?:zł|zl|PLN)'
    # The amount adjacent to a currency symbol is the quoted price, never a number from the title.
    m=re.search(r'(?<![\w.,])((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?)[ \t\u00a0\u202f]*'+symbol,text,re.I)
    if not m:return None
    value=re.sub(r'[\s\u00a0\u202f]','',m.group(1)).replace(',','.')
    try:return float(value)
    except ValueError:return None

def detect_block(status,title,text):
    t=normalized(title+'\n'+text[:12000])
    if status in (401,403,429):return 'HTTP '+str(status)+' access block or rate limit'
    markers=['verify you are human','checking your browser','just a moment','access denied','attention required','unusual traffic','potwierdz, ze nie jestes robotem','potwierdz ze nie jestes robotem','patvirtinkite, kad esate zmogus','enable javascript and cookies to continue','captcha verification','verify that you are not a robot','please enable js and disable any ad blocker','please enable javascript and disable any ad blocker']
    for marker in markers:
        if marker in t:return 'Browser access challenge: '+marker
    return None

EXTRACT_JS=r'''(source) => {
 const patterns={vinted:/\/items\/\d+/,skelbiu:/\/skelbimai\/.*-\d+\.html/,allegro:/\/oferta\/(?:[^/?]*-)?\d+(?:[/?]|$)/};
 const seen=new Set(),out=[];
 const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
 for(const anchor of document.querySelectorAll('a[href]')) {
  if(!patterns[source].test(anchor.href)||!visible(anchor))continue;
  const href=anchor.href.split('?')[0];if(seen.has(href))continue;
  let node=anchor,card=null;
  for(let depth=0;node&&depth<9;depth++,node=node.parentElement){
   const text=node.innerText||'';
   const urls=new Set([...node.querySelectorAll('a[href]')].filter(a=>patterns[source].test(a.href)).map(a=>a.href.split('?')[0]));
   if(urls.size>1)break;
   if(/(?:€|EUR|zł|PLN)/i.test(text)&&text.length<3000){card=node;break;}
  }
  if(!card)continue;
  seen.add(href);
  const img=card.querySelector('img');
  const heading=card.querySelector('h2,h3,[data-testid*="title"],.title,.item-title');
  const title=((source==='vinted'?anchor.getAttribute('title'):heading?.innerText)||anchor.getAttribute('title')||anchor.innerText||img?.getAttribute('alt')||'').trim();
  out.push({url:href,title,text:card.innerText,image:img?.currentSrc||img?.src||'',description:card.querySelector('.description,.item-description,.item-desc,.first-dataline')?.innerText||'',combined:card.querySelector('[data-testid="total-combined-price"]')?.innerText||''});
 }
 return out;
}'''

async def visit(page,url,source):
    await wait_request_slot(source)
    store.runtime('heartbeat',time.time())
    try:
        response=await page.goto(url,wait_until='domcontentloaded',timeout=35000)
    except BrowserTimeout:
        response=None
        if page.url=='about:blank':raise SourceError('Browser navigation timed out before the page loaded')
    # Status blocks are conclusive even when the challenge body never renders.
    if response and response.status in (401,403,429):
        raise block_source(source,'HTTP '+str(response.status)+' access block or rate limit',response.headers.get('retry-after'))
    # Let normal page rendering finish; never interact with an access challenge.
    await page.wait_for_timeout(1500)
    text=await page.locator('body').inner_text(timeout=10000)
    reason=detect_block(response.status if response else None,await page.title(),text)
    # Visible CAPTCHA frames are access challenges, not normal invisible widgets in the footer.
    frames=page.locator('iframe[src*="captcha"],iframe[src*="challenges.cloudflare.com"],iframe[src*="challenge-platform"]')
    if not reason:
        for i in range(await frames.count()):
            if await frames.nth(i).is_visible(): reason='Visible CAPTCHA/access challenge';break
    if reason:
        raise block_source(source,reason,response.headers.get('retry-after') if response else None)
    if response and response.status>=400:raise SourceError('Marketplace returned HTTP '+str(response.status))
    return text

async def extract_cards(page,source):
    raw=await page.evaluate(EXTRACT_JS,source)
    result={}
    for entry in raw:
        ident=external_id(source,entry['url'])
        if not ident:continue
        currency='PLN' if source=='allegro' else 'EUR'
        price=money(entry['text'],currency)
        if price is None:continue
        title=entry['title']
        # Vinted image/link accessibility labels contain the title and item price.
        if not title:title=entry['text'].split('\n')[0]
        if source=='vinted':title=re.split(r',\s*(?:Prekių ženklas|Būklė|Brand|Condition)\s*:',title,flags=re.I)[0]
        title=re.sub(r'\s+',' ',title).strip()
        if not title:continue
        image=entry.get('image','')
        if not image.startswith('https://'):image=''
        used=None
        condition=normalized(entry['text'])
        if re.search(r'\b(nauja|nowy|nowa|nowe|new with|new without)\b',condition):used=False
        elif re.search(r'\b(naudota|naudotas|uzywan\w*|labai gera|gera|very good|good)\b',condition):used=True
        combined=money(entry.get('combined',''),currency)
        fees=round(combined-price,2) if combined is not None and combined>=price else None
        try:
            item=Listing(source=source,external_id=ident,title=title[:500],description=entry.get('description',''),url=entry['url'],image=image,price=price,currency=currency,fees=fees,used=used)
            result[ident]=item
        except ValueError:continue
    return list(result.values()),len(raw)

async def enrich(page,item):
    text=await visit(page,item.url,item.source)
    # Schema.org metadata is part of the public HTML document, not a private API.
    data=await page.evaluate('''() => ({schemas:[...document.querySelectorAll('script[type="application/ld+json"]')].map(n=>n.textContent), title:document.querySelector('h1')?.innerText||'', description:(document.querySelector('[itemprop="description"], [data-testid="item-description"], [data-testid="description"], .description, #description, .item-description, [data-role="description"]')?.innerText||'')})''')
    products=[]
    def walk(value):
        if isinstance(value,list):
            for x in value:walk(x)
        elif isinstance(value,dict):
            kind=value.get('@type',[])
            if 'Product' in (kind if isinstance(kind,list) else [kind]):products.append(value)
            if '@graph' in value:walk(value['@graph'])
    for raw in data['schemas']:
        try:walk(json.loads(raw))
        except (ValueError,TypeError):pass
    changes={}
    for product in products:
        if product.get('name'):changes['title']=str(product['name'])[:500]
        if product.get('description'):changes['description']=re.sub('<[^>]+>',' ',str(product['description']))[:30000]
        offer=product.get('offers',{})
        if isinstance(offer,list):offer=offer[0] if offer else {}
        if isinstance(offer,dict):
            currency=offer.get('priceCurrency',item.currency)
            if currency==item.currency and offer.get('price') is not None:
                try:changes['price']=float(str(offer['price']).replace(',','.'))
                except ValueError:pass
            condition=str(offer.get('itemCondition',product.get('itemCondition','')))
            if 'UsedCondition' in condition:changes['used']=True
            elif 'NewCondition' in condition:changes['used']=False
        image=product.get('image')
        if isinstance(image,list):image=image[0] if image else ''
        if isinstance(image,dict):image=image.get('url','')
        if isinstance(image,str) and image.startswith('https://'):changes['image']=image
    if not changes.get('title') and data['title']:changes['title']=data['title'][:500]
    if not changes.get('description') and data['description']:changes['description']=data['description'][:30000]
    # Limit fallback to text after the description heading to avoid categorizing a whole recommendation feed.
    if not changes.get('description'):
        m=re.search(r'(?:Aprašymas|Opis przedmiotu|Description)\s*\n(.{1,12000})',text,re.S|re.I)
        if m:changes['description']=m.group(1).split('\nPanašūs')[0].split('\nPodobne')[0][:12000]
    condition=normalized(text)
    if re.search(r'(?:bukle|stan)\s*[:\n]\s*(?:naudot|uzywan)',condition):changes['used']=True
    elif re.search(r'(?:bukle|stan)\s*[:\n]\s*(?:nauj|nowy|nowa|nowe)',condition):changes['used']=False
    return Listing.model_validate({**item.model_dump(),**changes})

def device_candidate(item):
    text=normalized(item.title+' '+item.description)
    return item.used is not False and classify(item)!='excluded' and bool(re.search(r'naktin\w*|noktow\w*|night[ -]?vision|intensifier|\bpvs[ -]?\d|\bnvg[ -]?(?:30|50)\b',text))

DETAIL_CACHE_TTL = 24 * 60 * 60
DETAIL_BUDGET = 12

def card_fingerprint(item):
    """Fingerprint the search card, including its current price and condition."""
    payload=json.dumps(item.model_dump(),sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()

def cached_detail(item,cached):
    """Ignore malformed cache entries rather than interrupting monitoring."""
    if not cached:return None
    try:
        payload=json.loads(cached['data'])
        detail=Listing.model_validate(payload['detail'])
        if detail.source!=item.source or detail.external_id!=item.external_id:return None
        card=payload['card']
        if not isinstance(card,dict):return None
        return card,detail
    except (KeyError,TypeError,ValueError):return None

def merge_detail_metadata(item,cached,previous=None):
    """Reuse descriptions without replacing prices or conditions from this scan."""
    parsed=cached_detail(item,cached)
    changes={}
    if parsed:
        card,detail=parsed
        # A changed title can represent an edited/replaced item; stale descriptions
        # must not exclude its new device before that item gets a detail request.
        if card.get('title')==item.title:
            changes['title']=detail.title
            if not item.description or item.description==card.get('description',''):
                changes['description']=detail.description
            if not item.image:changes['image']=detail.image
            # Search cards often omit condition. Preserve authoritative detail
            # condition when that omission is unchanged, including new devices.
            if item.used is None and card.get('used') is None:changes['used']=detail.used
    elif previous and previous['title']==item.title:
        if not item.description:changes['description']=previous['description'] or ''
        if not item.image:changes['image']=previous['image'] or ''
        if item.used is None and previous.get('used') is not None:changes['used']=bool(previous['used'])
    return Listing.model_validate({**item.model_dump(),**changes})

def detail_priority(item,cached,now=None):
    """New or changed cards first, then expired unchanged cards; None is fresh."""
    if not cached or not cached_detail(item,cached):return 0
    if cached['fingerprint']!=card_fingerprint(item):return 0
    try:age=(time.time() if now is None else now)-float(cached['checked'])
    except (TypeError,ValueError):return 0
    return None if 0<=age<DETAIL_CACHE_TTL else 1

def save_detail(card,detail,checked=None):
    payload=json.dumps({'card':card.model_dump(),'detail':detail.model_dump()},separators=(',',':'),ensure_ascii=False)
    with store.db() as c:
        c.execute('INSERT INTO detail_cache(source,external_id,fingerprint,checked,data) VALUES(?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET fingerprint=excluded.fingerprint,checked=excluded.checked,data=excluded.data',
                  (card.source,card.external_id,card_fingerprint(card),time.time() if checked is None else checked,payload))

async def browser_scan(source,s,rate=1):
    cooldown=_finite(store.runtime(source+'_browser_blocked_until'))
    if cooldown>time.time():raise BrowserBlocked('Access challenge cooldown; next attempt '+time.strftime('%Y-%m-%d %H:%M',time.localtime(cooldown)),next_retry=cooldown)
    detail_budget=max(0,min(DETAIL_BUDGET,int(_finite(s.get('_detail_budget'),DETAIL_BUDGET))))
    result=BrowserResult(items=[]); found={};cards={};parse_errors=[]
    # Read saved metadata and feedback once rather than opening SQLite per card.
    with store.db() as c:
        previous={r['external_id']:dict(r) for r in c.execute('SELECT external_id,title,description,image,used,ignored,irrelevant FROM listings WHERE source=?',(source,))}
        cached={r['external_id']:dict(r) for r in c.execute('SELECT * FROM detail_cache WHERE source=?',(source,))}
        learned=feedback.prepare(feedback.examples(c))
    session_dir=store.DATA/'browser';session_dir.mkdir(exist_ok=True)
    state=session_dir/(source+'-cookies.json')
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(channel="chromium")
        context=await browser.new_context(locale='en-GB',storage_state=str(state) if state.exists() else None)
        page=await context.new_page()
        try:
            for search_index,keyword in enumerate(s['keywords'],1):
                with store.db() as c:c.execute("UPDATE sources SET status='checking',last_check=?,error=? WHERE name=?",(time.time(),f'Browser search {search_index}/{len(s["keywords"])}: {keyword}',source))
                try:text=await visit(page,search_url(source,keyword,s['max_price'],rate),source)
                except BrowserBlocked as e:
                    e.items=list(found.values());raise
                result.searches+=1
                items,raw_count=await extract_cards(page,source)
                if not items:
                    empty_markers=('no items found','no results','nothing found','nie znalezlismy','brak ofert','nerasta','skelbimu nerasta','0 skelbimu','0 results','0 items','neradome','skelbimu nera','prekiu nera','kol kas cia nieko nera','nieko nerasta','no matching','no listings','could not find','couldn’t find')
                    if not any(x in normalized(text) for x in empty_markers):
                        parse_errors.append(keyword)
                        store.log(source,'Search '+keyword+': no recognized cards or empty-state','warning')
                for card in items:
                    item=merge_detail_metadata(card,cached.get(card.external_id),previous.get(card.external_id))
                    raw_allowed=card.used is not False and classify(card)!='excluded'
                    due=detail_priority(card,cached.get(card.external_id)) is not None
                    # Description/condition-only edits are invisible on cards:
                    # once details expire, an eligible raw card must get a recheck
                    # even if its previous description excluded it.
                    candidate=device_candidate(item) or (due and device_candidate(card))
                    if raw_allowed and candidate and card.price<(s['max_price']*rate if card.currency=='PLN' else s['max_price']):
                        found[item.external_id]=item;cards[item.external_id]=card
                # First results page only: explicitly partial coverage, not an exhaustive scan.
            now=time.time();plan=[];reused=0;skipped=0;attempts=0
            keyword_text=[normalized(k) for k in s['keywords']]
            for ident,item in found.items():
                known=previous.get(ident)
                if (known and (known['ignored'] or known['irrelevant'])) or feedback.reason(item,learned):
                    skipped+=1;continue
                priority=detail_priority(cards[ident],cached.get(ident),now)
                recheck=priority is not None and device_candidate(cards[ident])
                if (classify(item)=='excluded' or item.used is False) and not recheck:continue
                title_text=normalized(item.title+' '+cards[ident].title)
                if not any(k in title_text for k in keyword_text):continue
                if priority is None:reused+=1;continue
                plan.append((priority,0 if classify(item) in s['categories'] else 1,ident))
            for _,_,ident in sorted(plan,key=lambda entry:entry[:2]):
                item=found[ident]
                if attempts>=detail_budget:result.partial=True;break
                attempts+=1;result.attempts=attempts
                try:
                    with store.db() as c:c.execute("UPDATE sources SET error=? WHERE name=?",(f'Reading device descriptions: {attempts}/{detail_budget}',source))
                    detailed=await enrich(page,cards[ident])
                    found[ident]=detailed;result.details+=1
                    save_detail(cards[ident],detailed)
                except BrowserBlocked as e:
                    e.items=list(found.values());e.attempts=attempts;raise
                except (BrowserTimeout,SourceError,ValueError):result.partial=True
            if parse_errors and not found:raise BrowserParseError('No recognized listings/empty states for searches: '+', '.join(parse_errors))
            result.items=list(found.values())
            result.note=f'Browser checked {result.searches} searches; read {len(found)} unique listings and {result.details} detail pages ({attempts} attempted, {reused} cached, {skipped} skipped by feedback/ignore). First results page per keyword; shipping/fees only if provided. Coverage is partial.'
            if parse_errors:result.note+=' Unverified searches: '+', '.join(parse_errors)+'.'
            result.partial=True
            await context.storage_state(path=str(state))
            # A parsed search that completed without an access block breaks
            # the streak; cooldown skips and parser failures do not.
            store.runtime(source+'_browser_block_count',0)
            return result
        finally:
            await context.close();await browser.close()
