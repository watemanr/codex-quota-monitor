import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from detection import normalize, classify, reset_summary
from analytics import register_analytics
from browser_auth import register_browser_auth
from account_management import register_account_management, DeletedAccount
from bark import register_bark
from tibo import register_tibo
from insights import collector_status, forecasts

BASE = Path(__file__).resolve().parent
LABELS = {'baseline': '建立基线', 'scheduled_reset': '计划内重置', 'scheduled_rollover': '计划周期切换',
          'unexpected_reset': '疑似突发重置', 'uncertain': '待确认', 'schedule_change': '重置时间变化',
          'measurement_adjustment': '小幅校正', 'configuration_change': '配置变化'}
# Retain old idle-window records for audit, but keep them off the dashboard.
VISIBLE_EVENT_SQL = "NOT (kind='schedule_change' AND before_used=0 AND after_used=0)"


def create_app(config=None):
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    app.config.update(DB_PATH=os.environ.get('DB_PATH', '/data/quota.sqlite3'),
                      UPLOAD_TOKEN=os.environ.get('UPLOAD_TOKEN', ''),
                      READ_TOKEN=os.environ.get('READ_TOKEN', ''),
                      RESET_TOLERANCE_SECONDS=int(os.environ.get('RESET_TOLERANCE_SECONDS', '300')),
                      MINIMUM_DROP_PP=float(os.environ.get('MINIMUM_DROP_PP', '2')),
                      WEB_PASSWORD=os.environ.get('WEB_PASSWORD',''),
                      WEB_PASSWORD_HASH=os.environ.get('WEB_PASSWORD_HASH',''),
                      WEB_SESSION_TTL_SECONDS=86400,
                      MAX_CONTENT_LENGTH=262144)
    if config:
        app.config.update(config)
    if min(len(app.config['UPLOAD_TOKEN']), len(app.config['READ_TOKEN'])) < 24:
        raise RuntimeError('UPLOAD_TOKEN 和 READ_TOKEN 必须分别配置为至少 24 字符的随机密钥')
    Path(app.config['DB_PATH']).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db():
        conn = sqlite3.connect(app.config['DB_PATH'], timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with db() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS reports (
          id INTEGER PRIMARY KEY, account TEXT NOT NULL, report_key TEXT NOT NULL,
          digest TEXT NOT NULL, observed_at REAL NOT NULL, received_at REAL NOT NULL,
          source TEXT NOT NULL, plan TEXT, UNIQUE(account, report_key));
        CREATE TABLE IF NOT EXISTS samples (
          id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, account TEXT NOT NULL,
          window_id TEXT NOT NULL, label TEXT NOT NULL, window_minutes REAL NOT NULL,
          observed_at REAL NOT NULL, used_percent REAL NOT NULL, resets_at REAL,
          in_order INTEGER NOT NULL, UNIQUE(account, window_id, observed_at));
        CREATE INDEX IF NOT EXISTS samples_latest ON samples(account, window_id, observed_at DESC);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL, account TEXT NOT NULL,
          window_id TEXT NOT NULL, label TEXT NOT NULL, observed_at REAL NOT NULL,
          interval_start REAL, expected_reset REAL, next_reset REAL,
          before_used REAL, after_used REAL NOT NULL, kind TEXT NOT NULL,
          confidence TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_account_time ON events(account, observed_at DESC);
        CREATE INDEX IF NOT EXISTS events_window_kind ON events(account,window_id,kind,observed_at DESC);
        CREATE INDEX IF NOT EXISTS events_time ON events(observed_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS reports_account_time ON reports(account, observed_at DESC);
        ''')

    browser_authenticated,browser_write=register_browser_auth(app,db)

    def auth(role):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                header = request.headers.get('Authorization', '')
                token = header[7:] if header.startswith('Bearer ') else ''
                keys = [app.config['UPLOAD_TOKEN']] if role == 'upload' else [app.config['READ_TOKEN'], app.config['UPLOAD_TOKEN']]
                bearer_ok=bool(token) and any(hmac.compare_digest(token.encode(), k.encode()) for k in keys)
                session_ok=not header and role=='read' and browser_authenticated()
                if not bearer_ok and not session_ok:
                    return jsonify(error='请先登录网页，或提供有效的访问密钥' if role=='read' else '上传密钥无效或未提供'), 401
                return fn(*args, **kwargs)
            return wrapped
        return decorator

    resolve_account=register_account_management(app,db,browser_authenticated,browser_write)
    bark=register_bark(app,db,browser_authenticated,browser_write)
    register_analytics(app, db, auth, resolve_account)
    app.extensions['tibo_cache'] = register_tibo(app, db, auth, bark)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') else 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def errors(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        app.logger.exception('Request failed')
        return jsonify(error='服务暂时不可用，请稍后重试'), 500

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/healthz')
    def health():
        with db() as conn:
            conn.execute('SELECT 1').fetchone()
        return jsonify(status='ok', service='codex-quota-monitor')

    @app.post('/api/usage')
    @auth('upload')
    def ingest():
        now = time.time()
        try:
            data = normalize(request.get_json(), now)
        except ValueError as error:
            return jsonify(error=str(error)), 422
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = data['report_id'] or digest
        outcomes = []
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                data['account'] = resolve_account(conn, data['account'])
            except DeletedAccount:
                return jsonify(error='账号已删除；请在账号管理中允许重新使用该名称，或更改采集器账号名'), 410
            old = conn.execute('SELECT id,digest FROM reports WHERE account=? AND report_key=?', (data['account'], key)).fetchone()
            if old:
                if old['digest'] != digest:
                    return jsonify(error='相同 report_id 对应不同内容；重试必须保留原始采样时间及数据'), 409
                return jsonify(status='duplicate', report_id=old['id'], results=[])
            # Validate the entire report before any write, so conflicts are atomic.
            for w in data['windows']:
                old_sample = conn.execute('SELECT * FROM samples WHERE account=? AND window_id=? AND observed_at=?',
                                          (data['account'], w['window_id'], data['observed_at'])).fetchone()
                if old_sample and any(old_sample[k] != w[k] for k in ('used_percent', 'resets_at', 'window_minutes')):
                    return jsonify(error='同一窗口和采样时间已存在不同数据；请核对 observed_at'), 409
            rid = conn.execute('INSERT INTO reports(account,report_key,digest,observed_at,received_at,source,plan) VALUES(?,?,?,?,?,?,?)',
                               (data['account'], key, digest, data['observed_at'], now, data['source'], data['plan'])).lastrowid
            for w in data['windows']:
                old_sample = conn.execute('SELECT id FROM samples WHERE account=? AND window_id=? AND observed_at=?',
                                          (data['account'], w['window_id'], data['observed_at'])).fetchone()
                if old_sample:
                    outcomes.append(dict(window_id=w['window_id'], status='duplicate'))
                    continue
                previous = conn.execute('SELECT * FROM samples WHERE account=? AND window_id=? AND in_order=1 ORDER BY observed_at DESC LIMIT 1',
                                        (data['account'], w['window_id'])).fetchone()
                previous = dict(previous) if previous else None
                in_order = previous is None or data['observed_at'] > previous['observed_at']
                current = dict(w, observed_at=data['observed_at'])
                sid = conn.execute('INSERT INTO samples(report_id,account,window_id,label,window_minutes,observed_at,used_percent,resets_at,in_order) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (rid, data['account'], w['window_id'], w['label'], w['window_minutes'], data['observed_at'], w['used_percent'], w['resets_at'], int(in_order))).lastrowid
                event = classify(previous, current, app.config['RESET_TOLERANCE_SECONDS'], app.config['MINIMUM_DROP_PP']) if in_order else None
                if event:
                    conn.execute('INSERT INTO events(sample_id,account,window_id,label,observed_at,interval_start,expected_reset,next_reset,before_used,after_used,kind,confidence,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (sid, data['account'], w['window_id'], w['label'], data['observed_at'], previous['observed_at'] if previous else None,
                                  previous['resets_at'] if previous else None, w['resets_at'], previous['used_percent'] if previous else None,
                                  w['used_percent'], event['kind'], event['confidence'], event['reason']))
                bark.on_sample(conn, data['account'], previous, current, event, sid)
                outcomes.append(dict(window_id=w['window_id'], status='accepted' if in_order else 'out_of_order', event=event))
        return jsonify(status='accepted', account=data['account'], report_id=rid, received_at=now, results=outcomes), 201

    @app.get('/api/accounts')
    @auth('read')
    def accounts():
        with db() as conn:
            rows = conn.execute('SELECT account, MAX(observed_at) AS last_observed, MAX(received_at) AS last_received FROM (SELECT account,observed_at,received_at FROM reports UNION ALL SELECT account,observed_at,received_at FROM analytics_state) GROUP BY account ORDER BY last_observed DESC').fetchall()
        return jsonify(accounts=[dict(r) for r in rows])

    def last_resets(conn, account=None):
        # Deliberately independent of the selected chart range.
        where = 'AND e.account=?' if account is not None else ''
        rows = conn.execute("""SELECT * FROM (
            SELECT e.*,s.window_minutes,
              ROW_NUMBER() OVER (PARTITION BY e.account,e.window_id ORDER BY e.observed_at DESC,e.id DESC) AS rn
            FROM events e JOIN samples s ON s.id=e.sample_id
            WHERE e.kind IN ('scheduled_reset','scheduled_rollover','unexpected_reset') """+where+"""
        ) WHERE rn=1""", (account,) if account is not None else ()).fetchall()
        return {(r['account'],r['window_id']):reset_summary(dict(r),r['window_minutes']) for r in rows}

    @app.get('/api/reset-status')
    @auth('read')
    def reset_status():
        account=request.args.get('account','default')
        with db() as conn:
            try:
                account=resolve_account(conn,account)
            except DeletedAccount:
                return jsonify(error='账号已删除'), 410
            result=last_resets(conn,account)
        return jsonify(account=account,last_resets={wid:value for (acct,wid),value in result.items()})

    @app.get('/api/dashboard')
    @auth('read')
    def dashboard():
        account = request.args.get('account', 'default')
        try:
            days = max(1, min(90, int(request.args.get('days', '7'))))
        except ValueError:
            return jsonify(error='days 必须是整数'), 422
        now, cutoff = time.time(), time.time() - days * 86400
        with db() as conn:
            try:
                account=resolve_account(conn,account)
            except DeletedAccount:
                return jsonify(error='账号已删除'), 410
            latest = [dict(r) for r in conn.execute('''SELECT s.* FROM samples s JOIN
              (SELECT window_id, MAX(observed_at) AS t FROM samples WHERE account=? AND in_order=1 GROUP BY window_id) x
              ON s.window_id=x.window_id AND s.observed_at=x.t WHERE s.account=? AND s.in_order=1 ORDER BY s.window_minutes,s.window_id''', (account, account))]
            resets=last_resets(conn,account)
            for row in latest:
                row['last_reset']=resets.get((account,row['window_id']))
                points = conn.execute('SELECT observed_at,used_percent,resets_at FROM samples WHERE account=? AND window_id=? AND in_order=1 AND observed_at>=? ORDER BY observed_at DESC LIMIT 1500',
                                      (account, row['window_id'], cutoff)).fetchall()
                row['history'] = [dict(p) for p in reversed(points)]
                row['history_limited'] = len(points) == 1500
                row['remaining_percent'] = 100 - row['used_percent']
                row['stale'] = now - row['observed_at'] > 1800
            events = [dict(r) for r in conn.execute(f'SELECT * FROM events WHERE account=? AND observed_at>=? AND {VISIBLE_EVENT_SQL} ORDER BY observed_at DESC,id DESC LIMIT 200', (account, cutoff))]
            timeline_events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE account=? AND observed_at>=? AND kind IN ('scheduled_reset','unexpected_reset','uncertain') ORDER BY observed_at DESC,id DESC LIMIT 80", (account, cutoff))]
            counts = {r['kind']: r['n'] for r in conn.execute(f'SELECT kind,COUNT(*) AS n FROM events WHERE account=? AND observed_at>=? AND {VISIBLE_EVENT_SQL} GROUP BY kind', (account, cutoff))}
            report = conn.execute('SELECT observed_at,received_at,source,plan FROM reports WHERE account=? ORDER BY observed_at DESC,received_at DESC LIMIT 1', (account,)).fetchone()
            collector_status_snapshot = collector_status(conn, now, account)
        return jsonify(account=account, now=now, days=days, latest=latest, events=events, timeline_events=timeline_events, counts=counts,
                       collectors=collector_status_snapshot,
                       labels=LABELS, last_report=dict(report) if report else None,
                       rules=dict(tolerance_seconds=app.config['RESET_TOLERANCE_SECONDS'], minimum_drop_pp=app.config['MINIMUM_DROP_PP']))

    @app.get('/api/overview')
    @auth('read')
    def overview():
        try:
            days = max(1, min(90, int(request.args.get('days', '7'))))
        except ValueError:
            return jsonify(error='days 必须是整数'), 422
        now = time.time()
        cutoff = now - days * 86400
        with db() as conn:
            conn.execute('BEGIN')
            reports = conn.execute('''SELECT account, observed_at, received_at, source, plan FROM
                (SELECT account, observed_at, received_at, source, plan,
                 ROW_NUMBER() OVER (PARTITION BY account ORDER BY observed_at DESC, received_at DESC, id DESC) AS rn
                 FROM (SELECT id,account,observed_at,received_at,source,plan FROM reports UNION ALL SELECT 0 AS id,account,observed_at,received_at,source,NULL AS plan FROM analytics_state)) WHERE rn=1 ORDER BY account COLLATE NOCASE''').fetchall()
            grouped = {r['account']: dict(account=r['account'], last_report=dict(r), latest=[], counts={}) for r in reports}
            latest = conn.execute('''SELECT s.* FROM samples s JOIN
                (SELECT account, window_id, MAX(observed_at) AS t FROM samples WHERE in_order=1 GROUP BY account,window_id) x
                ON s.account=x.account AND s.window_id=x.window_id AND s.observed_at=x.t
                WHERE s.in_order=1 ORDER BY s.window_minutes,s.window_id''').fetchall()
            resets=last_resets(conn)
            for row in latest:
                w = dict(row)
                w['last_reset']=resets.get((w['account'],w['window_id']))
                w['remaining_percent'] = 100 - w['used_percent']
                w['stale'] = now - w['observed_at'] > 1800
                grouped[w['account']]['latest'].append(w)
            counts = {}
            for row in conn.execute(f'SELECT account,kind,COUNT(*) AS n FROM events WHERE observed_at>=? AND {VISIBLE_EVENT_SQL} GROUP BY account,kind', (cutoff,)):
                grouped[row['account']]['counts'][row['kind']] = row['n']
                counts[row['kind']] = counts.get(row['kind'], 0) + row['n']
            events = [dict(r) for r in conn.execute(f'SELECT * FROM events WHERE observed_at>=? AND {VISIBLE_EVENT_SQL} ORDER BY observed_at DESC,id DESC LIMIT 200', (cutoff,))]
            timeline_events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE observed_at>=? AND kind IN ('scheduled_reset','unexpected_reset','uncertain') ORDER BY observed_at DESC,id DESC LIMIT 80", (cutoff,))]
            collector_status_snapshot = collector_status(conn, now)
        return jsonify(accounts=list(grouped.values()), now=now, days=days, events=events, timeline_events=timeline_events, counts=counts,
                       collectors=collector_status_snapshot,
                       labels=LABELS, rules=dict(tolerance_seconds=app.config['RESET_TOLERANCE_SECONDS'], minimum_drop_pp=app.config['MINIMUM_DROP_PP']))

    @app.get('/api/forecast')
    @auth('read')
    def quota_forecast():
        now = time.time()
        account = request.args.get('account') or None
        with db() as conn:
            if account is not None:
                try:
                    account = resolve_account(conn, account)
                except DeletedAccount:
                    return jsonify(error='账号已删除'), 410
            result = forecasts(conn, now, account)
        return jsonify(now=now, forecasts=result,
                       method='最近连续采样的已用百分比变化；不代表平台承诺或精确耗尽时间')

    @app.get('/openapi.json')
    def openapi():
        return app.send_static_file('openapi.json')

    return app
