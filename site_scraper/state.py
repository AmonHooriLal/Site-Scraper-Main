"""Crawl state (SQLite), stop signal and the polite rate limiter."""
from .helpers import *  # noqa: F401,F403  (shared imports, constants and helpers)

# ─── Crawl state (SQLite, so memory stays flat on huge sites) ────────────────────

class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS urls(
                url TEXT PRIMARY KEY, status TEXT, depth INT, src INT, section TEXT, lastmod TEXT,
                etag TEXT, last_modified TEXT, http INT, error TEXT, rendered INT DEFAULT 0, hash TEXT,
                title TEXT, words INT, canonical TEXT, md_path TEXT, found_on TEXT);
            CREATE INDEX IF NOT EXISTS ix_status ON urls(status, src, depth);
            CREATE TABLE IF NOT EXISTS assets(
                url TEXT PRIMARY KEY, kind TEXT, status TEXT, path TEXT, bytes INT, page TEXT, alt TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS hashes(hash TEXT PRIMARY KEY, url TEXT);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)

    def many(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)

    def commit(self):
        with self.lock:
            self.db.commit()

    def count(self, where="1=1", args=()):
        return self.q(f"SELECT COUNT(*) FROM urls WHERE {where}", args)[0][0]

    def get_meta(self, k, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0][0]) if r else default

    def set_meta(self, k, v):
        self.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))


class StopCrawl(Exception):
    """Raised inside workers once the user/time/disk limit asked the crawl to stop."""


# ─── Polite, adaptive rate limiter ──────────────────────────────────────────────

class Limiter:
    def __init__(self, delay):
        self.base = self.delay = max(delay, 0.0)
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.delay
        if at > now:
            time.sleep(at - now)

    def slow_down(self, retry_after=None):
        with self.lock:
            self.delay = min(max(self.delay * 2, 0.5), 10.0)
            if retry_after:
                self.next_at = max(self.next_at, time.monotonic() + min(retry_after, 120))

    def speed_up(self):
        with self.lock:
            self.delay = max(self.base, self.delay * 0.95)
