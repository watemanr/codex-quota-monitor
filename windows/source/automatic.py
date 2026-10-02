"""Independent interval timers and a durable, ordered upload outbox."""
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import sqlite3
import time

from collector_core import CollectorError, normalize_url, check_service, upload_snapshot


def interval_minutes(value):
    try:
        if isinstance(value,bool):raise ValueError()
        number=int(str(value))
        if not 0<=number<=1440:raise ValueError()
        return number
    except (ValueError,TypeError):
        raise CollectorError('读取和上传间隔必须是 0–1440 的整数，单位为分钟；0 表示关闭') from None


class IntervalSchedule:
    def __init__(self,clock=time.monotonic):
        self.clock=clock
        self.active=False
        self.running=None
        self.seconds={}
        self.next_at={}

    def start(self,read_minutes,upload_minutes,read_enabled=True,upload_enabled=True):
        if self.running:
            raise CollectorError('当前操作尚未结束，请等待后再开始自动运行')
        read=interval_minutes(read_minutes)
        upload=interval_minutes(upload_minutes)
        self.seconds={}
        if read_enabled and read>0:self.seconds['read']=read*60
        if upload_enabled and upload>0:self.seconds['upload']=upload*60
        now=self.clock()
        self.next_at={k:now if k=='read' else now+v for k,v in self.seconds.items()}
        self.active=bool(self.seconds)

    def stop(self):
        self.active=False
        self.next_at={}

    def due(self):
        if not self.active or self.running:return None
        now=self.clock()
        ready=[(at,0 if kind=='read' else 1,kind) for kind,at in self.next_at.items() if at<=now]
        return min(ready)[2] if ready else None

    def begin(self,kind):
        if not self.active or self.running or kind not in self.next_at:
            raise CollectorError('自动任务状态发生变化')
        self.running=kind
        self.next_at.pop(kind)

    def finish(self):
        kind=self.running
        self.running=None
        if kind and self.active:
            # No catch-up storm after sleep or a slow/failing request.
            self.next_at[kind]=self.clock()+self.seconds[kind]


class Outbox:
    MAX_BYTES=256*1024*1024
    MAX_ROWS=10000

    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS pending (
                  id INTEGER PRIMARY KEY,account TEXT NOT NULL,destination TEXT NOT NULL,
                  kind TEXT NOT NULL,report_id TEXT NOT NULL,observed REAL NOT NULL,
                  payload TEXT NOT NULL,size INTEGER NOT NULL,
                  UNIQUE(account,kind,report_id));
                CREATE INDEX IF NOT EXISTS pending_scope ON pending(account,destination,kind,observed,id);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        conn=sqlite3.connect(self.path,timeout=10)
        conn.row_factory=sqlite3.Row
        try:
            with conn:yield conn
        finally:conn.close()

    def scope(self,account,destination):
        with self.connection() as conn:
            count=conn.execute('SELECT COUNT(*) FROM pending WHERE account=? AND destination IN (?,?)',
                (account,destination,'')).fetchone()[0]
            total=conn.execute('SELECT COUNT(*) FROM pending').fetchone()[0]
        return count,total-count

    def check_capacity(self):
        with self.connection() as conn:
            count,size=conn.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM pending').fetchone()
        if count>=self.MAX_ROWS or size>=self.MAX_BYTES-1024*1024:
            raise CollectorError('待上传队列接近上限，请先上传已有数据；不会删除未上传记录')

    def _add(self,conn,snapshot,destination):
        destination=normalize_url(destination) if destination else ''
        account=snapshot['account']
        rows=[]
        for kind,payload in snapshot['reports'].items():
            if kind not in ('usage','analytics') or payload.get('account')!=account:
                raise CollectorError('采集数据的账号或类型不一致')
            rid=payload.get('report_id')
            if not isinstance(rid,str) or not rid:raise CollectorError('采集数据缺少 report_id')
            observed=dt.datetime.fromisoformat(payload['observed_at'].replace('Z','+00:00'))
            if observed.tzinfo is None:raise CollectorError('采样时间缺少时区')
            encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            old=conn.execute('SELECT payload FROM pending WHERE account=? AND kind=? AND report_id=?',
                             (account,kind,rid)).fetchone()
            if old:
                if json.loads(old['payload'])!=payload:raise CollectorError('相同上报 ID 的内容发生变化')
                continue
            rows.append((account,destination,kind,rid,observed.timestamp(),encoded,len(encoded.encode('utf8'))))
        count,size=conn.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM pending').fetchone()
        if count+len(rows)>self.MAX_ROWS or size+sum(r[-1] for r in rows)>self.MAX_BYTES:
            raise CollectorError('待上传队列已满，请先上传；原有记录保持不变')
        conn.executemany('INSERT INTO pending(account,destination,kind,report_id,observed,payload,size) VALUES(?,?,?,?,?,?,?)',rows)
        return len(rows)

    def add(self,snapshot,destination):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._add(conn,snapshot,destination)

    def migrate_legacy(self,snapshot,destination):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():return 0
            count=self._add(conn,snapshot,destination) if snapshot else 0
            conn.execute("INSERT INTO metadata(key,value) VALUES('legacy_imported','1')")
            return count

    def bind(self,account,destination):
        destination=normalize_url(destination)
        with self.connection() as conn:
            conn.execute("UPDATE pending SET destination=? WHERE account=? AND destination=''",(destination,account))

    def entries(self,account,destination,kind,limit=100):
        with self.connection() as conn:
            rows=conn.execute('SELECT id,payload FROM pending WHERE account=? AND destination=? AND kind=? ORDER BY observed,id LIMIT ?',
                              (account,destination,kind,limit)).fetchall()
        return [(r['id'],json.loads(r['payload'])) for r in rows]

    def acknowledge(self,row_id):
        with self.connection() as conn:
            conn.execute('DELETE FROM pending WHERE id=?',(row_id,))


def flush_outbox(outbox,account,base,token,progress=lambda message:None,max_seconds=30):
    base=normalize_url(base)
    if not token:raise CollectorError('请填写上传密钥 UPLOAD_TOKEN')
    count,other=outbox.scope(account,base)
    result={'sent':0,'duplicate':0,'stale':0,'errors':[],'remaining':count,'other':other}
    if not count:return result
    check_service(base)
    outbox.bind(account,base)
    # The time budget prevents a large offline backlog from monopolizing the reader.
    deadline=time.monotonic()+max_seconds
    for kind in ('usage','analytics'):
        for row_id,payload in outbox.entries(account,base,kind):
            if time.monotonic()>=deadline:break
            progress('正在补传'+('额度' if kind=='usage' else '分析')+'，已确认 '+str(result['sent'])+' 条…')
            outcomes=upload_snapshot({'reports':{kind:payload}},base,token,verify_service=False)
            status=outcomes[kind]
            if not status['ok']:
                result['errors'].append(('额度' if kind=='usage' else '分析')+'：'+status['error'])
                # Preserve chronological processing within each report type.
                break
            outbox.acknowledge(row_id)
            result['sent']+=1
            if status['status']=='duplicate':result['duplicate']+=1
            if status['status']=='out_of_order':result['stale']+=1
    result['remaining'],result['other']=outbox.scope(account,base)
    return result
