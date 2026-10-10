import os, json, sqlite3, contextlib, time
from pathlib import Path
DATA = Path(os.getenv('DATA_DIR', '/data'))
DEFAULTS = dict(max_price=400, interval=15, browser_delay=10, paused=False, keywords=['naktinio matymo','naktinis matymas','noktowizor','noktowizja','night vision','gen 2','gen 2+','NVG30','NVG50'], categories=['gen2','gen2plus','analog_unknown','nvg30','nvg50'], price_drop=10, ntfy_enabled=True, ntfy_url='https://ntfy.sh', ntfy_topic=os.getenv('NTFY_TOPIC',''), ntfy_token='', allegro_client_id='', allegro_client_secret='', allegro_access_token='', allegro_enabled=True, vinted_enabled=True, skelbiu_enabled=True)
@contextlib.contextmanager
def db():
    c = sqlite3.connect(DATA/'tracker.sqlite3', timeout=20)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
def init():
    DATA.mkdir(parents=True,exist_ok=True); (DATA/'inbox').mkdir(exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY,status TEXT,last_check REAL,last_success REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY,source TEXT NOT NULL,external_id TEXT NOT NULL,title TEXT,description TEXT,url TEXT,image TEXT,currency TEXT,price REAL,shipping REAL,fees REAL,total_eur REAL,category TEXT,used INTEGER,first_seen REAL,last_seen REAL,favorite INTEGER DEFAULT 0,ignored INTEGER DEFAULT 0,matched INTEGER DEFAULT 0,fx_rate REAL,UNIQUE(source,external_id));
        CREATE TABLE IF NOT EXISTS prices(id INTEGER PRIMARY KEY,listing_id INTEGER REFERENCES listings(id),checked REAL,price REAL,total_eur REAL,shipping REAL,fees REAL,fx_rate REAL,currency TEXT);
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,listing_id INTEGER REFERENCES listings(id),event_key TEXT UNIQUE,title TEXT,body TEXT,url TEXT,state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT,created REAL,sent REAL);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,time REAL,source TEXT,level TEXT,message TEXT);
        CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS detail_cache(source TEXT NOT NULL,external_id TEXT NOT NULL,fingerprint TEXT NOT NULL,checked REAL NOT NULL,data TEXT NOT NULL,PRIMARY KEY(source,external_id));
        ''')
        c.execute('BEGIN IMMEDIATE')
        if 'currency' not in [r['name'] for r in c.execute('PRAGMA table_info(prices)')]: c.execute('ALTER TABLE prices ADD COLUMN currency TEXT')
        columns={r['name'] for r in c.execute('PRAGMA table_info(listings)')}
        if 'irrelevant' not in columns: c.execute('ALTER TABLE listings ADD COLUMN irrelevant INTEGER NOT NULL DEFAULT 0')
        if 'learned_reason' not in columns: c.execute("ALTER TABLE listings ADD COLUMN learned_reason TEXT NOT NULL DEFAULT ''")
        c.execute('CREATE INDEX IF NOT EXISTS idx_listing_match ON listings(matched,ignored,first_seen)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_listing_found ON listings(first_seen,id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_listing_irrelevant ON listings(irrelevant)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_prices_listing ON prices(listing_id,id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_notify_pending ON notifications(state,next_try)')
        for k,v in DEFAULTS.items(): c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        for s in ['vinted','skelbiu','allegro']: c.execute('INSERT OR IGNORE INTO sources(name,status) VALUES(?,?)',(s,'not_checked'))
def settings():
    with db() as c: return {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
def save(values):
    with db() as c:
        for k,v in values.items(): c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,json.dumps(v)))
def log(source,message,level='info'):
    with db() as c:
        c.execute('INSERT INTO logs(time,source,level,message) VALUES(?,?,?,?)',(time.time(),source,level,message[:1000]))
        c.execute('DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)')
def runtime(k,v=None):
    with db() as c:
        if v is not None: c.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)',(k,str(v))); return v
        r=c.execute('SELECT value FROM runtime WHERE key=?',(k,)).fetchone(); return r['value'] if r else None
