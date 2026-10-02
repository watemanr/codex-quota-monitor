"""Persistent quota transitions, independent of upload timing and UI lifetime."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from detection import normalize, classify, reset_summary, RESET_KINDS, timestamp


class UsageMonitor:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS monitor_state(account TEXT,identity TEXT,window_id TEXT,payload TEXT,
                PRIMARY KEY(account,identity,window_id));
              CREATE TABLE IF NOT EXISTS monitor_events(id INTEGER PRIMARY KEY,account TEXT,identity TEXT,
                window_id TEXT,observed_at REAL,kind TEXT,payload TEXT);
              CREATE INDEX IF NOT EXISTS monitor_recent ON monitor_events(account,identity,window_id,observed_at DESC);
              CREATE TABLE IF NOT EXISTS monitor_remote(account TEXT,identity TEXT,window_id TEXT,payload TEXT,
                PRIMARY KEY(account,identity,window_id));
            ''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def observe(self,snapshot):
        usage=snapshot.get('reports',{}).get('usage')
        if not usage:return []
        data=normalize(usage,time.time())
        account,identity=data['account'],snapshot['identity']
        if account!=snapshot['account']:raise ValueError('采集账号不一致')
        detected=[]
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for w in data['windows']:
                key=(account,identity,w['window_id'])
                row=db.execute('SELECT payload FROM monitor_state WHERE account=? AND identity=? AND window_id=?',key).fetchone()
                previous=json.loads(row['payload']) if row else None
                current=dict(w,observed_at=data['observed_at'])
                if previous and current['observed_at']<=previous['observed_at']:continue
                event=classify(previous,current)
                kinds=[]
                if event and event['kind'] in (*RESET_KINDS,'uncertain'):kinds.append(event)
                same_window=previous and previous['window_minutes']==w['window_minutes']
                if w['used_percent']>=100 and (not same_window or previous['used_percent']<100):
                    kinds.append(dict(kind='depleted',confidence='high',reason='剩余额度已归零（已用 100%）。'))
                for event in kinds:
                    detail=dict(event,account=account,window_id=w['window_id'],window_minutes=w['window_minutes'],
                        window_type='weekly' if w['window_minutes']==10080 else '5h' if w['window_minutes']==300 else 'other',
                        observed_at=current['observed_at'],interval_start=previous['observed_at'] if previous else None,
                        expected_reset=previous['resets_at'] if previous else None,next_reset=w['resets_at'],
                        before_used=previous['used_percent'] if previous else None,after_used=w['used_percent'])
                    db.execute('INSERT INTO monitor_events(account,identity,window_id,observed_at,kind,payload) VALUES(?,?,?,?,?,?)',
                        (*key,current['observed_at'],event['kind'],json.dumps(detail,ensure_ascii=False)))
                    detected.append(detail)
                db.execute('INSERT OR REPLACE INTO monitor_state VALUES(?,?,?,?)',(*key,json.dumps(current,ensure_ascii=False)))
        return detected

    def merge_remote(self,account,identity,resets):
        if not identity or not isinstance(resets,dict):return
        with self.connection() as db:
            for wid,r in resets.items():
                if not isinstance(wid,str) or len(wid)>160 or not isinstance(r,dict) or r.get('kind') not in RESET_KINDS:continue
                try:
                    observed=timestamp(r['detected_at'],'detected_at')
                    if observed>time.time()+300:continue
                    minutes=r['window_minutes']
                    if not isinstance(minutes,(int,float)) or not 1<=minutes<=525600:continue
                    value=reset_summary(dict(r,observed_at=observed),minutes)
                    for name in ('interval_start','expected_reset','next_reset'):
                        value[name]=timestamp(value[name],name,optional=True)
                except (ValueError,KeyError,TypeError):continue
                old=db.execute('SELECT payload FROM monitor_remote WHERE account=? AND identity=? AND window_id=?',(account,identity,wid)).fetchone()
                if old and json.loads(old['payload'])['detected_at']>observed:continue
                db.execute('INSERT OR REPLACE INTO monitor_remote VALUES(?,?,?,?)',(account,identity,wid,json.dumps(value,ensure_ascii=False)))

    def latest(self,account,identity):
        if not identity:return {}
        with self.connection() as db:
            rows=db.execute('''SELECT * FROM (
                SELECT *,ROW_NUMBER() OVER (PARTITION BY window_id ORDER BY observed_at DESC,id DESC) AS rn
                FROM monitor_events WHERE account=? AND identity=?
                AND kind IN ('scheduled_reset','scheduled_rollover','unexpected_reset')) WHERE rn=1''',(account,identity)).fetchall()
            result={r['window_id']:reset_summary(json.loads(r['payload']),json.loads(r['payload'])['window_minutes']) for r in rows}
            for r in db.execute('SELECT window_id,payload FROM monitor_remote WHERE account=? AND identity=?',(account,identity)):
                value=json.loads(r['payload']);old=result.get(r['window_id'])
                if not old or value['detected_at']>=old['detected_at']:result[r['window_id']]=value
        return result


def notification_text(events):
    labels={'depleted':'剩余额度归零','scheduled_reset':'计划内重置','scheduled_rollover':'计划周期切换',
            'unexpected_reset':'疑似突发重置','uncertain':'额度回升，重置类型待确认'}
    title='Codex 额度提醒'
    parts=[]
    for e in events:
        label=e['window_type'] if e['window_type']!='other' else f"{e['window_minutes']:g} 分钟"
        at=dt.datetime.fromtimestamp(e['observed_at']).strftime('%m/%d %H:%M')
        parts.append(f"{e['account']} · {label}：{labels[e['kind']]}（{at} 检测）")
    return title,'\n'.join(parts)
