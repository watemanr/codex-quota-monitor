"""Snapshot-based inference; event names describe evidence, never provider intent."""
import math
from datetime import datetime, timezone


def timestamp(value, field, optional=False):
    if value is None and optional:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError()
        if isinstance(value, (int, float)):
            result = float(value)
            if result > 100_000_000_000:
                result /= 1000
        else:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError()
            result = dt.timestamp()
        if not math.isfinite(result) or not 1577836800 <= result <= 4102444800:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{field} 必须是 Unix 秒/毫秒或含时区的 ISO 8601 时间') from None


def number(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{field} 必须是数字')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{field} 必须在 {low}–{high} 之间')
    return float(value)


def short_text(value, field, default=None):
    if value is None:
        value = default
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError(f'{field} 必须是 1–160 字符的字符串')
    return value.strip()


def unwrap(value):
    import json
    if not isinstance(value, dict):
        raise ValueError('JSON 顶层必须是对象')
    if isinstance(value.get('structuredContent'), dict):
        return unwrap(value['structuredContent'])
    if isinstance(value.get('content'), list):
        for item in value['content']:
            if isinstance(item, dict) and item.get('type') == 'text':
                try:
                    return unwrap(json.loads(item['text']))
                except (ValueError, KeyError, TypeError):
                    continue
    return value


def normalize(payload, now):
    outer = unwrap(payload)
    data = unwrap(outer.get('usage', outer))
    account = short_text(outer.get('account', data.get('accountId')), 'account', 'default')
    observed = timestamp(outer.get('observed_at', now), 'observed_at')
    if observed > now + 300:
        raise ValueError('observed_at 超出服务器时间 5 分钟，请检查采集端时钟')
    source = short_text(outer.get('source'), 'source', 'codex')
    report_id = outer.get('report_id')
    if report_id is not None:
        report_id = short_text(report_id, 'report_id')
    windows = data.get('windows')
    plan = None
    if windows is None:
        windows = []
        buckets = data.get('rateLimitsByLimitId')
        if not isinstance(buckets, dict) or not buckets:
            legacy = data.get('rateLimits')
            buckets = {legacy.get('limitId') or 'codex': legacy} if isinstance(legacy, dict) else {}
        for key, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            plan = bucket.get('planType') or plan
            for slot in ('primary', 'secondary'):
                w = bucket.get(slot)
                if not isinstance(w, dict) or w.get('usedPercent') is None:
                    continue
                minutes = w.get('windowDurationMins')
                number(minutes, 'windowDurationMins', 1, 525600)
                label = '5 小时额度' if minutes == 300 else '每周额度' if minutes == 10080 else f'{minutes:g} 分钟额度'
                if key != 'codex':
                    label = f'{key} · {label}'
                windows.append({'id': f'{key}:{minutes:g}', 'label': label,
                                'window_minutes': minutes, 'used_percent': w['usedPercent'],
                                'resets_at': w.get('resetsAt')})
    if not isinstance(windows, list) or not 1 <= len(windows) <= 32:
        raise ValueError('没有可用额度窗口；需要 windows 数组或 Codex rateLimits 数据，null 不代表零用量')
    normalized, seen = [], set()
    for w in windows:
        if not isinstance(w, dict):
            raise ValueError('每个窗口必须是对象')
        minutes = number(w.get('window_minutes'), 'window_minutes', 1, 525600)
        wid = short_text(w.get('id'), 'windows.id', f'codex:{minutes:g}')
        if wid in seen:
            raise ValueError(f'窗口重复：{wid}')
        seen.add(wid)
        used = w.get('used_percent')
        if used is None and w.get('remaining_percent') is not None:
            used = 100 - number(w['remaining_percent'], 'remaining_percent', 0, 100)
        used = number(used, 'used_percent', 0, 100)
        if w.get('remaining_percent') is not None and abs(used + number(w['remaining_percent'], 'remaining_percent', 0, 100) - 100) > 0.01:
            raise ValueError('used_percent 与 remaining_percent 不一致')
        default_label = '5 小时额度' if minutes == 300 else '每周额度' if minutes == 10080 else wid
        normalized.append({'window_id': wid, 'label': short_text(w.get('label'), 'label', default_label),
                           'window_minutes': minutes, 'used_percent': used,
                           'resets_at': timestamp(w.get('resets_at'), 'resets_at', optional=True)})
    return dict(account=account, observed_at=observed, source=source, report_id=report_id,
                plan=plan if isinstance(plan, str) and len(plan) <= 80 else None, windows=normalized)


def classify(previous, current, tolerance=300, minimum_drop=2):
    def event(kind, confidence, reason):
        return {'kind': kind, 'confidence': confidence, 'reason': reason}
    if previous is None:
        return event('baseline', 'info', '首次收到此额度窗口，建立比较基线。')
    if previous['window_minutes'] != current['window_minutes']:
        return event('configuration_change', 'low', '额度窗口长度发生变化，需要重新建立基线。')
    before, after = previous['observed_at'], current['observed_at']
    old_reset, new_reset = previous['resets_at'], current['resets_at']
    drop = previous['used_percent'] - current['used_percent']
    shifted = old_reset is not None and new_reset is not None and abs(new_reset - old_reset) > tolerance
    crossed = old_reset is not None and before < old_reset <= after
    gap = after - before
    # A previously expired deadline cannot explain a newly observed reset.
    if drop >= minimum_drop:
        detail = f'已用额度下降 {drop:g} 个百分点。'
        if old_reset is None or old_reset <= before:
            return event('uncertain', 'low', detail + '上一条没有有效的未来重置时间，无法区分计划内与突然重置。')
        if after < old_reset - tolerance:
            return event('unexpected_reset', 'high' if shifted and new_reset > old_reset else 'medium',
                         detail + '两次采样均早于原计划重置时间，疑似提前重置或额度调整；无法单凭额度确定原因。')
        if crossed and gap <= current['window_minutes'] * 60:
            return event('scheduled_reset', 'medium', detail + '采样区间跨越原计划重置时间，符合计划内重置。')
        if crossed:
            return event('uncertain', 'low', detail + '采样间隔超过一个额度周期，无法确定重置次数和类型。')
        return event('uncertain', 'low', detail + '变化发生在计划时间前的容差区间内，待后续采样确认。')
    if shifted:
        if new_reset > old_reset and crossed and gap <= current['window_minutes'] * 60:
            return event('scheduled_rollover', 'medium', '重置时间在原计划到期后进入下一周期；采样间未观察到明显额度回升。')
        # At 0% used, an idle deadline can slide at every poll without a reset.
        if previous['used_percent'] == current['used_percent'] == 0:
            return None
        return event('schedule_change', 'low', '预计重置时间发生变化，但没有明显额度回升，不能确认发生重置。')
    if drop > 0:
        return event('measurement_adjustment', 'low', f'已用额度下降 {drop:g} 个百分点，低于 {minimum_drop:g} 点阈值，作为小幅校正记录。')
    return None


RESET_KINDS = ('scheduled_reset', 'scheduled_rollover', 'unexpected_reset')
RESET_LABELS = {'scheduled_reset': '计划内重置', 'scheduled_rollover': '计划周期切换',
                'unexpected_reset': '疑似突发重置'}


def reset_summary(event, window_minutes):
    """Detection time is an upper bound, not the exact provider reset time."""
    if not event or event['kind'] not in RESET_KINDS:
        return None
    return {key: event.get(key) for key in ('kind', 'confidence', 'reason', 'interval_start',
            'expected_reset', 'next_reset', 'before_used', 'after_used')} | {
        'detected_at': event['observed_at'], 'window_minutes': window_minutes,
        'window_type': 'weekly' if window_minutes == 10080 else '5h' if window_minutes == 300 else 'other',
        'type_label': RESET_LABELS[event['kind']]}
