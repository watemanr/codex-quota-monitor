"""Read-only analytics snapshots. Never infer task cost from quota deltas."""
import hashlib
import json
import math
import time
from datetime import datetime, timedelta, timezone
from flask import jsonify, request
from detection import short_text, timestamp
from account_management import DeletedAccount

FEATURES = {'user': 'Tasks', 'guardian_review': 'Auto review', 'thread_title': 'Task titles',
            'guardian_classifier': 'Auto review checks', 'memory_consolidation': 'Memory updates',
            'thread_description': 'Task descriptions', 'subagent': 'Subagents', 'unknown': 'Unknown'}
GROUPS = {'feature': 'thread_source', 'model': 'model', 'surface': 'surface', 'trigger': 'turn_trigger'}
RESET_LABELS = {'scheduled_reset': '计划内重置', 'scheduled_rollover': '计划周期切换',
                'unexpected_reset': '疑似突发重置'}


def value(v, nullable=False):
    if v is None and nullable:
        return None
    if isinstance(v, bool):
        raise ValueError('用量不能是布尔值')
    try:
        v = float(v)
        if not math.isfinite(v) or not 0 <= v <= 1e15:
            raise ValueError()
        return v
    except (TypeError, ValueError, OverflowError):
        raise ValueError('用量必须是有限的非负数字或 null') from None


def date(v):
    try:
        return datetime.strptime(v, '%Y-%m-%d').date().isoformat()
    except (ValueError, TypeError):
        raise ValueError('日统计日期必须使用 YYYY-MM-DD') from None


def rows(v, limit=1000):
    if not isinstance(v, list) or len(v) > limit or any(not isinstance(r, dict) for r in v):
        raise ValueError('统计记录必须是对象数组，且不能超过条数上限')
    return v


def add(target, key, v):
    key = short_text(key, '统计分类', 'unknown')
    target[key] = target.get(key, 0) + value(v)


def normalize_analytics(payload, now):
    if not isinstance(payload, dict) or not isinstance(payload.get('analytics'), dict):
        raise ValueError('需要 account、observed_at、report_id 和 analytics 对象')
    account = short_text(payload.get('account'), 'account')
    observed = timestamp(payload.get('observed_at'), 'observed_at')
    if observed > now + 300:
        raise ValueError('采样时间超过服务器时间 5 分钟')
    report_id = short_text(payload.get('report_id'), 'report_id')
    data = payload['analytics']
    result = {}
    if data.get('daily') is not None:
        raw = data['daily']
        if not isinstance(raw, dict):
            raise ValueError('daily 必须是对象')
        units = short_text(raw.get('units'), 'units')
        if units not in ('percent', 'credits', 'tokens'):
            raise ValueError('daily.units 只支持 percent、credits 或 tokens')
        out, seen = [], set()
        for row in rows(raw.get('data'), 366):
            day = date(row.get('date'))
            if day in seen:
                raise ValueError('同一份上报不能包含重复日期')
            seen.add(day)
            dims = {k: {} for k in GROUPS}
            if row.get('attribution') is not None:
                for a in rows(row['attribution']):
                    for group, source_key in GROUPS.items():
                        add(dims[group], a.get(source_key), a.get('value'))
            else:
                dims['feature'] = dims['trigger'] = None
                dims['surface'] = None
                for m in rows(row.get('models', [])):
                    add(dims['model'], m.get('model'), m.get('credits'))
            out.append(dict(date=day, dimensions=dims))
        result['daily'] = dict(units=units, data=out,
                               data_as_of=timestamp(raw.get('data_freshness_ts'), 'data_freshness_ts', optional=True),
                               collected_at=observed)
    if data.get('periods') is not None:
        raw = data['periods']
        if not isinstance(raw, dict):
            raise ValueError('periods 必须是对象')
        out, seen = [], set()
        as_of = timestamp(raw.get('data_as_of'), 'periods.data_as_of', optional=True)
        for p in rows(raw.get('periods'), 500):
            pid = short_text(p.get('id'), 'period.id')
            if pid in seen:
                raise ValueError('套餐周期 ID 重复')
            seen.add(pid)
            minutes = p.get('window_minutes')
            if minutes not in (300, 10080):
                raise ValueError('套餐周期仅支持 300 / 10080 分钟')
            start = timestamp(p.get('starts_at'), 'starts_at')
            end = timestamp(p.get('ends_at'), 'ends_at')
            if end <= start:
                raise ValueError('套餐周期结束时间必须晚于开始时间')
            breakdowns = {}
            for b in rows(p.get('breakdowns') or [], 16):
                dim = short_text(b.get('dimension'), 'dimension')
                if dim in breakdowns:
                    raise ValueError('套餐周期统计维度重复')
                breakdowns[dim] = {}
                for r in rows(b.get('rows')):
                    add(breakdowns[dim], r.get('key'), value(r.get('basis_points')) / 100)
            bp = value(p.get('used_basis_points'), True)
            out.append(dict(id=pid, window_minutes=minutes, starts_at=start, ends_at=end,
                            plan_type=short_text(p.get('plan_type'), 'plan_type', 'unknown'),
                            used_percent=None if bp is None else bp / 100,
                            accounting_complete=p.get('accounting_complete') is True,
                            breakdowns=breakdowns, data_as_of=as_of))
        result['periods'] = dict(periods=out, data_as_of=as_of,
                                 coverage_start=timestamp(raw.get('coverage_start'), 'coverage_start', optional=True),
                                 coverage_complete=raw.get('coverage_complete') is True,
                                 approximate=raw.get('approximate', True) is not False, collected_at=observed)
    if data.get('chats') is not None:
        raw = data['chats']
        if not isinstance(raw, dict):
            raise ValueError('chats 必须是对象')
        out, seen = [], set()
        for r in rows(raw.get('rows'), 100):
            tid = short_text(r.get('thread_id'), 'thread_id')
            if tid in seen:
                raise ValueError('聊天 ID 重复')
            seen.add(tid)
            title = r.get('title') or tid
            if not isinstance(title, str) or len(title) > 1000:
                raise ValueError('聊天标题必须是最多 1000 字符的字符串')
            status = r.get('data_status', 'unavailable')
            available = status != 'unavailable'
            item = dict(thread_id=tid, title=title, data_status=status,
                        subagent_count=value(r.get('subagent_count'), True), groups=[])
            for metric in ('weekly_limit_percent', 'five_hour_limit_percent', 'balance_usage_credits'):
                item[metric] = value(r.get(metric), True) if available else None
            for g in rows(r.get('groups') or [], 200):
                group = {k: short_text(g.get(k), k, 'unknown') for k in ('model', 'reasoning_effort', 'speed')}
                group.update({k: value(g.get(k), True) for k in ('weekly_limit_percent', 'five_hour_limit_percent', 'balance_usage_credits')})
                item['groups'].append(group)
            out.append(item)
        result['chats'] = dict(rows=out, data_as_of=timestamp(raw.get('data_as_of'), 'chats.data_as_of', optional=True),
                               is_truncated=raw.get('is_truncated') is True, collected_at=observed,
                               comparison_plan=raw.get('comparison_plan') if isinstance(raw.get('comparison_plan'), dict) else None,
                               scope='最近 30 天活跃的本机聊天；累计消耗含本机已知子任务，最多 100 个聊天')
    if not result:
        raise ValueError('analytics 至少需要 daily、periods 或 chats 中的一项有效数据')
    return dict(account=account, observed_at=observed, report_id=report_id,
                source=short_text(payload.get('source'), 'source', 'codex-analytics'), sections=result)


def merge_sections(old, incoming):
    result = dict(old)
    for name, section in incoming.items():
        previous = old.get(name, {})
        if previous.get('data_as_of') is not None and section.get('data_as_of') is not None and section['data_as_of'] < previous['data_as_of']:
            continue
        if name == 'daily' and previous.get('units') == section['units']:
            merged = {r['date']: r for r in previous.get('data', [])}
            merged.update({r['date']: r for r in section['data']})
            section = dict(section, data=sorted(merged.values(), key=lambda r:r['date'])[-366:])
        if name == 'periods':
            merged = {r['id']: r for r in previous.get('periods', [])}
            merged.update({r['id']: r for r in section['periods']})
            section = dict(section, periods=sorted(merged.values(), key=lambda r:r['starts_at'], reverse=True)[:500])
        result[name] = section
    return result


def save_period_snapshots(conn, account, section):
    """Preserve source values from before a reset across later analytics uploads."""
    if not section:
        return
    for period in section.get('periods', []):
        as_of = period.get('data_as_of') or section.get('data_as_of') or section.get('collected_at')
        conn.execute("""INSERT OR IGNORE INTO analytics_period_snapshots
            (account,period_id,snapshot_at,starts_at,ends_at,data_json) VALUES(?,?,?,?,?,?)""",
            (account, period['id'], as_of, period['starts_at'], period['ends_at'],
             json.dumps(period, ensure_ascii=False)))


def reset_boundary(event):
    """Unexpected resets can only be placed at their detection time."""
    expected = event['expected_reset']
    if (event['kind'] != 'unexpected_reset' and expected is not None
            and event['interval_start'] is not None
            and event['interval_start'] < expected <= event['observed_at']):
        return expected
    return event['observed_at']


def reset_metadata(event):
    return dict(kind=event['kind'], label=RESET_LABELS[event['kind']],
                detected_at=event['observed_at'], interval_start=event['interval_start'],
                boundary=reset_boundary(event), estimated=event['kind'] == 'unexpected_reset')


def period_history(conn, account, section, group):
    """Show separate cycles for each observed reset; never split source percentages."""
    events = [dict(row) for row in conn.execute("""SELECT e.*,s.window_minutes FROM events e
        JOIN samples s ON s.id=e.sample_id WHERE e.account=?
        AND e.kind IN ('scheduled_reset','scheduled_rollover','unexpected_reset')
        ORDER BY e.observed_at,e.id""", (account,))]
    by_window = {}
    for event in events:
        by_window.setdefault(event['window_minutes'], {}).setdefault(event['window_id'], []).append(event)
    output = []
    for minutes in (10080, 300):
        # Plan history has no window_id. Prefer the Codex bucket; a single custom
        # bucket of this duration is also unambiguous.
        windows = by_window.get(minutes, {})
        relevant = windows.get(f'codex:{minutes}', [])
        if not relevant and len(windows) == 1:
            relevant = next(iter(windows.values()))
        boundaries = {}
        for event in relevant:
            boundaries.setdefault(reset_boundary(event), event)
        same_start = {}
        for period in sorted((p for p in section['periods'] if p['window_minutes'] == minutes),
                             key=lambda p: (p['starts_at'], p.get('data_as_of') or 0, p['id'])):
            same_start[period['starts_at']] = period
        raw = list(same_start.values())
        for index, period in enumerate(raw):
            start = period['starts_at']
            following = raw[index + 1]['starts_at'] if index + 1 < len(raw) else None
            end = min(period['ends_at'], following) if following is not None else period['ends_at']
            if end <= start:
                continue
            cuts = sorted(t for t in boundaries if start < t < end)
            stops = [start, *cuts, end]
            for part, (begin, finish) in enumerate(zip(stops, stops[1:])):
                closing = boundaries.get(finish)
                opening = boundaries.get(begin)
                source = period if part == 0 else None
                if source is not None and closing is not None:
                    # The latest source revision might mix usage from both sides.
                    snapshot = conn.execute("""SELECT data_json FROM analytics_period_snapshots
                        WHERE account=? AND period_id=? AND snapshot_at<=?
                        AND starts_at<=? AND ends_at>=?
                        ORDER BY snapshot_at DESC LIMIT 1""",
                        (account, period['id'], finish, begin, finish)).fetchone()
                    source = json.loads(snapshot['data_json']) if snapshot else None
                item = dict(period, starts_at=begin, ends_at=finish,
                            source_starts_at=period['starts_at'], source_ends_at=period['ends_at'],
                            segment_data_available=source is not None,
                            reset_at_start=reset_metadata(opening) if opening else None,
                            reset_at_end=reset_metadata(closing) if closing else None)
                if source is None:
                    item.update(used_percent=None, rows=[], accounting_complete=False, data_as_of=None)
                else:
                    item.update(used_percent=source['used_percent'],
                                accounting_complete=source['accounting_complete'],
                                data_as_of=source.get('data_as_of'),
                                rows=[dict(key=k, label=FEATURES.get(k, k) if group == 'feature' else k,
                                           percent=n)
                                      for k, n in sorted(source['breakdowns'].get(GROUPS[group], {}).items(),
                                                         key=lambda pair: -pair[1])])
                output.append(item)
        # A newly detected cycle can outlive the stale provider period. Extend its
        # empty segment to the newly reported deadline, or create one when the
        # reset falls exactly at the old period's end.
        for boundary, event in boundaries.items():
            next_reset = event['next_reset']
            if next_reset is None or next_reset <= boundary:
                continue
            if any(p['starts_at'] == boundary for p in raw):
                continue
            next_source = next((p['starts_at'] for p in raw if p['starts_at'] > boundary), None)
            next_boundary = next((t for t in sorted(boundaries) if t > boundary), None)
            limits = [next_reset]
            if next_source is not None:
                limits.append(next_source)
            if next_boundary is not None:
                limits.append(next_boundary)
            end = min(limits)
            if end <= boundary:
                continue
            synthetic = next((p for p in output if p['window_minutes'] == minutes
                              and p['starts_at'] == boundary and not p['segment_data_available']), None)
            if synthetic:
                synthetic['ends_at'] = end
                synthetic['reset_at_end'] = reset_metadata(boundaries[end]) if end in boundaries else None
                continue
            previous = next((p for p in reversed(output) if p['window_minutes'] == minutes
                             and p['ends_at'] == boundary), None)
            if previous:
                output.append(dict(previous, starts_at=boundary, ends_at=end,
                                   used_percent=None, rows=[], accounting_complete=False,
                                   data_as_of=None, segment_data_available=False,
                                   reset_at_start=reset_metadata(event),
                                   reset_at_end=reset_metadata(boundaries[end]) if end in boundaries else None))
    return sorted(output, key=lambda p: (p['starts_at'], p['ends_at']), reverse=True)


def register_analytics(app, db, auth, resolve_account):
    with db() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS analytics_state (
            account TEXT PRIMARY KEY, observed_at REAL NOT NULL, received_at REAL NOT NULL,
            source TEXT NOT NULL, data_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS analytics_reports (
            account TEXT NOT NULL, report_id TEXT NOT NULL, digest TEXT NOT NULL,
            observed_at REAL NOT NULL, PRIMARY KEY(account,report_id));
            CREATE TABLE IF NOT EXISTS analytics_period_snapshots (
            account TEXT NOT NULL, period_id TEXT NOT NULL, snapshot_at REAL NOT NULL,
            starts_at REAL NOT NULL, ends_at REAL NOT NULL, data_json TEXT NOT NULL,
            PRIMARY KEY(account,period_id,snapshot_at));''')
        for row in conn.execute('SELECT account,data_json FROM analytics_state'):
            save_period_snapshots(conn, row['account'], json.loads(row['data_json']).get('periods'))

    @app.post('/api/analytics')
    @auth('upload')
    def ingest_analytics():
        now = time.time()
        try:
            data = normalize_analytics(request.get_json(), now)
        except ValueError as error:
            return jsonify(error=str(error)), 422
        digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                data['account'] = resolve_account(conn, data['account'])
            except DeletedAccount:
                return jsonify(error='账号已删除；请在账号管理中允许重新使用该名称，或更改采集器账号名'), 410
            duplicate = conn.execute('SELECT digest FROM analytics_reports WHERE account=? AND report_id=?', (data['account'], data['report_id'])).fetchone()
            if duplicate:
                if duplicate['digest'] != digest:
                    return jsonify(error='同一 report_id 的分析内容发生冲突'), 409
                return jsonify(status='duplicate')
            old = conn.execute('SELECT * FROM analytics_state WHERE account=?', (data['account'],)).fetchone()
            if old and data['observed_at'] <= old['observed_at']:
                return jsonify(status='out_of_order', message='分析快照已过期，未覆盖较新数据')
            merged = merge_sections(json.loads(old['data_json']) if old else {}, data['sections'])
            save_period_snapshots(conn, data['account'], data['sections'].get('periods'))
            conn.execute('INSERT INTO analytics_reports VALUES(?,?,?,?)', (data['account'],data['report_id'],digest,data['observed_at']))
            conn.execute('INSERT INTO analytics_state VALUES(?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET observed_at=excluded.observed_at,received_at=excluded.received_at,source=excluded.source,data_json=excluded.data_json',
                         (data['account'], data['observed_at'], now, data['source'], json.dumps(merged,ensure_ascii=False)))
        return jsonify(status='accepted', sections=list(data['sections']), account=data['account']), 201

    @app.get('/api/analytics')
    @auth('read')
    def analytics():
        account = request.args.get('account', '')
        group = request.args.get('group_by', 'feature')
        try:
            days = int(request.args.get('days', '7'))
            if days not in (7,30) or group not in GROUPS:
                raise ValueError()
        except ValueError:
            return jsonify(error='days 只能是 7 或 30；group_by 只能是 feature、model、surface 或 trigger'), 422
        with db() as conn:
            try:
                account = resolve_account(conn, account)
            except DeletedAccount:
                return jsonify(error='账号已删除'), 410
            row = conn.execute('SELECT * FROM analytics_state WHERE account=?', (account,)).fetchone()
            periods = None
            if row:
                sections = json.loads(row['data_json'])
                if sections.get('periods'):
                    periods = dict(sections['periods'], periods=period_history(
                        conn, account, sections['periods'], group))
        result = dict(account=account, days=days, group_by=group, available=bool(row),
                      feature_labels=FEATURES, now=time.time(), daily=None, chats=None, periods=None)
        if not row:
            return jsonify(result)
        sections = json.loads(row['data_json'])
        result.update(observed_at=row['observed_at'], source=row['source'])
        daily = sections.get('daily')
        if daily:
            end = datetime.now(timezone.utc).date()
            dates = [(end-timedelta(days=i)).isoformat() for i in reversed(range(days))]
            by_date = {r['date']:r for r in daily['data']}
            totals, points = {}, []
            for day in dates:
                r = by_date.get(day)
                v = r['dimensions'].get(group) if r else None
                if v is not None:
                    for k,n in v.items(): totals[k] = totals.get(k,0)+n
                points.append(dict(date=day, values=v, known=v is not None))
            total = sum(totals.values())
            series = [dict(key=k,label=FEATURES.get(k,k) if group=='feature' else k,total=n,share=n/total if total else None)
                      for k,n in sorted(totals.items(),key=lambda item:(-item[1],item[0]))]
            result['daily'] = dict(data=points, series=series, total=total, units=daily['units'],
                                   data_as_of=daily['data_as_of'], collected_at=daily['collected_at'],
                                   missing_days=sum(not p['known'] for p in points))
        result['chats'] = sections.get('chats')
        result['periods'] = periods
        return jsonify(result)
