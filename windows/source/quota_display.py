"""Platform-independent quota text for notification areas."""
import datetime as dt
import math

def quota_tooltip(snapshot):
    """Use the actual sampled account; primary/secondary order does not identify weekly."""
    if not snapshot:
        return 'Codex 用量采集器 · 尚未读取额度\n双击打开，右键菜单'
    windows=snapshot.get('reports',{}).get('usage',{}).get('windows',[])
    parts=[]
    for minutes,label in ((10080,'weekly'),(300,'5h')):
        window=next((w for w in windows if w.get('window_minutes')==minutes),None)
        used=window.get('used_percent') if window else None
        if isinstance(used,(int,float)) and not isinstance(used,bool) and math.isfinite(used) and 0<=used<=100:
            parts.append(f'{100-used:g}%/{label}')
    account=' '.join(str(snapshot.get('account','')).split())
    account=account[:36]+('…' if len(account)>36 else '')
    lines=[' · '.join(parts) if parts else '额度未返回']
    if account:lines.append('账号：'+account)
    try:
        observed=snapshot.get('reports',{}).get('usage',{}).get('observed_at') or snapshot.get('collected_at')
        when=dt.datetime.fromisoformat(observed.replace('Z','+00:00')).astimezone().strftime('%m/%d %H:%M')
        lines.append('采样 '+when+' · 双击打开')
    except (ValueError,TypeError,AttributeError,OverflowError):
        lines.append('双击打开，右键菜单')
    return '\n'.join(lines)
