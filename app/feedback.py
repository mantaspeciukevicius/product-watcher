"""Conservative, reversible learning from listings explicitly marked irrelevant."""
import re
from dataclasses import dataclass
from .domain import normalized, Listing, classify, matches

FAMILIES = {
    'baby cameras': r'\b(?:baby|babies|infant|nursery|babymonitor|niania|niani\w*|kudiki\w*|kudikel\w*|vaik\w*)\b',
    'security cameras': r'\b(?:cctv|surveillance|ip camera|security camera|stebejimo kamer\w*|monitoring camera)\b',
}
STOP = set('night vision naktinio matymo naktinis matymas noktowizor noktowizja gen generation analog digital used camera kamera kamer cameras with and the for a an lt pl eur'.split())
_FAMILY_PATTERNS = tuple((name, re.compile(pattern)) for name, pattern in FAMILIES.items())


@dataclass(frozen=True)
class _Example:
    source: str
    external_id: str
    title: str
    tokens: frozenset[str]
    families: frozenset[str]


@dataclass(frozen=True)
class PreparedExamples:
    """Immutable feedback snapshot shared by a short ingestion transaction."""
    records: tuple[_Example, ...]


def _tokens(title):
    return frozenset(t for t in re.findall(r'[a-z0-9]+', title) if len(t)>2 and t not in STOP and not t.isdigit())

def tokens(title):
    return set(_tokens(normalized(title)))


def prepare(examples):
    """Normalize each example once, preserving its original matching order."""
    if isinstance(examples, PreparedExamples):
        return examples
    records = []
    for example in examples:
        title = normalized(example['title'])
        records.append(_Example(
            example['source'], example['external_id'], title, _tokens(title),
            frozenset(name for name, pattern in _FAMILY_PATTERNS if pattern.search(title)),
        ))
    return PreparedExamples(tuple(records))

def reason(item, examples):
    # Examples use titles only: seller descriptions often contain unrelated keywords.
    title=normalized(item.title)
    protected=bool(re.search(r'\b(?:nvg[ -]?(?:30|50)|pvs[ -]?(?:7|14))\b',title))
    candidate=_tokens(title)
    families=frozenset(name for name, pattern in _FAMILY_PATTERNS if pattern.search(title)) if not protected else frozenset()
    for example in prepare(examples).records:
        if example.source==item.source and example.external_id==item.external_id:
            return 'Marked irrelevant by you'
        if not protected:
            for family, _ in _FAMILY_PATTERNS:
                if family in example.families and family in families:
                    return 'Learned from your feedback: '+family
        known=example.tokens
        overlap=known & candidate
        if len(overlap)>=3 and len(overlap)/max(len(known),len(candidate))>=0.8:
            return 'Similar to an item you marked irrelevant'
        if example.title==title:
            return 'Same title as an item you marked irrelevant'
    return ''

def examples(c):
    return c.execute('SELECT source,external_id,title FROM listings WHERE irrelevant=1').fetchall()

def refresh(c,s):
    """Apply feedback to saved records immediately, without generating new alerts."""
    learned=prepare(examples(c))
    for row in c.execute('SELECT * FROM listings').fetchall():
        item=Listing(**{k:row[k] for k in ('source','external_id','title','description','url','image','price','currency','shipping','fees','used')})
        why=reason(item,learned); category=classify(item)
        match=matches(item,category,row['total_eur'],s) and not why
        if (row['matched'],row['category'],row['learned_reason']) != (int(match),category,why):
            c.execute('UPDATE listings SET matched=?,category=?,learned_reason=? WHERE id=?',(int(match),category,why,row['id']))
    c.execute("UPDATE notifications SET state='cancelled' WHERE state='pending' AND listing_id IN (SELECT id FROM listings WHERE matched=0)")
