"""Account management for quota and analytics history.

All mutations use browser sessions and CSRF; collector Bearer keys cannot erase data.
"""
import json
import time

from flask import jsonify, request

from detection import short_text


class DeletedAccount(Exception):
    pass


def resolve_account(conn, name):
    row = conn.execute('SELECT canonical FROM account_routes WHERE alias=?', (name,)).fetchone()
    if row is None:
        return name
    if row['canonical'] is None:
        raise DeletedAccount(name)
    return row['canonical']


def combine_sections(destination, incoming):
    """Merge snapshots without adding percentages from duplicate account labels."""
    result = dict(destination)
    for section_name, source in incoming.items():
        target = result.get(section_name)
        if target is None:
            result[section_name] = source
            continue
        source_time = source.get('data_as_of') or source.get('collected_at') or 0
        target_time = target.get('data_as_of') or target.get('collected_at') or 0
        newer, older = (source, target) if source_time > target_time else (target, source)
        if section_name == 'daily' and source.get('units') == target.get('units'):
            by_day = {row['date']: row for row in older.get('data', [])}
            by_day.update({row['date']: row for row in newer.get('data', [])})
            result[section_name] = dict(newer, data=sorted(by_day.values(),
                                       key=lambda row: row['date'])[-366:])
        elif section_name == 'periods':
            # Two labels can report the same provider cycle under different IDs.
            by_start = {(row['window_minutes'], row['starts_at']): row
                        for row in older.get('periods', [])}
            by_start.update({(row['window_minutes'], row['starts_at']): row
                             for row in newer.get('periods', [])})
            result[section_name] = dict(newer, periods=sorted(by_start.values(),
                                       key=lambda row: row['starts_at'], reverse=True)[:500])
        elif section_name == 'chats':
            by_thread = {row['thread_id']: row for row in older.get('rows', [])}
            by_thread.update({row['thread_id']: row for row in newer.get('rows', [])})
            rows = list(by_thread.values())
            result[section_name] = dict(newer, rows=rows,
                                       is_truncated=(newer.get('is_truncated') or older.get('is_truncated')),
                                       scope='合并账号的最近 30 天活跃聊天；累计消耗含已知子任务')
        else:
            result[section_name] = newer
    return result


def register_account_management(app, db, browser_authenticated, browser_write):
    with db() as conn:
        conn.executescript("""CREATE TABLE IF NOT EXISTS account_routes (
            alias TEXT PRIMARY KEY, canonical TEXT, changed_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS account_routes_target ON account_routes(canonical);""")

    def exists(conn, name):
        return conn.execute("""SELECT 1 FROM (
            SELECT account FROM reports WHERE account=?
            UNION ALL SELECT account FROM analytics_state WHERE account=?
        ) LIMIT 1""", (name, name)).fetchone() is not None

    def names(conn):
        return [row['account'] for row in conn.execute("""SELECT account FROM reports
            UNION SELECT account FROM analytics_state ORDER BY account COLLATE NOCASE""")]

    def validate_name(value, field):
        try:
            name = short_text(value, field)
        except ValueError as error:
            return None, str(error)
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            return None, f'{field} 不能包含控制字符'
        return name, None

    def body():
        if not request.is_json:
            return None, (jsonify(error='请提交 JSON'), 415)
        if request.content_length is not None and request.content_length > 4096:
            return None, (jsonify(error='请求过大'), 413)
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            return None, (jsonify(error='请求格式不正确'), 422)
        return value, None

    def canonical_exists(conn, name):
        return exists(conn, name) and conn.execute(
            'SELECT 1 FROM account_routes WHERE alias=?', (name,)).fetchone() is None

    def summary(conn, name):
        return dict(account=name,
                    reports=conn.execute('SELECT COUNT(*) FROM reports WHERE account=?', (name,)).fetchone()[0],
                    samples=conn.execute('SELECT COUNT(*) FROM samples WHERE account=?', (name,)).fetchone()[0],
                    events=conn.execute('SELECT COUNT(*) FROM events WHERE account=?', (name,)).fetchone()[0],
                    analytics_reports=conn.execute('SELECT COUNT(*) FROM analytics_reports WHERE account=?',
                                                   (name,)).fetchone()[0],
                    aliases=[row['alias'] for row in conn.execute(
                        'SELECT alias FROM account_routes WHERE canonical=? ORDER BY alias', (name,))])

    @app.get('/api/accounts/manage')
    def account_management_list():
        if not browser_authenticated():
            return jsonify(error='请先登录网页'), 401
        with db() as conn:
            accounts = [summary(conn, name) for name in names(conn)]
            deleted = [row['alias'] for row in conn.execute(
                'SELECT alias FROM account_routes WHERE canonical IS NULL ORDER BY alias')]
        return jsonify(accounts=accounts, deleted_names=deleted)

    def conflict(conn, source, target):
        if all(conn.execute('SELECT 1 FROM bark_settings WHERE account=?', (name,)).fetchone()
               for name in (source, target)):
            return 'Bark 推送配置'
        states = [conn.execute('SELECT data_json FROM analytics_state WHERE account=?',
                               (name,)).fetchone() for name in (source, target)]
        if all(states):
            sections = [json.loads(state['data_json']) for state in states]
            daily = [section.get('daily') for section in sections]
            if all(daily) and daily[0]['units'] != daily[1]['units']:
                return '分析日统计单位'
        checks = (
            ("""SELECT 1 FROM reports a JOIN reports b ON b.account=?
                AND b.observed_at=a.observed_at WHERE a.account=?
                AND a.reset_card_count IS NOT NULL AND b.reset_card_count IS NOT NULL
                AND a.reset_card_count!=b.reset_card_count LIMIT 1""", '同一采样时间的重置卡数量'),
            ("""SELECT 1 FROM reports a JOIN reports b ON b.account=?
                AND b.report_key=a.report_key WHERE a.account=? LIMIT 1""", '上报 ID'),
            ("""SELECT 1 FROM samples a JOIN samples b ON b.account=?
                AND b.window_id=a.window_id AND b.observed_at=a.observed_at
                WHERE a.account=? LIMIT 1""", '同一窗口的采样时间'),
            ("""SELECT 1 FROM analytics_reports a JOIN analytics_reports b ON b.account=?
                AND b.report_id=a.report_id WHERE a.account=? LIMIT 1""", '分析上报 ID'),
        )
        for sql, label in checks:
            if conn.execute(sql, (target, source)).fetchone():
                return label
        return None

    def move(conn, source, target):
        source_state = conn.execute('SELECT * FROM analytics_state WHERE account=?', (source,)).fetchone()
        target_state = conn.execute('SELECT * FROM analytics_state WHERE account=?', (target,)).fetchone()
        if source_state and target_state:
            fresh = source_state if source_state['observed_at'] > target_state['observed_at'] else target_state
            merged = combine_sections(json.loads(target_state['data_json']),
                                      json.loads(source_state['data_json']))
            conn.execute('UPDATE analytics_state SET observed_at=?,received_at=?,source=?,data_json=? WHERE account=?',
                         (max(source_state['observed_at'], target_state['observed_at']),
                          max(source_state['received_at'], target_state['received_at']),
                          fresh['source'], json.dumps(merged, ensure_ascii=False), target))
            conn.execute('DELETE FROM analytics_state WHERE account=?', (source,))
        elif source_state:
            conn.execute('UPDATE analytics_state SET account=? WHERE account=?', (target, source))
        for table in ('reports', 'samples', 'events', 'analytics_reports', 'bark_outbox', 'bark_low_state'):
            conn.execute(f'UPDATE {table} SET account=? WHERE account=?', (target, source))
        conn.execute('UPDATE bark_settings SET account=? WHERE account=?', (target, source))
        snapshot_action = 'REPLACE' if source_state and (
            not target_state or source_state['observed_at'] > target_state['observed_at']) else 'IGNORE'
        conn.execute(f"""INSERT OR {snapshot_action} INTO analytics_period_snapshots
            (account,period_id,snapshot_at,starts_at,ends_at,data_json)
            SELECT ?,period_id,snapshot_at,starts_at,ends_at,data_json
            FROM analytics_period_snapshots WHERE account=?""", (target, source))
        conn.execute('DELETE FROM analytics_period_snapshots WHERE account=?', (source,))
        conn.execute('UPDATE account_routes SET canonical=?,changed_at=? WHERE canonical=?',
                     (target, time.time(), source))
        conn.execute("""INSERT INTO account_routes(alias,canonical,changed_at) VALUES(?,?,?)
            ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical,changed_at=excluded.changed_at""",
                     (source, target, time.time()))

    @app.post('/api/accounts/rename')
    def rename():
        error = browser_write()
        if error:
            return error
        data, error = body()
        if error:
            return error
        source, bad = validate_name(data.get('account'), '原账号')
        if bad:
            return jsonify(error=bad), 422
        target, bad = validate_name(data.get('new_name'), '新账号名')
        if bad:
            return jsonify(error=bad), 422
        if source == target:
            return jsonify(error='新账号名与原账号相同'), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not canonical_exists(conn, source):
                return jsonify(error='原账号不存在或已被改名'), 404
            if exists(conn, target):
                return jsonify(error='新名字已有数据；请选择“合并账号”'), 409
            route = conn.execute('SELECT canonical FROM account_routes WHERE alias=?',
                                 (target,)).fetchone()
            if route is not None:
                if route['canonical'] == source:
                    # Reclaiming this account's own old name keeps every other alias.
                    conn.execute('DELETE FROM account_routes WHERE alias=?', (target,))
                elif route['canonical'] is None:
                    return jsonify(error='这个名字已删除；请先在管理页允许重新使用'), 409
                else:
                    return jsonify(error='这个名字是另一账号的旧名称；请先在管理页解除占用'), 409
            moved = summary(conn, source)
            move(conn, source, target)
        return jsonify(status='renamed', account=target, moved=moved)

    @app.post('/api/accounts/merge')
    def merge():
        error = browser_write()
        if error:
            return error
        data, error = body()
        if error:
            return error
        source, bad = validate_name(data.get('source'), '要并入的账号')
        if bad:
            return jsonify(error=bad), 422
        target, bad = validate_name(data.get('target'), '保留的账号')
        if bad:
            return jsonify(error=bad), 422
        if source == target:
            return jsonify(error='请选择两个不同账号'), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not canonical_exists(conn, source) or not canonical_exists(conn, target):
                return jsonify(error='账号不存在或已被改名，请刷新列表'), 404
            duplicate = conflict(conn, source, target)
            if duplicate:
                return jsonify(error=f'两账号存在冲突的{duplicate}，为避免丢失记录，合并未执行'), 409
            moved = summary(conn, source)
            move(conn, source, target)
        return jsonify(status='merged', account=target, moved=moved)

    @app.post('/api/accounts/delete')
    def delete():
        error = browser_write()
        if error:
            return error
        data, error = body()
        if error:
            return error
        name, bad = validate_name(data.get('account'), '账号')
        if bad:
            return jsonify(error=bad), 422
        if data.get('confirm_name') != name:
            return jsonify(error='请完整输入账号名确认删除'), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not canonical_exists(conn, name):
                return jsonify(error='账号不存在或已被改名'), 404
            removed = summary(conn, name)
            for table in ('events', 'samples', 'reports', 'analytics_period_snapshots',
                          'analytics_reports', 'analytics_state', 'bark_outbox', 'bark_low_state', 'bark_settings'):
                conn.execute(f'DELETE FROM {table} WHERE account=?', (name,))
            now = time.time()
            conn.execute('UPDATE account_routes SET canonical=NULL,changed_at=? WHERE canonical=?',
                         (now, name))
            conn.execute("""INSERT INTO account_routes(alias,canonical,changed_at) VALUES(?,NULL,?)
                ON CONFLICT(alias) DO UPDATE SET canonical=NULL,changed_at=excluded.changed_at""",
                         (name, now))
        return jsonify(status='deleted', account=name, removed=removed)

    @app.post('/api/accounts/release-alias')
    def release_alias():
        error = browser_write()
        if error:
            return error
        data, error = body()
        if error:
            return error
        owner, bad = validate_name(data.get('account'), '当前账号')
        if bad:
            return jsonify(error=bad), 422
        alias, bad = validate_name(data.get('alias'), '旧名称')
        if bad:
            return jsonify(error=bad), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not canonical_exists(conn, owner):
                return jsonify(error='当前账号不存在或已被改名，请刷新列表'), 404
            row = conn.execute('SELECT canonical FROM account_routes WHERE alias=?',
                               (alias,)).fetchone()
            if row is None:
                return jsonify(error='这个旧名称已解除占用，请刷新列表'), 404
            if row['canonical'] != owner:
                return jsonify(error='旧名称已不属于这个账号，请刷新列表'), 409
            if exists(conn, alias):
                return jsonify(error='这个名称已有独立数据，无法解除占用'), 409
            conn.execute('DELETE FROM account_routes WHERE alias=? AND canonical=?',
                         (alias, owner))
        return jsonify(status='released', account=owner, alias=alias)

    @app.post('/api/accounts/unblock')
    def unblock():
        error = browser_write()
        if error:
            return error
        data, error = body()
        if error:
            return error
        name, bad = validate_name(data.get('account'), '账号')
        if bad:
            return jsonify(error=bad), 422
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT canonical FROM account_routes WHERE alias=?', (name,)).fetchone()
            if row is None or row['canonical'] is not None:
                return jsonify(error='这个名字未被删除或仍在使用'), 404
            conn.execute('DELETE FROM account_routes WHERE alias=?', (name,))
        return jsonify(status='unblocked', account=name)

    return resolve_account

