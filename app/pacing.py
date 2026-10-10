"""Persistent, conservative scheduling for one keyword at a time.

The configured interval is one full keyword rotation, not the delay between
individual browser requests. Manual scans obey the same minimum spacing and
source cooldowns as scheduled work.
"""
import hashlib
import json
import math
import time

from . import store

SOURCES = ('vinted', 'skelbiu', 'allegro')
MIN_STEP_SECONDS = 60
DETAILS_PER_STEP = 2
DETAILS_PER_ROTATION = 12


def _number(value, default=0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def keywords(s):
    # Keep the user's order but avoid performing the same search twice.
    return list(dict.fromkeys(str(k).strip() for k in s.get('keywords', []) if str(k).strip()))


def step_seconds(s):
    return max(MIN_STEP_SECONDS, max(1, _number(s.get('interval'), 15)) * 60 / max(1, len(keywords(s))))


def _read(c, key):
    row = c.execute('SELECT value FROM runtime WHERE key=?', (key,)).fetchone()
    return row['value'] if row else None


def _write(c, key, value):
    c.execute('INSERT OR REPLACE INTO runtime(key,value) VALUES(?,?)', (key, str(value)))


def _state(c, source, s, now):
    terms = keywords(s)
    signature = hashlib.sha256(json.dumps({'keywords': terms, 'interval': s.get('interval', 15)}, sort_keys=True).encode()).hexdigest()
    saved = _read(c, source + '_pacing')
    try:
        state = json.loads(saved or '{}')
        if not isinstance(state, dict): state = {}
    except (ValueError, TypeError):
        state = {}
    last = _number(state.get('last_attempt'))
    due = _number(state.get('next_due'))
    if saved is None:
        # Upgrade the old global schedule without firing a fresh request just
        # because this worker was recreated before the source's next step.
        last = max(last, _number(_read(c, 'last_scan')))
        if last: due = max(due, last + step_seconds(s))
    if state.get('signature') != signature:
        # Configuration changes restart the rotation, without overriding a
        # previously reserved request or shortening an existing wait.
        state = {'signature': signature, 'cursor': 0, 'rotation_attempts': 0,
                 'last_attempt': last, 'next_due': max(due, last + MIN_STEP_SECONDS if last else now)}
    state['cursor'] = max(0, int(_number(state.get('cursor')))) % max(1, len(terms))
    state['rotation_attempts'] = max(0, int(_number(state.get('rotation_attempts'))))
    state['last_attempt'] = last
    state['next_due'] = max(_number(state.get('next_due')), last + MIN_STEP_SECONDS if last else 0)
    return state, terms


def due_sources(s, force=False, now=None):
    """Reserve and return at most one keyword for every eligible source.

    Reserving in the same SQLite transaction prevents a restart or concurrent
    manual request from repeating a step within sixty seconds. Force bypasses
    the ordinary schedule only; it never bypasses minimum spacing or blocks.
    """
    now = time.time() if now is None else float(now)
    due = []
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        for source in SOURCES:
            saved = _read(c, source + '_pacing')
            state, terms = _state(c, source, s, now)
            blocked = _number(_read(c, source + '_browser_blocked_until'))
            state['next_due'] = max(state['next_due'], blocked)
            enabled = s.get(source + '_enabled', True)
            last = state['last_attempt']
            spaced = not last or now >= last + MIN_STEP_SECONDS
            if enabled and terms and now >= blocked and spaced and (force or now >= state['next_due']):
                due.append((source, terms[state['cursor']]))
                state['last_attempt'] = now
                state['next_due'] = now + step_seconds(s)
            encoded = json.dumps(state, separators=(',', ':'))
            if encoded != saved:
                _write(c, source + '_pacing', encoded)
    return due


def detail_budget(source, s):
    with store.db() as c:
        state, _ = _state(c, source, s, time.time())
    return max(0, min(DETAILS_PER_STEP, DETAILS_PER_ROTATION - state['rotation_attempts']))


def complete(source, s, attempts=0, now=None):
    """Advance a reserved keyword and preserve both pacing and block deadlines."""
    now = time.time() if now is None else float(now)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        state, terms = _state(c, source, s, now)
        state['rotation_attempts'] += max(0, int(_number(attempts)))
        state['cursor'] += 1
        if state['cursor'] >= max(1, len(terms)):
            state['cursor'] = 0
            state['rotation_attempts'] = 0
        state['next_due'] = max(state['next_due'], state['last_attempt'] + step_seconds(s),
                                now + MIN_STEP_SECONDS, _number(_read(c, source + '_browser_blocked_until')))
        _write(c, source + '_pacing', json.dumps(state, separators=(',', ':')))
        return state['next_due']
