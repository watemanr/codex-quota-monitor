"""Read-only collector freshness and deliberately conservative quota forecasts."""
import math

COLLECTOR_SOURCES = ('windows-collector', 'macos-collector')
RESET_KINDS = ('scheduled_reset', 'unexpected_reset')


def collector_status(conn, now, account=None):
    names = [row['account'] for row in conn.execute(
        'SELECT account FROM reports UNION SELECT account FROM analytics_state ORDER BY account COLLATE NOCASE')]
    if account is not None:
        names = [name for name in names if name == account]
    latest = {row['account']: row for row in conn.execute('''
        SELECT account, MAX(received_at) AS last_upload_at,
               MAX(observed_at) AS last_sample_at, COUNT(*) AS upload_count
        FROM reports WHERE source IN (?,?) GROUP BY account
    ''', COLLECTOR_SOURCES)}
    result = []
    for name in names:
        row = latest.get(name)
        age = max(0, now - row['last_upload_at']) if row else None
        status = ('never' if age is None else 'recent' if age <= 1800 else
                  'delayed' if age <= 7200 else 'stale')
        result.append(dict(account=name, status=status,
                           last_upload_at=row['last_upload_at'] if row else None,
                           last_sample_at=row['last_sample_at'] if row else None,
                           upload_count=row['upload_count'] if row else 0))
    return result


def estimate_window(points, latest, now):
    """Estimate only within a continuous, fresh, increasing used-percent segment."""
    base = dict(window_id=latest['window_id'], label=latest['label'],
                window_minutes=latest['window_minutes'], observed_at=latest['observed_at'],
                used_percent=latest['used_percent'], remaining_percent=100-latest['used_percent'],
                resets_at=latest['resets_at'], status='insufficient',
                estimated_at=None, rate_pp_per_hour=None, points=0, span_hours=None)
    if now - latest['observed_at'] > 1800:
        base['status'] = 'stale'
        return base
    if latest['used_percent'] >= 100:
        base['status'] = 'exhausted'
        return base
    if latest['resets_at'] is not None and latest['resets_at'] <= now:
        base['status'] = 'reset_due'
        return base
    segment = [points[-1]] if points else []
    for older in reversed(points[:-1]):
        newer = segment[-1]
        if newer['used_percent'] < older['used_percent'] - 1.5:
            break
        segment.append(older)
    segment.reverse()
    if len(segment) < 3:
        return base
    span = segment[-1]['observed_at'] - segment[0]['observed_at']
    minimum_span = 600 if latest['window_minutes'] <= 300 else 7200
    if span < minimum_span:
        return base
    increase = segment[-1]['used_percent'] - segment[0]['used_percent']
    if increase < 1:
        base['status'] = 'flat'
        return base
    rate = increase / (span / 3600)
    if not math.isfinite(rate) or rate <= 0:
        return base
    estimated = latest['observed_at'] + (100 - latest['used_percent']) / rate * 3600
    base.update(rate_pp_per_hour=round(rate, 2), points=len(segment),
                span_hours=round(span / 3600, 2))
    if estimated <= now:
        base['status'] = 'awaiting_sample'
    elif latest['resets_at'] is not None and estimated >= latest['resets_at']:
        base['status'] = 'after_reset'
    else:
        base.update(status='before_reset', estimated_at=estimated)
    return base


def forecasts(conn, now, account=None):
    names = [row['account'] for row in conn.execute(
        'SELECT account FROM reports UNION SELECT account FROM analytics_state ORDER BY account COLLATE NOCASE')]
    if account is not None:
        names = [name for name in names if name == account]
    result = []
    for name in names:
        latest = [dict(row) for row in conn.execute('''
            SELECT s.window_id,s.label,s.window_minutes,s.observed_at,s.used_percent,s.resets_at
            FROM samples s JOIN (SELECT window_id,MAX(observed_at) AS t FROM samples
                WHERE account=? AND in_order=1 GROUP BY window_id) x
            ON s.window_id=x.window_id AND s.observed_at=x.t
            WHERE s.account=? AND s.in_order=1 ORDER BY s.window_minutes,s.window_id
        ''', (name, name))]
        windows = []
        for row in latest:
            lookback = 7200 if row['window_minutes'] <= 300 else 172800
            cutoff = row['observed_at'] - lookback
            reset = conn.execute('''SELECT MAX(observed_at) AS t FROM events
                WHERE account=? AND window_id=? AND kind IN (?,?) AND observed_at<=?''',
                (name, row['window_id'], *RESET_KINDS, row['observed_at'])).fetchone()['t']
            if reset is not None:
                cutoff = max(cutoff, reset)
            points = [dict(p) for p in conn.execute('''SELECT observed_at,used_percent,resets_at
                FROM samples WHERE account=? AND window_id=? AND in_order=1 AND observed_at>=?
                ORDER BY observed_at DESC LIMIT 200''', (name, row['window_id'], cutoff))]
            points.reverse()
            windows.append(estimate_window(points, row, now))
        result.append(dict(account=name, windows=windows))
    return result
