"""All scheduling and access-block checks use synthetic times/pages."""
import asyncio
import json
from types import SimpleNamespace
from email.utils import formatdate

import pytest

from app import pacing, store
from app import browser_sources as browser


def settings(**changes):
    return {**store.settings(), 'keywords': ['night vision', 'noktowizor', 'NVG30'],
            'interval': 15, **changes}


def state(source='vinted'):
    return json.loads(store.runtime(source + '_pacing'))


def test_one_keyword_per_source_spread_over_rotation_and_durable_restart():
    s = settings()
    assert pacing.due_sources(s, now=1000) == [(name, 'night vision') for name in pacing.SOURCES]
    for name in pacing.SOURCES: pacing.complete(name, s, attempts=2, now=1005)
    assert pacing.due_sources(s, now=1299) == []
    assert pacing.due_sources(s, now=1300) == [(name, 'noktowizor') for name in pacing.SOURCES]
    for name in pacing.SOURCES: pacing.complete(name, s, attempts=1, now=1305)
    # A fresh caller reads the persisted state, not a process-local timer.
    assert state()['cursor'] == 2
    assert pacing.due_sources(s, now=1600) == [(name, 'NVG30') for name in pacing.SOURCES]
    for name in pacing.SOURCES: pacing.complete(name, s, now=1605)
    assert state()['cursor'] == 0
    assert state()['rotation_attempts'] == 0
    assert pacing.due_sources(s, now=1900) == [(name, 'night vision') for name in pacing.SOURCES]


def test_manual_scan_never_overrides_minimum_spacing_or_source_block():
    s = settings(skelbiu_enabled=False, allegro_enabled=False)
    assert pacing.due_sources(s, now=1000) == [('vinted', 'night vision')]
    pacing.complete('vinted', s, now=1000)
    assert pacing.due_sources(s, force=True, now=1059) == []
    assert pacing.due_sources(s, force=True, now=1060) == [('vinted', 'noktowizor')]
    pacing.complete('vinted', s, now=1070)
    store.runtime('vinted_browser_blocked_until', 5000)
    assert pacing.due_sources(s, force=True, now=4999) == []
    assert pacing.due_sources(s, force=True, now=5000) == [('vinted', 'NVG30')]


def test_changes_preserve_pending_waits_and_cooldown():
    s = settings()
    pacing.due_sources(s, now=1000)
    pacing.complete('vinted', s, attempts=2, now=1005)
    changed = settings(keywords=['new search'], interval=1, skelbiu_enabled=False, allegro_enabled=False)
    assert pacing.due_sources(changed, now=1100) == []
    assert state()['cursor'] == 0 and state()['rotation_attempts'] == 0
    assert state()['next_due'] == 1300
    store.runtime('vinted_browser_blocked_until', 6000)
    assert pacing.due_sources(changed, force=True, now=5999) == []
    assert pacing.due_sources(changed, now=6000) == [('vinted', 'new search')]


def test_very_short_intervals_still_space_sources_at_least_sixty_seconds():
    s = settings(interval=1, keywords=[str(i) for i in range(20)])
    assert pacing.step_seconds(s) == 60
    pacing.due_sources(s, now=1000)
    pacing.complete('vinted', s, now=1050)
    assert state()['next_due'] == 1110
    assert pacing.due_sources(s, now=1109) == [('skelbiu', '0'), ('allegro', '0')]
    assert pacing.due_sources(s, now=1110) == [('vinted', '1')]


def test_reserved_attempt_survives_process_failure_without_completion():
    s = settings(skelbiu_enabled=False, allegro_enabled=False)
    assert pacing.due_sources(s, now=1000)
    assert pacing.due_sources(s, force=True, now=1059) == []
    assert pacing.due_sources(s, now=1300) == [('vinted', 'night vision')]


def test_detail_attempts_budget_is_per_step_and_entire_rotation():
    s = settings(keywords=[str(i) for i in range(8)])
    for index in range(8):
        pacing.due_sources(s, now=1000+index*120)
        assert pacing.detail_budget('vinted', s) == (2 if index < 6 else 0)
        pacing.complete('vinted', s, attempts=2 if index < 6 else 0, now=1000+index*120)
    assert pacing.detail_budget('vinted', s) == 2


def test_disabled_and_empty_keywords_never_schedule():
    s = settings(vinted_enabled=False, skelbiu_enabled=False, allegro_enabled=False)
    assert pacing.due_sources(s, force=True, now=1000) == []
    assert pacing.due_sources(settings(keywords=['', '  ']), force=True, now=1000) == []
    assert pacing.keywords(settings(keywords=['NVG30', ' NVG30 ', 'night vision'])) == ['NVG30', 'night vision']


def test_access_blocks_escalate_and_retry_after_is_respected():
    for index, expected in enumerate([3600, 7200, 14400, 28800, 57600, 86400, 86400]):
        now = 1000 + index * 100000
        blocked = browser.block_source('allegro', 'HTTP 403', now=now)
        assert blocked.next_retry == now+expected
        assert int(store.runtime('allegro_browser_block_count')) == index+1
    # An existing longer wait must never be shortened by a new block.
    previous = float(store.runtime('allegro_browser_blocked_until'))
    blocked = browser.block_source('allegro', 'HTTP 429', retry_after='200000', now=1000)
    assert blocked.next_retry == max(previous, 201000)
    assert browser.retry_after_seconds('3601', now=1000) == 3601
    assert browser.retry_after_seconds(formatdate(10000, usegmt=True), now=1000) == 9000
    assert browser.retry_after_seconds('invalid', now=1000) == 0
    assert browser.retry_after_seconds('-5', now=1000) == 0


def test_request_spacing_is_persisted_between_browser_invocations(monkeypatch):
    clock = [1000.0]
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(browser.time, 'time', lambda: clock[0])
    monkeypatch.setattr(browser.asyncio, 'sleep', sleep)
    asyncio.run(browser.wait_request_slot('vinted', 10))
    asyncio.run(browser.wait_request_slot('vinted', 10))
    assert waits == [10]
    assert float(store.runtime('vinted_last_browser_request')) == 1010
    # A second source has a separate request clock.
    asyncio.run(browser.wait_request_slot('skelbiu', 10))
    assert waits == [10]
    asyncio.run(browser.wait_request_slot('vinted', 0))
    assert waits == [10, 3]
    asyncio.run(browser.wait_request_slot('vinted', 1000))
    assert waits[-1] == 60


def test_cooldown_skip_does_not_escalate_and_makes_no_request(monkeypatch):
    monkeypatch.setattr(browser.time, 'time', lambda: 1000)
    blocked = browser.block_source('skelbiu', 'HTTP 403', now=1000)
    with pytest.raises(browser.BrowserBlocked) as caught:
        asyncio.run(browser.wait_request_slot('skelbiu', 10))
    assert caught.value.next_retry == blocked.next_retry
    assert int(store.runtime('skelbiu_browser_block_count')) == 1
    assert store.runtime('skelbiu_last_browser_request') is None
    with pytest.raises(browser.BrowserBlocked):
        asyncio.run(browser.browser_scan('skelbiu', settings()))
    assert int(store.runtime('skelbiu_browser_block_count')) == 1


def test_visit_reads_retry_after_and_stops_without_other_navigation(monkeypatch):
    async def slot(source): pass
    monkeypatch.setattr(browser, 'wait_request_slot', slot)
    monkeypatch.setattr(browser.time, 'time', lambda: 1000)
    class Page:
        requests = []
        async def goto(self, url, **kwargs):
            self.requests.append(url)
            return SimpleNamespace(status=429, headers={'retry-after': '8000'})
        async def wait_for_timeout(self, value): pass
        async def title(self): return 'Marketplace'
        def locator(self, selector):
            return SimpleNamespace(inner_text=self.body, count=self.count)
        async def body(self, **kwargs): return 'Try later'
        async def count(self): return 0
    page = Page()
    with pytest.raises(browser.BrowserBlocked) as caught:
        asyncio.run(browser.visit(page, 'https://allegro.pl/listing', 'allegro'))
    assert caught.value.next_retry == 9000
    assert page.requests == ['https://allegro.pl/listing']
    assert int(store.runtime('allegro_browser_block_count')) == 1


def test_explicit_step_budget_counts_failed_details_and_blocked_detail_attempts(monkeypatch):
    from app.domain import Listing
    cards = [Listing(source='vinted', external_id=str(i), title='Used night vision Gen 2',
                     url=f'https://www.vinted.lt/items/{i}', price=300, used=True) for i in range(4)]
    class Context:
        async def new_page(self): return object()
        async def storage_state(self, **kwargs): pass
        async def close(self): pass
    class Browser:
        async def new_context(self, **kwargs): return Context()
        async def close(self): pass
    class Manager:
        async def __aenter__(self): return SimpleNamespace(chromium=SimpleNamespace(launch=self.launch))
        async def launch(self, **kwargs): return Browser()
        async def __aexit__(self, *args): pass
    async def visit(*args): return 'Results'
    async def extract(*args): return cards, len(cards)
    calls = []
    async def enrich(page, item):
        calls.append(item.external_id)
        raise browser.SourceError('Mock temporary failure')
    monkeypatch.setattr(browser, 'async_playwright', Manager)
    monkeypatch.setattr(browser, 'visit', visit)
    monkeypatch.setattr(browser, 'extract_cards', extract)
    monkeypatch.setattr(browser, 'enrich', enrich)
    s = settings(keywords=['night vision'], _detail_budget=2)
    result = asyncio.run(browser.browser_scan('vinted', s))
    assert result.attempts == 2 and result.details == 0 and len(calls) == 2
    async def blocked(page, item): raise browser.BrowserBlocked('Mock challenge', next_retry=5000)
    monkeypatch.setattr(browser, 'enrich', blocked)
    with pytest.raises(browser.BrowserBlocked) as caught:
        asyncio.run(browser.browser_scan('vinted', s))
    assert caught.value.attempts == 1 and len(caught.value.items) == 4


def test_completed_browser_scan_resets_block_streak_but_parse_failures_do_not(monkeypatch):
    class Context:
        async def new_page(self): return object()
        async def storage_state(self, **kwargs): pass
        async def close(self): pass
    class Browser:
        async def new_context(self, **kwargs): return Context()
        async def close(self): pass
    class Manager:
        async def __aenter__(self): return SimpleNamespace(chromium=SimpleNamespace(launch=self.launch))
        async def launch(self, **kwargs): return Browser()
        async def __aexit__(self, *args): pass
    async def visit(*args): return 'No items found'
    async def extract(*args): return [], 0
    monkeypatch.setattr(browser, 'async_playwright', Manager)
    monkeypatch.setattr(browser, 'visit', visit)
    monkeypatch.setattr(browser, 'extract_cards', extract)
    store.runtime('vinted_browser_block_count', 3)
    asyncio.run(browser.browser_scan('vinted', settings()))
    assert int(store.runtime('vinted_browser_block_count')) == 0
    store.runtime('vinted_browser_block_count', 3)
    async def bad_visit(*args): return 'Unrecognized layout'
    monkeypatch.setattr(browser, 'visit', bad_visit)
    with pytest.raises(browser.BrowserParseError):
        asyncio.run(browser.browser_scan('vinted', settings()))
    assert int(store.runtime('vinted_browser_block_count')) == 3


def test_upgrade_honors_saved_global_scan_and_force_spacing():
    s = settings(skelbiu_enabled=False, allegro_enabled=False)
    store.runtime('last_scan', 1000)
    assert pacing.due_sources(s, now=1001) == []
    assert state()['last_attempt'] == 1000
    assert state()['next_due'] == 1300
    assert pacing.due_sources(s, force=True, now=1059) == []
    assert pacing.due_sources(s, now=1299) == []
    assert pacing.due_sources(s, force=True, now=1060) == [('vinted', 'night vision')]


def test_upgrade_resumes_normal_schedule_after_saved_step_gap():
    store.runtime('last_scan', 1000)
    s = settings()
    assert pacing.due_sources(s, now=1299) == []
    assert pacing.due_sources(s, now=1300) == [(name, 'night vision') for name in pacing.SOURCES]


def test_noop_scheduler_poll_does_not_write_unchanged_runtime(monkeypatch):
    original = pacing._write
    writes = []
    def write(c, key, value):
        writes.append(key)
        original(c, key, value)
    monkeypatch.setattr(pacing, '_write', write)
    s = settings()
    pacing.due_sources(s, now=1000)
    assert len(writes) == 3
    writes.clear()
    for now in range(1005, 1300, 5):
        assert pacing.due_sources(s, now=now) == []
    assert writes == []
    # Disabled sources also remain unchanged across scheduler polls.
    s = settings(vinted_enabled=False, skelbiu_enabled=False, allegro_enabled=False)
    assert pacing.due_sources(s, now=1300) == []
    assert writes == []
