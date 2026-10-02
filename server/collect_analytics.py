#!/usr/bin/env python3
"""Collect usage only; OpenAI credentials stay on this computer.
Uses endpoints observed in Codex Desktop 26.915.4065.0. These may change.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def collect(codex_home, account):
    credentials = json.loads((codex_home/'auth.json').read_text(encoding='utf-8-sig')).get('tokens') or {}
    if not credentials.get('access_token') or not credentials.get('account_id'):
        raise RuntimeError('需要此电脑已经登录 Codex；未找到可用的 ChatGPT 登录状态')
    client = urllib.request.build_opener(NoRedirect())
    headers = {'Authorization':'Bearer '+credentials['access_token'], 'ChatGPT-Account-Id':credentials['account_id'],
               'Accept':'application/json', 'Content-Type':'application/json', 'User-Agent':'codex-quota-monitor/1.2'}
    def get(path, body=None):
        # The credential-bearing destination is fixed, never supplied by the server.
        if not path.startswith('/wham/usage/'):
            raise ValueError('Unsupported usage endpoint')
        req = urllib.request.Request('https://chatgpt.com/backend-api'+path, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        with client.open(req, timeout=30) as response:
            return json.load(response)
    now = dt.datetime.now(dt.timezone.utc)
    start = (now-dt.timedelta(days=29)).date().isoformat()
    end = now.date().isoformat()
    sections, failures = {}, []
    for name, path in [('daily','/wham/usage/daily-token-usage-breakdown?start_date='+start+'&end_date='+end+'&group_by=day'),
                       ('periods','/wham/usage/plan_limit_history?days=7')]:
        try:
            sections[name] = get(path)
        except urllib.error.HTTPError as e:
            failures.append(f'{name}: HTTP {e.code}')
        except Exception as e:
            failures.append(f'{name}: {type(e).__name__}')
    try:
        # Only index metadata is read: no rollout, prompt, reply or conversation body.
        db_file = codex_home/'state_5.sqlite'
        con = sqlite3.connect(db_file.as_uri()+'?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        items = [dict(r) for r in con.execute('SELECT id,name,title,created_at,updated_at,source,archived FROM threads')]
        con.close()
        children, roots = {}, []
        for item in items:
            try: source = json.loads(item['source'])
            except (ValueError,TypeError): source = {}
            sub = source.get('subagent') if isinstance(source,dict) else None
            parent = (sub.get('thread_spawn') or {}).get('parent_thread_id') if isinstance(sub,dict) else None
            if parent: children.setdefault(parent,[]).append(item['id'])
            if not sub and not item['archived'] and item['updated_at'] >= now.timestamp()-30*86400:
                roots.append(item)
        roots.sort(key=lambda r:(-r['updated_at'],r['id']))
        truncated = len(roots)>100
        roots = roots[:100]
        query, meta = [], {}
        for root in roots:
            seen, pending = set(), list(children.get(root['id'],[]))
            while pending:
                child = pending.pop()
                if child not in seen:
                    seen.add(child); pending.extend(children.get(child,[]))
            if len(seen)>=1000:
                truncated=True
                continue
            query.append({'thread_id':root['id'],'created_at':dt.datetime.fromtimestamp(root['created_at'],dt.timezone.utc).isoformat(),
                          'descendant_thread_ids':sorted(seen)})
            meta[root['id']]={'title':root['name'] or root['title'] or root['id'],'subagent_count':len(seen)}
        batches, batch, count = [], [], 0
        used_ids = set()
        for item in query:
            ids = {item['thread_id'],*item['descendant_thread_ids']}
            if used_ids.intersection(ids):
                raise RuntimeError('任务分组包含重叠的子任务，停止排名采集以避免重复计算')
            used_ids.update(ids)
            if count+len(ids)>1000 or len(batch)>=20:
                batches.append(batch);batch=[];count=0
            batch.append(item);count+=len(ids)
        if batch:batches.append(batch)
        output, as_of, comparison_plan = [], [], None
        for batch in batches:
            response = get('/wham/usage/thread_usage/query_v2', {'threads':batch})
            comparison_plan = response.get('comparison_plan')
            as_of.append(response.get('data_as_of'))
            found = {r['thread_id']:r for r in response['threads']}
            for item in batch:
                tid = item['thread_id']
                row = found.get(tid, {'thread_id':tid,'data_status':'unavailable','groups':[]})
                output.append(dict(row,**meta[tid]))
        # Empty local history is a known empty result, not a query failure.
        sections['chats']={'rows':output,'data_as_of':min(as_of) if as_of and all(as_of) else None,
                           'comparison_plan':comparison_plan,'is_truncated':truncated}
    except urllib.error.HTTPError as e:
        failures.append(f'chats: HTTP {e.code}')
    except Exception as e:
        failures.append(f'chats: {type(e).__name__}')
    if not sections:
        raise RuntimeError('没有取得分析数据；'+', '.join(failures))
    return {'account':account,'observed_at':now.isoformat(),'report_id':str(uuid.uuid4()),
            'source':'codex-desktop-usage','analytics':sections}, failures


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True,help='网页中的固定账号名称，例如 my-codex')
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    parser.add_argument('--output',type=Path,required=True,help='仅包含统计数据的 JSON 输出文件')
    args=parser.parse_args()
    payload,failures=collect(args.codex_home.resolve(),args.account)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'saved':str(args.output),'sections':list(payload['analytics']),
                      'chats':len(payload['analytics'].get('chats',{}).get('rows',[])),
                      'warnings':failures},ensure_ascii=False))


if __name__=='__main__':
    main()
