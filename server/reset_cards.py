"""Observed reset-card inventory. Count changes do not identify redemptions."""
from collections import deque


def reset_card_stats(conn, now, cutoff, account=None):
    where = ' AND account=?' if account is not None else ''
    args = (account,) if account is not None else ()
    result = {}
    # Reports are also moved/deleted by account management. Derive transitions in
    # observation order so retries, late uploads and merges cannot double-count.
    latest = conn.execute('''SELECT * FROM (
        SELECT account,observed_at,reset_card_count,
          ROW_NUMBER() OVER (PARTITION BY account ORDER BY observed_at DESC,received_at DESC,id DESC) AS rn
        FROM reports WHERE reset_cards_reported=1''' + where + ''') WHERE rn=1''', args)
    for row in latest:
        result[row['account']] = dict(
            available_count=row['reset_card_count'], observed_at=row['observed_at'],
            stale=now-row['observed_at'] > 1800,
            baseline_at=None, baseline_count=None, last_known_count=None,
            last_known_at=None, last_change=None, increases=0, decreases=0,
            event_count=0, events=deque(maxlen=20))
    changes = conn.execute('''WITH dated AS (
        SELECT account,observed_at,reset_card_count,
          ROW_NUMBER() OVER (PARTITION BY account,observed_at ORDER BY received_at DESC,id DESC) AS rn
        FROM reports WHERE reset_card_count IS NOT NULL''' + where + '''
    ), series AS (
        SELECT account,observed_at,reset_card_count,
          LAG(reset_card_count) OVER (PARTITION BY account ORDER BY observed_at) AS before_count,
          LAG(observed_at) OVER (PARTITION BY account ORDER BY observed_at) AS interval_start,
          MAX(observed_at) OVER (PARTITION BY account) AS latest_at
        FROM dated WHERE rn=1
    ) SELECT * FROM series WHERE before_count IS NULL OR before_count!=reset_card_count
        OR observed_at=latest_at ORDER BY account,observed_at''', args)
    for row in changes:
        item = result[row['account']]
        count, before, when = row['reset_card_count'], row['before_count'], row['observed_at']
        item.update(last_known_count=count, last_known_at=when)
        if before is None:
            item.update(baseline_count=count, baseline_at=when)
        elif count != before:
            event = dict(observed_at=when, interval_start=row['interval_start'],
                         before_count=before, after_count=count, delta=count-before)
            item['last_change'] = event
            if when >= cutoff:
                item['event_count'] += 1
                item['increases'] += max(0, count-before)
                item['decreases'] += max(0, before-count)
                item['events'].append(event)
    for item in result.values():
        item['events'] = list(reversed(item['events']))
    return result
