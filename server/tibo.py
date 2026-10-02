"""Cache Tibo's public text-only RSS posts for the quota dashboard."""

import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import jsonify

RSS_URL = 'https://x.noodl3.net/thsottiaux/rss'
PROFILE_URL = 'https://x.com/thsottiaux'
REFRESH_SECONDS = 600
MAX_FEED_BYTES = 512 * 1024
POST_LIMIT = 20
RETAIN_LIMIT = 80
POST_PATH = re.compile(r'^/thsottiaux/status/([0-9]{10,25})$')
QUOTA_WORDS = re.compile(r'\b(?:quotas?|usage|rate[ -]?limits?|allowance|allocations?|'
                         r'usage[ -]?caps?|message[ -]?caps?|weekly[ -]?limits?|'
                         r'global[ -]?resets?)\b|额度|用量|限额|重置', re.IGNORECASE)
LIMIT_CHANGE = re.compile(r'\b(?:limits?\s+(?:reset|increase|increased|lifted|raised)|'
                          r'(?:increase|raise|lift|reset)\s+(?:the\s+)?limits?)\b', re.IGNORECASE)


def quota_related(text):
    """Conservative text match; public posts are context, not proof of a reset."""
    return bool(QUOTA_WORDS.search(text) or LIMIT_CHANGE.search(text))


def parse_feed(raw):
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError('RSS 响应过大')
    root = ET.fromstring(raw)
    channel = root.find('channel')
    if root.tag != 'rss' or channel is None:
        raise ValueError('RSS 格式无效')
    title = (channel.findtext('title') or '').lower()
    if '@thsottiaux' not in title:
        raise ValueError('RSS 账号不匹配')
    posts = {}
    for item in channel.findall('item'):
        source = urlsplit((item.findtext('link') or '').strip())
        match = POST_PATH.fullmatch(source.path)
        if source.scheme != 'https' or source.hostname != 'x.noodl3.net' or not match:
            continue
        text = (item.findtext('title') or '').strip()
        text = re.sub(r'[\t\r\n ]+', ' ', text)
        if not text or len(text) > 4000:
            continue
        try:
            published = parsedate_to_datetime(item.findtext('pubDate') or '').timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        post_id = match.group(1)
        posts[post_id] = {
            'id': post_id,
            'text': text,
            'published_at': published,
            'url': f'https://x.com/thsottiaux/status/{post_id}',
        }
    ordered = sorted(posts.values(), key=lambda post: (post['published_at'], post['id']), reverse=True)
    if not ordered:
        raise ValueError('RSS 未返回有效的 Tibo 帖文')
    return ordered[:POST_LIMIT]


def fetch_posts():
    request = Request(RSS_URL, headers={
        'User-Agent': 'CodexQuotaMonitor/1.0 (+text-only RSS cache)',
        'Accept': 'application/rss+xml, application/xml;q=0.9',
    })
    with urlopen(request, timeout=12) as response:
        raw = response.read(MAX_FEED_BYTES + 1)
    return parse_feed(raw)


class TiboCache:
    def __init__(self, app, db, bark):
        self.app = app
        self.db = db
        self.bark = bark
        self.lock = threading.Lock()
        with db() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS tibo_posts (
                    post_id TEXT PRIMARY KEY,
                    published_at REAL NOT NULL,
                    text TEXT NOT NULL,
                    original_url TEXT NOT NULL,
                    cached_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tibo_posts_time ON tibo_posts(published_at DESC, post_id DESC);
                CREATE TABLE IF NOT EXISTS tibo_feed_state (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    fetched_at REAL,
                    attempted_at REAL,
                    last_error TEXT
                );
                INSERT OR IGNORE INTO tibo_feed_state(id) VALUES (1);
            ''')

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            attempted = time.time()
            try:
                posts = fetch_posts()
            except Exception as exc:
                self.app.logger.warning('Tibo RSS update failed: %s', exc)
                with self.db() as conn:
                    conn.execute('UPDATE tibo_feed_state SET attempted_at=?, last_error=? WHERE id=1',
                                 (attempted, '公开 RSS 暂时无法更新'))
                return False
            with self.db() as conn:
                known_ids = {row['post_id'] for row in conn.execute('SELECT post_id FROM tibo_posts')}
                conn.executemany('''
                    INSERT INTO tibo_posts(post_id,published_at,text,original_url,cached_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(post_id) DO UPDATE SET
                        published_at=excluded.published_at,
                        text=excluded.text,
                        original_url=excluded.original_url,
                        cached_at=excluded.cached_at
                ''', [(post['id'], post['published_at'], post['text'], post['url'], attempted)
                      for post in posts])
                conn.execute('''DELETE FROM tibo_posts WHERE post_id NOT IN
                    (SELECT post_id FROM tibo_posts ORDER BY published_at DESC, post_id DESC LIMIT ?)''',
                    (RETAIN_LIMIT,))
                conn.execute('''UPDATE tibo_feed_state SET fetched_at=?, attempted_at=?, last_error=NULL
                                WHERE id=1''', (attempted, attempted))
                related = [post for post in posts if quota_related(post['text'])]
                fresh = [post for post in related if post['id'] not in known_ids]
                self.bark.on_tibo_posts(conn, fresh, related, attempted)
            return True
        finally:
            self.lock.release()

    def snapshot(self):
        with self.db() as conn:
            state = conn.execute('SELECT fetched_at,attempted_at,last_error FROM tibo_feed_state WHERE id=1').fetchone()
            rows = conn.execute('''SELECT post_id,published_at,text,original_url FROM tibo_posts
                                   ORDER BY published_at DESC, post_id DESC LIMIT ?''', (POST_LIMIT,)).fetchall()
        return dict(posts=[dict(id=row['post_id'], published_at=row['published_at'],
                                text=row['text'], url=row['original_url']) for row in rows],
                    fetched_at=state['fetched_at'], attempted_at=state['attempted_at'],
                    last_error=state['last_error'], source='公开 RSS 镜像', source_url=RSS_URL,
                    profile_url=PROFILE_URL, refresh_seconds=REFRESH_SECONDS)

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception as exc:
                self.app.logger.warning('Tibo update failed: %s', type(exc).__name__)
            threading.Event().wait(REFRESH_SECONDS)


def register_tibo(app, db, auth, bark):
    cache = TiboCache(app, db, bark)

    @app.get('/api/tibo')
    @auth('read')
    def tibo_feed():
        return jsonify(cache.snapshot())

    if not app.config.get('TESTING') and app.config.get('TIBO_WORKER_ENABLED', True):
        threading.Thread(target=cache.loop, name='tibo-rss-worker', daemon=True).start()
    return cache
