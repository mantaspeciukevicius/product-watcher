"""Detail caching/selection checks with no marketplace network requests."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import browser_sources as browser, store
from app.domain import Listing
from app.integrations import SourceError
from app.worker import ingest


def card(ident='1', **changes):
    values=dict(source='vinted', external_id=ident, title='Used night vision Gen 2',
                url=f'https://www.vinted.lt/items/{ident}', price=300, currency='EUR',
                shipping=5, fees=2, used=True)
    values.update(changes)
    return Listing(**values)


def detail(item, **changes):
    values={**item.model_dump(), 'title':'Analog night vision Gen 2 monocular',
            'description':'Used analog image intensifier generation 2.',
            'image':'https://images.example/device.jpg'}
    values.update(changes)
    return Listing(**values)


def cached(ident='1'):
    with store.db() as c:
        row=c.execute('SELECT * FROM detail_cache WHERE source=? AND external_id=?',
                      ('vinted', ident)).fetchone()
    return dict(row) if row else None


def settings():
    return {**store.settings(), 'keywords':['night vision']}


@pytest.fixture
def fake_browser(monkeypatch):
    class Context:
        async def new_page(self):return object()
        async def storage_state(self, **kwargs):pass
        async def close(self):pass
    class Browser:
        async def new_context(self, **kwargs):return Context()
        async def close(self):pass
    class Chromium:
        async def launch(self, **kwargs):return Browser()
    class Manager:
        async def __aenter__(self):return SimpleNamespace(chromium=Chromium())
        async def __aexit__(self, *args):pass
    async def visit(*args):return 'Results'
    async def sleep(*args):pass
    monkeypatch.setattr(browser, 'async_playwright', Manager)
    monkeypatch.setattr(browser, 'visit', visit)
    monkeypatch.setattr(browser.asyncio, 'sleep', sleep)


def setup_scan(monkeypatch, cards, calls, fail=False):
    async def extract(*args):return list(cards), len(cards)
    async def enrich(_page, item):
        calls.append(item.external_id)
        if fail:raise SourceError('Mock unavailable detail')
        return detail(item)
    monkeypatch.setattr(browser, 'extract_cards', extract)
    monkeypatch.setattr(browser, 'enrich', enrich)


def test_cache_freshness_and_changed_card():
    item=card()
    browser.save_detail(item, detail(item), checked=1000)
    record=cached()
    assert browser.detail_priority(item, record, now=1001) is None
    assert browser.detail_priority(item, record, now=1000+browser.DETAIL_CACHE_TTL)==1
    assert browser.detail_priority(card(price=250), record, now=1001)==0
    assert browser.detail_priority(item, None, now=1001)==0
    assert browser.detail_priority(item, {**record, 'data':'invalid JSON'}, now=1001)==0


def test_cached_metadata_never_replaces_current_financial_or_condition_fields():
    old=card()
    browser.save_detail(old, detail(old, price=280, shipping=30, fees=7, used=True))
    current=card(price=250, shipping=None, fees=3, used=None, currency='PLN')
    merged=browser.merge_detail_metadata(current, cached())
    assert merged.price==250 and merged.currency=='PLN'
    assert merged.shipping is None and merged.fees==3 and merged.used is None
    assert merged.description=='Used analog image intensifier generation 2.'
    assert merged.title=='Analog night vision Gen 2 monocular'
    assert merged.image=='https://images.example/device.jpg'


def test_title_change_does_not_reuse_obsolete_description():
    old=card(title='Used night vision Gen 1')
    browser.save_detail(old, detail(old, title=old.title, description='Generation 1 device'))
    changed=card(title='Used night vision Gen 2+')
    merged=browser.merge_detail_metadata(changed, cached())
    assert merged.title==changed.title and merged.description==''
    assert browser.device_candidate(merged)


def test_saved_description_survives_before_first_cache_entry():
    item=card()
    merged=browser.merge_detail_metadata(item, None, {'title':item.title, 'description':'Existing analog description',
                                                    'image':'https://images.example/old.jpg'})
    assert merged.description=='Existing analog description'
    assert merged.image=='https://images.example/old.jpg'
    changed=browser.merge_detail_metadata(card(title='Different night vision Gen 2 device'), None,
                                         {'title':item.title, 'description':'Old generation 1 details', 'image':''})
    assert changed.description==''


def test_scan_reuses_cache_and_refreshes_changed_price(fake_browser, monkeypatch):
    cards=[card()];calls=[]
    setup_scan(monkeypatch, cards, calls)
    first=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1'] and first.details==1
    second=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1'] and second.details==0
    assert second.items[0].description==first.items[0].description
    assert second.items[0].price==300
    assert '1 cached' in second.note
    cards[0]=card(price=250)
    third=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1','1'] and third.items[0].price==250


@pytest.mark.parametrize('known_condition',[False,True])
def test_imported_condition_survives_cards_without_detail_budget(fake_browser,monkeypatch,known_condition):
    known=card(used=known_condition)
    ingest(known,1,store.settings())
    cards=[card(used=None)];calls=[]
    setup_scan(monkeypatch,cards,calls)
    s={**settings(),'_detail_budget':0}
    result=asyncio.run(browser.browser_scan('vinted',s))
    assert result.items[0].used is known_condition and not calls
    # A condition explicitly shown on the current card always wins.
    cards[0]=card(used=not known_condition)
    # Newly marked new-condition cards are excluded at collection, as intended.
    result=asyncio.run(browser.browser_scan('vinted',s))
    if cards[0].used:assert result.items[0].used is True
    else:assert not result.items


def test_scan_refreshes_expired_detail(fake_browser, monkeypatch):
    cards=[card()];calls=[]
    browser.save_detail(cards[0], detail(cards[0]), checked=time.time()-browser.DETAIL_CACHE_TTL-1)
    setup_scan(monkeypatch, cards, calls)
    result=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1'] and result.details==1
    assert browser.detail_priority(cards[0], cached()) is None


def test_feedback_and_ignore_skip_detail_requests_but_keep_cards(fake_browser, monkeypatch):
    cards=[card('1', title='Baby monitor night vision'),
           card('2', title='Used night vision Gen 2 ignored'),
           card('3', title='New baby monitor with night vision')]
    # Establish explicit feedback and ignore state in saved records.
    ident=ingest(cards[0], 4.2, settings())
    ignored=ingest(cards[1], 4.2, settings())
    with store.db() as c:
        c.execute('UPDATE listings SET irrelevant=1 WHERE id=?', (ident,))
        c.execute('UPDATE listings SET ignored=1 WHERE id=?', (ignored,))
    calls=[];setup_scan(monkeypatch, cards, calls)
    result=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==[]
    assert {item.external_id for item in result.items}=={'1','2','3'}
    assert '3 skipped by feedback/ignore' in result.note


def test_budget_prioritizes_unknown_and_changed_before_stale(fake_browser, monkeypatch):
    stale=card('1');fresh=card('2');unknown=card('3');changed=card('4', price=250)
    browser.save_detail(stale, detail(stale), checked=time.time()-browser.DETAIL_CACHE_TTL-1)
    browser.save_detail(fresh, detail(fresh))
    browser.save_detail(card('4'), detail(card('4')))
    calls=[];setup_scan(monkeypatch, [stale, fresh, unknown, changed], calls)
    monkeypatch.setattr(browser, 'DETAIL_BUDGET', 2)
    result=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['3','4'] and result.details==2
    assert len(result.items)==4


def test_failed_detail_requests_still_consume_budget(fake_browser, monkeypatch):
    calls=[];setup_scan(monkeypatch, [card(str(i)) for i in range(1,5)], calls, fail=True)
    monkeypatch.setattr(browser, 'DETAIL_BUDGET', 2)
    result=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1','2'] and result.details==0
    assert '2 attempted' in result.note and cached() is None


def test_expired_excluded_description_can_be_rechecked(fake_browser, monkeypatch):
    item=card(title='Used night vision monocular')
    excluded=detail(item, title=item.title, description='Analog generation 1 device')
    browser.save_detail(item, excluded)
    calls=[];setup_scan(monkeypatch, [item], calls)
    fresh=asyncio.run(browser.browser_scan('vinted', settings()))
    assert fresh.items==[] and calls==[]
    browser.save_detail(item, excluded, checked=time.time()-browser.DETAIL_CACHE_TTL-1)
    refreshed=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1'] and len(refreshed.items)==1
    assert refreshed.items[0].description=='Used analog image intensifier generation 2.'


def test_cached_new_condition_persists_until_details_refresh(fake_browser, monkeypatch):
    item=card(used=None)
    new_detail=detail(item, used=False)
    browser.save_detail(item, new_detail)
    merged=browser.merge_detail_metadata(item, cached())
    assert merged.used is False
    calls=[];setup_scan(monkeypatch, [item], calls)
    fresh=asyncio.run(browser.browser_scan('vinted', settings()))
    assert fresh.items==[] and calls==[]
    browser.save_detail(item, new_detail, checked=time.time()-browser.DETAIL_CACHE_TTL-1)
    async def now_used(_page, item):
        calls.append(item.external_id)
        return detail(item, used=True)
    monkeypatch.setattr(browser, 'enrich', now_used)
    refreshed=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==['1'] and refreshed.items[0].used is True


@pytest.mark.parametrize('changes', [{'used':False}, {'title':'Used night vision Gen 1'}])
def test_explicit_fresh_exclusion_overrides_cached_detail(fake_browser, monkeypatch, changes):
    original=card(used=None)
    browser.save_detail(original, detail(original, used=True))
    current=card(used=None, **changes) if 'used' not in changes else card(**changes)
    calls=[];setup_scan(monkeypatch, [current], calls)
    result=asyncio.run(browser.browser_scan('vinted', settings()))
    assert calls==[] and result.items==[]


def test_explicit_used_card_overrides_cached_new_condition():
    original=card(used=None)
    browser.save_detail(original, detail(original, used=False))
    changed=card(used=True)
    merged=browser.merge_detail_metadata(changed, cached())
    assert merged.used is True
