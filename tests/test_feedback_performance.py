"""Feedback retains its decisions while avoiding work per item × example."""
import re
import pytest
from app import feedback, store
from app.domain import Listing, normalized


def item(title, **changes):
    values = dict(source='vinted', external_id='candidate', title=title,
                  url='https://www.vinted.lt/items/candidate', price=200)
    values.update(changes)
    return Listing(**values)


def original_reason(candidate, examples):
    """Reference behavior before feedback was prepared in batches."""
    title = normalized(candidate.title)
    protected = bool(re.search(r'\b(?:nvg[ -]?(?:30|50)|pvs[ -]?(?:7|14))\b', title))
    candidate_tokens = feedback.tokens(candidate.title)
    for example in examples:
        if example['source'] == candidate.source and example['external_id'] == candidate.external_id:
            return 'Marked irrelevant by you'
        original = normalized(example['title'])
        if not protected:
            for family, pattern in feedback.FAMILIES.items():
                if re.search(pattern, original) and re.search(pattern, title):
                    return 'Learned from your feedback: ' + family
        known = feedback.tokens(example['title'])
        overlap = known & candidate_tokens
        if len(overlap) >= 3 and len(overlap) / max(len(known), len(candidate_tokens)) >= 0.8:
            return 'Similar to an item you marked irrelevant'
        if original == title:
            return 'Same title as an item you marked irrelevant'
    return ''


@pytest.mark.parametrize('titles', [
    ['Baby CCTV monitor night vision', 'Acme smart wireless indoor monitor', 'Naktinio matymo kamera'],
    ['PVS-14 baby camera', 'NVG50 indoor security camera', 'Analog Gen 2'],
    ['Kūdikio kamera', 'Niania elektroniczna', 'IP camera night vision'],
    ['Acme smart wireless indoor monitor', 'Baby CCTV monitor night vision'],
    [],
])
def test_prepared_feedback_preserves_decisions_and_order(titles):
    examples = [dict(source='vinted', external_id=str(index), title=title) for index, title in enumerate(titles)]
    prepared = feedback.prepare(examples)
    candidates = titles + [
        'Infant nursery camera', 'Security camera baby monitor', 'NVG30 baby monitor',
        'PVS-7 surveillance camera', 'Acme smart wireless indoor monitor black',
        'Acme smart wireless monitor', 'Acme indoor unrelated monitor',
        'Naktinio matymo kamera', 'KUDIKIO KAMERA', 'Used analog Gen 2 night vision',
    ]
    for title in candidates:
        for external_id in ('candidate', '0', '1'):
            candidate = item(title, external_id=external_id)
            expected = original_reason(candidate, examples)
            assert feedback.reason(candidate, examples) == expected
            assert feedback.reason(candidate, prepared) == expected


def test_preparation_is_immutable_snapshot_and_idempotent():
    examples = [dict(source='vinted', external_id='marked', title='Baby camera')]
    prepared = feedback.prepare(examples)
    assert feedback.prepare(prepared) is prepared
    examples[0]['title'] = 'Unrelated accessory'
    assert feedback.reason(item('Infant camera'), prepared) == 'Learned from your feedback: baby cameras'
    assert feedback.reason(item('Infant camera'), examples) == ''
    with pytest.raises(AttributeError):
        prepared.records = ()


def test_batch_normalization_cost_is_linear(monkeypatch):
    calls = []
    def counted(value):
        calls.append(value)
        return normalized(value)
    monkeypatch.setattr(feedback, 'normalized', counted)
    examples = [dict(source='vinted', external_id=str(i), title=f'Unrelated brand model{i}') for i in range(100)]
    prepared = feedback.prepare(examples)
    for index in range(200):
        assert feedback.reason(item(f'Used analog night vision candidate{index}'), prepared) == ''
    assert len(calls) == 300


def test_refresh_preserves_history_and_avoids_unchanged_writes():
    from app.worker import ingest
    candidate = item('Used analog Gen 2 night vision')
    ident = ingest(candidate, 4.2, store.settings())
    with store.db() as c:
        before = dict(c.execute('SELECT * FROM listings WHERE id=?', (ident,)).fetchone())
        changes = c.total_changes
        feedback.refresh(c, store.settings())
        assert c.total_changes == changes
        after = dict(c.execute('SELECT * FROM listings WHERE id=?', (ident,)).fetchone())
        assert after == before
        assert c.execute('SELECT COUNT(*) FROM prices').fetchone()[0] == 1


def test_refresh_cancels_pending_alert_even_when_listing_is_unchanged():
    from app.worker import ingest
    ident = ingest(item('Used analog Gen 2 night vision'), 4.2, store.settings())
    with store.db() as c:
        c.execute("UPDATE listings SET irrelevant=1,matched=0,learned_reason='Marked irrelevant by you' WHERE id=?", (ident,))
        feedback.refresh(c, store.settings())
        assert c.execute('SELECT state FROM notifications WHERE listing_id=?', (ident,)).fetchone()[0] == 'cancelled'
