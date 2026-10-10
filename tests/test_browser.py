import asyncio,time
import pytest
from app import store
from app.browser_sources import money,external_id,search_url,detect_block,extract_cards,enrich,BrowserBlocked,visit
from playwright.async_api import async_playwright

@pytest.mark.parametrize('text,currency,expected',[('300 €','EUR',300),('1 250,50 €','EUR',1250.5),('Noktowizor NVG50 1 099,99 zł','PLN',1099.99),('NVG30','EUR',None),('€ unknown','EUR',None)])
def test_currency_from_rendered_text(text,currency,expected):assert money(text,currency)==expected
@pytest.mark.parametrize('source,url,ident',[('vinted','https://www.vinted.lt/items/123-title','123'),('allegro','https://allegro.pl/oferta/monokular-123456','123456'),('skelbiu','https://www.skelbiu.lt/skelbimai/nvg-123456.html','123456')])
def test_item_ids(source,url,ident):assert external_id(source,url)==ident

def test_search_and_block_detection():
 assert 'cost_max=400' in search_url('skelbiu','naktinio matymo',400)
 assert 'price_to=1680' in search_url('allegro','noktowizor',400,4.2)
 assert detect_block(403,'','')
 assert detect_block(200,'Just a moment...','')
 assert detect_block(200,'Night vision','This listing has a captcha-resistant camera') is None

def test_browser_cards_and_detail():
 async def go():
  async with async_playwright() as p:
   browser=await p.chromium.launch(channel='chromium')
   page=await browser.new_page()
   await page.set_content('''<html><body><div class="card"><a href="https://www.vinted.lt/items/123-used-gen2"><img src="https://images.example/nvg.jpg" alt="Used night vision Gen 2"></a><h3>Used night vision Gen 2</h3><p>300 €</p></div><div class="card"><a href="https://www.vinted.lt/items/456"><h3>NVG50 night vision</h3></a><span>200,50 €</span></div></body></html>''')
   items,raw=await extract_cards(page,'vinted')
   assert len(items)==2;assert items[0].price==300;assert items[1].price==200.5
   await page.route('https://www.vinted.lt/items/**',lambda route:route.fulfill(status=200,content_type='text/html',body='''<html><body><h1>Analog Gen 2 night vision</h1><div data-testid="item-description">Used analog image intensifier, generation 2.</div><script type="application/ld+json">{"@type":"Product","name":"Analog Gen 2 night vision","description":"Used analog generation 2","offers":{"price":"280","priceCurrency":"EUR","itemCondition":"https://schema.org/UsedCondition"}}</script></body></html>'''))
   detailed=await enrich(page,items[0]);assert detailed.price==280;assert detailed.used is True;assert 'generation 2' in detailed.description
   await page.route('https://www.skelbiu.lt/**',lambda route:route.fulfill(status=403,content_type='text/html',body='<h1>Access denied</h1>'))
   with pytest.raises(BrowserBlocked):await visit(page,'https://www.skelbiu.lt/paieska/test/','skelbiu')
   assert float(store.runtime('skelbiu_browser_blocked_until'))>time.time()
   await browser.close()
 asyncio.run(go())

def test_irrelevant_search_hits_are_not_collected():
 from app.browser_sources import device_candidate
 from app.domain import Listing
 base=dict(source='vinted',external_id='1',url='https://www.vinted.lt/items/1',price=200)
 assert not device_candidate(Listing(**base,title='Wireless headphones Gen 2'))
 assert not device_candidate(Listing(**base,title='New NVG30 night vision',used=False))
 assert not device_candidate(Listing(**base,title='Used night vision Gen 1',used=True))
 assert device_candidate(Listing(**base,title='Used night vision Gen 2+',used=True))
