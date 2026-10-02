"""Per-account Bark notifications with a durable SQLite outbox."""
import json
import hashlib
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import jsonify, request


RESET_EVENTS = {'scheduled_reset', 'unexpected_reset'}
DEFAULTS = dict(
    server_url='', key_configured=False, group_name='Codex额度',
    enabled=False, notify_5h_reset=True, notify_weekly_reset=True,
    notify_5h_low=True, notify_weekly_low=True, low_threshold=5,
    notify_tibo_posts=False, report_interval_minutes=0, next_report_at=None,
)
HK = timezone(timedelta(hours=8))


def format_time(value):
    return datetime.fromtimestamp(value, HK).strftime('%m/%d %H:%M')


def validate_server_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError('请输入 Bark 服务器地址')
    value = value.strip().rstrip('/')
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError('Bark 地址不能包含空格或控制字符')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('Bark 地址格式不正确') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Bark 地址必须是 http:// 或 https:// 开头的服务器地址') from None
    if parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('只填写 Bark 服务器地址，不要包含 key、/push、标题或查询参数')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Bark 端口不正确')
    return value


def validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', value):
        raise ValueError('Bark key 必须是 8–256 位字母、数字、- 或 _')
    return value


def send_bark(setting, title, body):
    """POST JSON keeps the device key out of URL/access logs."""
    payload = json.dumps(dict(
        device_key=setting['device_key'], title=title, body=body,
        group=setting['group_name'], ttl=600,
    ), ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        setting['server_url'].rstrip('/') + '/push', data=payload,
        headers={'Content-Type': 'application/json; charset=utf-8'},
        method='POST')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=5) as response:
            result = json.loads(response.read(4096).decode('utf-8'))
            if not isinstance(result, dict) or result.get('code') not in (200, 0):
                raise RuntimeError('Bark 返回失败状态')
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Bark HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('Bark 连接失败或超时') from None


class BarkService:
    def __init__(self, app, db):
        self.app, self.db = app, db
        with db() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS bark_settings (
              account TEXT PRIMARY KEY, server_url TEXT NOT NULL,
              device_key TEXT NOT NULL, group_name TEXT NOT NULL,
              enabled INTEGER NOT NULL, notify_5h_reset INTEGER NOT NULL,
              notify_weekly_reset INTEGER NOT NULL, notify_5h_low INTEGER NOT NULL,
              notify_weekly_low INTEGER NOT NULL, low_threshold REAL NOT NULL,
              report_interval_minutes INTEGER NOT NULL,
              next_report_at REAL, updated_at REAL NOT NULL,
              notify_tibo_posts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bark_low_state (
              account TEXT NOT NULL, window_id TEXT NOT NULL, below INTEGER NOT NULL,
              PRIMARY KEY(account,window_id));
            CREATE TABLE IF NOT EXISTS bark_outbox (
              id INTEGER PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL,
              created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              next_attempt_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              lease_until REAL, sent_at REAL, last_error TEXT);
            CREATE INDEX IF NOT EXISTS bark_outbox_due ON bark_outbox(status,next_attempt_at);
            CREATE INDEX IF NOT EXISTS bark_outbox_account ON bark_outbox(account,created_at DESC);
            CREATE TABLE IF NOT EXISTS bark_tibo_recipients (
              endpoint_hash TEXT PRIMARY KEY, initialized_at REAL NOT NULL);
            """)
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(bark_settings)')}
            if 'notify_tibo_posts' not in columns:
                conn.execute('ALTER TABLE bark_settings ADD COLUMN notify_tibo_posts INTEGER NOT NULL DEFAULT 0')

    def enqueue(self, conn, account, kind, dedupe_key, title, body, now=None):
        now = time.time() if now is None else now
        conn.execute("""INSERT OR IGNORE INTO bark_outbox
            (account,kind,dedupe_key,title,body,created_at,next_attempt_at)
            VALUES(?,?,?,?,?,?,?)""",
            (account, kind, dedupe_key, title, body, now, now))

    def on_sample(self, conn, account, previous, current, event, sample_id):
        setting = conn.execute('SELECT * FROM bark_settings WHERE account=?', (account,)).fetchone()
        if not setting or not setting['enabled']:
            return
        minutes = current['window_minutes']
        reset = bool(event and event['kind'] in RESET_EVENTS)
        if reset:
            field = 'notify_5h_reset' if minutes == 300 else 'notify_weekly_reset' if minutes == 10080 else None
            if field and setting[field]:
                label = '5h' if minutes == 300 else 'weekly'
                kind = event['kind']
                title = f'Codex {label} 额度重置'
                body = (f"账号：{account}\n剩余：{100-current['used_percent']:g}%\n"
                        f"检测：{format_time(current['observed_at'])}\n"
                        f"{'符合计划时间' if kind == 'scheduled_reset' else '疑似提前重置；请核对'}")
                self.enqueue(conn, account, 'reset', f'sample:{sample_id}:reset', title, body)
        low_field = 'notify_5h_low' if minutes == 300 else 'notify_weekly_low' if minutes == 10080 else None
        if low_field and setting[low_field]:
            remaining = 100 - current['used_percent']
            row = conn.execute('SELECT below FROM bark_low_state WHERE account=? AND window_id=?',
                               (account, current['window_id'])).fetchone()
            was_below = bool(row['below']) if row else False
            # Two-point hysteresis avoids repeated alerts from small rounding jitter.
            below = (remaining <= setting['low_threshold'] or
                     was_below and remaining < setting['low_threshold'] + 2)
            if below and not reset and not was_below:
                label = '5h' if minutes == 300 else 'weekly'
                title = f'Codex {label} 额度低'
                body = (f'账号：{account}\n剩余：{remaining:g}%（阈值 {setting["low_threshold"]:g}%）\n'
                        f'采样：{format_time(current["observed_at"])}')
                self.enqueue(conn, account, 'low', f'sample:{sample_id}:low', title, body)
            conn.execute('INSERT INTO bark_low_state VALUES(?,?,?) ON CONFLICT(account,window_id) '
                         'DO UPDATE SET below=excluded.below',
                         (account, current['window_id'], int(below)))

    def on_tibo_posts(self, conn, new_posts, recent_posts, now):
        """Queue relevant public posts once per Bark endpoint, with a one-post bootstrap."""
        recipients = conn.execute('''SELECT account,server_url,device_key FROM bark_settings
            WHERE enabled=1 AND notify_tibo_posts=1 ORDER BY account''').fetchall()
        seen = set()
        for recipient in recipients:
            endpoint = hashlib.sha256((recipient['server_url'] + '\0' + recipient['device_key']).encode()).hexdigest()
            if endpoint in seen:
                continue
            seen.add(endpoint)
            initialized = conn.execute('SELECT 1 FROM bark_tibo_recipients WHERE endpoint_hash=?',
                                       (endpoint,)).fetchone()
            if initialized:
                candidates = [post for post in new_posts if now - 86400 <= post['published_at'] <= now + 300]
            else:
                # First opt-in gets only the newest timely post, never a backlog.
                candidates = [post for post in recent_posts if now - 21600 <= post['published_at'] <= now + 300][:1]
                conn.execute('INSERT INTO bark_tibo_recipients VALUES(?,?)', (endpoint, now))
            for post in candidates:
                excerpt = post['text'][:700].rstrip()
                if len(post['text']) > 700:
                    excerpt += '…'
                body = f"发布时间：{format_time(post['published_at'])}（北京时间）\n{excerpt}\n{post['url']}"
                self.enqueue(conn, recipient['account'], 'tibo', f"tibo:{post['id']}:{endpoint}",
                             'Tibo 发布额度相关动态', body, now)

    def schedule_reports_once(self, now=None):
        now = time.time() if now is None else now
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            settings = conn.execute("""SELECT * FROM bark_settings WHERE enabled=1
                AND report_interval_minutes>0 AND next_report_at<=?""", (now,)).fetchall()
            for setting in settings:
                account = setting['account']
                windows = conn.execute("""SELECT window_minutes,used_percent,observed_at FROM (
                    SELECT window_minutes,used_percent,observed_at,
                    ROW_NUMBER() OVER (PARTITION BY window_id ORDER BY observed_at DESC,id DESC) AS rn
                    FROM samples WHERE account=? AND in_order=1) WHERE rn=1 ORDER BY window_minutes""",
                    (account,)).fetchall()
                lines = [f'账号：{account}']
                for window in windows[:8]:
                    label = '5h' if window['window_minutes'] == 300 else 'weekly' if window['window_minutes'] == 10080 else f'{window["window_minutes"]:g} 分钟'
                    lines.append(f'{label} 剩余 {100-window["used_percent"]:g}% · 采样 {format_time(window["observed_at"])}')
                if not windows:
                    lines.append('尚无额度采样')
                self.enqueue(conn, account, 'report', f'report:{account}:{setting["next_report_at"]:.0f}',
                             'Codex 额度报告', '\n'.join(lines), now)
                conn.execute('UPDATE bark_settings SET next_report_at=? WHERE account=?',
                             (now + setting['report_interval_minutes'] * 60, account))
        return len(settings)

    def process_once(self, send_func=send_bark, now=None):
        now = time.time() if now is None else now
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            item = conn.execute("""SELECT * FROM bark_outbox WHERE
                (status='pending' AND next_attempt_at<=?) OR
                (status='sending' AND lease_until<=?)
                ORDER BY created_at,id LIMIT 1""", (now, now)).fetchone()
            if not item:
                return False
            conn.execute("UPDATE bark_outbox SET status='sending',attempts=attempts+1,lease_until=? WHERE id=?",
                         (now + 60, item['id']))
            setting = conn.execute('SELECT * FROM bark_settings WHERE account=?', (item['account'],)).fetchone()
            allowed = setting and (setting['enabled'] or item['kind'] == 'test')
            if allowed and item['kind'] == 'tibo':
                allowed = bool(setting['notify_tibo_posts'])
            if not allowed:
                conn.execute("UPDATE bark_outbox SET status='canceled',lease_until=NULL WHERE id=?", (item['id'],))
                return True
            setting = dict(setting)
            item = dict(item)
        error = None
        try:
            send_func(setting, item['title'], item['body'])
        except Exception as exc:
            # Never persist a response body, URL or device key.
            error = str(exc) if isinstance(exc, RuntimeError) else 'Bark 网络请求失败'
        with self.db() as conn:
            if error:
                retry = item['attempts'] + 1 < 5
                conn.execute("""UPDATE bark_outbox SET status=?,last_error=?,lease_until=NULL,
                    next_attempt_at=? WHERE id=?""",
                    ('pending' if retry else 'failed', error,
                     time.time() + min(3600, 30 * 2 ** item['attempts']), item['id']))
            else:
                conn.execute("""UPDATE bark_outbox SET status='sent',sent_at=?,last_error=NULL,
                    lease_until=NULL WHERE id=?""", (time.time(), item['id']))
        return True

    def loop(self):
        while True:
            try:
                self.schedule_reports_once()
                for _ in range(5):
                    if not self.process_once():
                        break
                with self.db() as conn:
                    conn.execute("DELETE FROM bark_outbox WHERE status IN ('sent','failed','canceled') AND created_at<?",
                                 (time.time() - 30 * 86400,))
            except Exception as exc:
                self.app.logger.warning('Bark worker tick failed: %s', type(exc).__name__)
            threading.Event().wait(15)


def register_bark(app, db, browser_authenticated, browser_write):
    service = BarkService(app, db)
    app.extensions['bark'] = service

    def parse_body():
        if not request.is_json:
            return None, (jsonify(error='请提交 JSON'), 415)
        if request.content_length is not None and request.content_length > 4096:
            return None, (jsonify(error='请求过大'), 413)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None, (jsonify(error='请求格式不正确'), 422)
        return data, None

    def account_exists(conn, account):
        return conn.execute("""SELECT 1 FROM (
            SELECT account FROM reports WHERE account=?
            UNION ALL SELECT account FROM analytics_state WHERE account=?
        ) LIMIT 1""", (account, account)).fetchone() is not None

    def public_setting(row):
        if row is None:
            return dict(DEFAULTS)
        return dict(
            server_url=row['server_url'], key_configured=True,
            key_hint='••••' + row['device_key'][-4:],
            group_name=row['group_name'], enabled=bool(row['enabled']),
            notify_5h_reset=bool(row['notify_5h_reset']),
            notify_weekly_reset=bool(row['notify_weekly_reset']),
            notify_5h_low=bool(row['notify_5h_low']),
            notify_weekly_low=bool(row['notify_weekly_low']),
            notify_tibo_posts=bool(row['notify_tibo_posts']),
            low_threshold=row['low_threshold'],
            report_interval_minutes=row['report_interval_minutes'],
            next_report_at=row['next_report_at'])

    @app.get('/api/bark/settings')
    def bark_settings():
        if not browser_authenticated():
            return jsonify(error='请先登录网页'), 401
        with db() as conn:
            accounts = [row['account'] for row in conn.execute(
                'SELECT account FROM reports UNION SELECT account FROM analytics_state ORDER BY account COLLATE NOCASE')]
            settings = {row['account']: public_setting(row) for row in conn.execute('SELECT * FROM bark_settings')}
            recent = {}
            for account in accounts:
                recent[account] = [dict(row) for row in conn.execute("""SELECT kind,created_at,status,sent_at,
                    attempts,last_error FROM bark_outbox WHERE account=? ORDER BY id DESC LIMIT 8""", (account,))]
        return jsonify(accounts=accounts, settings=settings, recent=recent, defaults=DEFAULTS)

    @app.post('/api/bark/settings')
    def save_bark_settings():
        error = browser_write()
        if error:
            return error
        data, error = parse_body()
        if error:
            return error
        account = data.get('account')
        if not isinstance(account, str) or not 1 <= len(account) <= 160:
            return jsonify(error='请选择账号'), 422
        try:
            url = validate_server_url(data.get('server_url'))
            supplied_key = data.get('device_key', '')
            if supplied_key:
                key = validate_key(supplied_key)
            elif supplied_key != '':
                raise ValueError('Bark key 格式不正确')
            group = data.get('group_name', 'Codex额度')
            if not isinstance(group, str) or not 1 <= len(group.strip()) <= 64 or any(ord(c) < 32 for c in group):
                raise ValueError('分组名称必须是 1–64 个字符')
            group = group.strip()
            flags = {}
            for field in ('enabled', 'notify_5h_reset', 'notify_weekly_reset',
                          'notify_5h_low', 'notify_weekly_low'):
                if not isinstance(data.get(field), bool):
                    raise ValueError(f'{field} 必须为开关值')
                flags[field] = int(data[field])
            tibo_flag = data.get('notify_tibo_posts')
            if tibo_flag is not None and not isinstance(tibo_flag, bool):
                raise ValueError('notify_tibo_posts 必须为开关值')
            threshold = data.get('low_threshold')
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 50:
                raise ValueError('低额度阈值必须在 0–50% 之间')
            interval = data.get('report_interval_minutes')
            if isinstance(interval, bool) or not isinstance(interval, int) or interval != 0 and not 5 <= interval <= 10080:
                raise ValueError('报告间隔必须为 0 或 5–10080 分钟')
        except ValueError as exc:
            return jsonify(error=str(exc)), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not account_exists(conn, account):
                return jsonify(error='账号不存在或已改名'), 404
            existing = conn.execute('SELECT * FROM bark_settings WHERE account=?', (account,)).fetchone()
            flags['notify_tibo_posts'] = (int(tibo_flag) if tibo_flag is not None else
                                           existing['notify_tibo_posts'] if existing else 0)
            key = key if supplied_key else existing['device_key'] if existing else None
            if not key:
                return jsonify(error='请填写 Bark key'), 422
            now = time.time()
            same_interval = existing and existing['report_interval_minutes'] == interval
            next_report = (existing['next_report_at'] if same_interval and existing['next_report_at']
                           else now + interval * 60 if interval else None)
            if interval == 0:
                next_report = None
            conn.execute("""INSERT INTO bark_settings
                (account,server_url,device_key,group_name,enabled,notify_5h_reset,
                 notify_weekly_reset,notify_5h_low,notify_weekly_low,low_threshold,
                 report_interval_minutes,next_report_at,updated_at,notify_tibo_posts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account) DO UPDATE SET
                server_url=excluded.server_url,device_key=excluded.device_key,
                group_name=excluded.group_name,enabled=excluded.enabled,
                notify_5h_reset=excluded.notify_5h_reset,
                notify_weekly_reset=excluded.notify_weekly_reset,
                notify_5h_low=excluded.notify_5h_low,
                notify_weekly_low=excluded.notify_weekly_low,
                notify_tibo_posts=excluded.notify_tibo_posts,
                low_threshold=excluded.low_threshold,
                report_interval_minutes=excluded.report_interval_minutes,
                next_report_at=excluded.next_report_at,updated_at=excluded.updated_at""",
                (account, url, key, group, flags['enabled'],
                 flags['notify_5h_reset'], flags['notify_weekly_reset'],
                 flags['notify_5h_low'], flags['notify_weekly_low'],
                 float(threshold), interval, next_report, now, flags['notify_tibo_posts']))
            if not existing or not existing['enabled'] or not flags['enabled'] or (
                    existing['low_threshold'] != threshold or
                    existing['notify_5h_low'] != flags['notify_5h_low'] or
                    existing['notify_weekly_low'] != flags['notify_weekly_low']):
                conn.execute('DELETE FROM bark_low_state WHERE account=?', (account,))
            if not flags['enabled']:
                conn.execute("UPDATE bark_outbox SET status='canceled' WHERE account=? AND status='pending'",
                             (account,))
            elif not flags['notify_tibo_posts']:
                conn.execute("UPDATE bark_outbox SET status='canceled' WHERE account=? AND kind='tibo' AND status='pending'",
                             (account,))
            result = public_setting(conn.execute('SELECT * FROM bark_settings WHERE account=?', (account,)).fetchone())
        return jsonify(account=account, setting=result)

    @app.post('/api/bark/test')
    def test_bark():
        error = browser_write()
        if error:
            return error
        data, error = parse_body()
        if error:
            return error
        account = data.get('account')
        if not isinstance(account, str) or not 1 <= len(account) <= 160:
            return jsonify(error='请选择账号'), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            setting = conn.execute('SELECT 1 FROM bark_settings WHERE account=?', (account,)).fetchone()
            if not setting:
                return jsonify(error='请先保存 Bark 地址和 key'), 404
            recent = conn.execute("""SELECT created_at FROM bark_outbox WHERE account=? AND kind='test'
                ORDER BY id DESC LIMIT 1""", (account,)).fetchone()
            now = time.time()
            if recent and now - recent['created_at'] < 30:
                return jsonify(error='测试推送每 30 秒最多发送一次'), 429
            service.enqueue(conn, account, 'test', f'test:{account}:{now}',
                            'Codex Bark 测试', f'账号：{account}\n连接测试，{format_time(now)}', now)
        return jsonify(status='queued')

    @app.post('/api/bark/remove')
    def remove_bark():
        error = browser_write()
        if error:
            return error
        data, error = parse_body()
        if error:
            return error
        account = data.get('account')
        if not isinstance(account, str) or not 1 <= len(account) <= 160:
            return jsonify(error='请选择账号'), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM bark_settings WHERE account=?', (account,))
            conn.execute('DELETE FROM bark_low_state WHERE account=?', (account,))
            conn.execute("UPDATE bark_outbox SET status='canceled' WHERE account=? AND status IN ('pending','sending')",
                         (account,))
        return jsonify(status='removed')

    if not app.config.get('TESTING') and app.config.get('BARK_WORKER_ENABLED', True):
        threading.Thread(target=service.loop, name='bark-worker', daemon=True).start()
    return service
