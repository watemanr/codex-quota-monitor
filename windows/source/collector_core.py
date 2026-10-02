"""Shared desktop collector: no model calls; credentials never leave their intended origin."""
import base64
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from collect_analytics import collect as collect_analytics, NoRedirect

VERSION = '1.2.1'
DEFAULT_URL = ''
MAX_UPLOAD = 262144


class CollectorError(Exception):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_home():
    return Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))


def normalize_url(value):
    value = value.strip().rstrip('/')
    try:
        p = urllib.parse.urlsplit(value)
        valid_port = p.port
    except ValueError:
        raise CollectorError('服务地址格式不正确') from None
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username is not None
            or p.password is not None or p.query or p.fragment or '\\' in value
            or re.search(r'\s|[\x00-\x1f\x7f]', value)):
        raise CollectorError('请填写完整网页地址，如 https://quota.example.com；不要包含密码、参数或片段')
    if p.path.endswith(('/api/usage', '/api/analytics', '/healthz')):
        raise CollectorError('请填写网页根地址，不要填写 /api/usage 等接口路径')
    if '..' in p.path.split('/'):
        raise CollectorError('服务地址不能包含上级目录')
    return value


def validate_account(value):
    value = value.strip()
    if not value or len(value) > 100 or any(ord(c)<32 for c in value):
        raise CollectorError('账号名称需要 1–100 个字符，例如 my-codex')
    return value


def load_login(home):
    try:
        data = json.loads((Path(home).expanduser().resolve()/'auth.json').read_text(encoding='utf-8-sig'))
        tokens = data.get('tokens') or {}
        if not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token','account_id')):
            raise CollectorError('未找到 ChatGPT 登录状态。请先在本机 Codex 登录；仅配置 API Key 不适用。')
        return tokens
    except FileNotFoundError:
        raise CollectorError('没有找到 auth.json，请检查 Codex 登录目录，或先在 Codex 中登录。') from None
    except (ValueError, OSError, AttributeError):
        raise CollectorError('无法读取 Codex 登录文件，请检查目录和文件权限。') from None


def login_fingerprint(tokens):
    # Used only to keep local account labels stable, never as proof of authentication.
    subject = ''
    for name in ('id_token', 'access_token'):
        try:
            part = tokens.get(name, '').split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '='*(-len(part)%4)))
            subject = claims.get('sub') or subject
        except (ValueError, IndexError, TypeError):
            pass
    return hashlib.sha256((tokens['account_id']+'\0'+str(subject)).encode()).hexdigest()


def error_text(exc):
    if isinstance(exc, CollectorError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return 'OpenAI 统计接口返回 HTTP '+str(exc.code)+('；请在 Codex 恢复登录后重试' if exc.code==401 else '')
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return '网络请求超时，请检查网络或代理后重试'
    if isinstance(exc, urllib.error.URLError):
        return '网络连接失败，请检查网络、代理和服务地址'
    if isinstance(exc, OSError):
        return '本地文件或网络操作失败，请检查目录权限和网络'
    return '操作失败（'+type(exc).__name__+'），可查看使用说明排查'


def http_json(url, token=None, payload=None, headers=None, timeout=30):
    hdr = {'Accept':'application/json', 'User-Agent':'CodexUsageCollector/'+VERSION}
    if headers:
        hdr.update(headers)
    if token:
        if not token.isascii() or any(ord(c)<33 or ord(c)>126 for c in token):
            raise CollectorError('上传密钥应为一行字符，请勿包含空格或换行')
        hdr['Authorization'] = 'Bearer '+token
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(',',':'), allow_nan=False).encode('utf8')
        if len(body) > MAX_UPLOAD:
            raise CollectorError('分析数据超过服务器 256 KiB 限制；请先仅上传实时额度，并保留导出文件')
        hdr['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=hdr)
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        raw = response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:
            raise CollectorError('服务返回的数据过大')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CollectorError('服务未返回 JSON；请检查网址和 Nginx 转发配置') from None
    if not isinstance(data, dict):
        raise CollectorError('服务返回格式不正确')
    return data


def convert_quota(raw, account, observed_at):
    rate = raw.get('rate_limit')
    windows = []
    warnings = []
    seen = set()
    for slot in ('primary_window','secondary_window'):
        w = rate.get(slot) if isinstance(rate, dict) else None
        if not isinstance(w, dict) or w.get('used_percent') is None:
            continue
        seconds, used = w.get('limit_window_seconds'), w.get('used_percent')
        valid = lambda n: isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n)
        if not valid(seconds) or not 60<=seconds<=31536000 or not valid(used) or not 0<=used<=100:
            warnings.append('一个额度窗口的数值不合法，已跳过')
            continue
        minutes = seconds/60
        if minutes in seen:
            raise CollectorError('接口返回重复额度窗口，已停止采集以避免混淆')
        seen.add(minutes)
        reset = w.get('reset_at')
        if reset is not None and (not valid(reset) or not 1577836800<=reset<=4102444800):
            reset = None
            warnings.append('重置时间无效，已保留为未知')
        label = '5 小时额度' if minutes==300 else '每周额度' if minutes==10080 else f'{minutes:g} 分钟额度'
        windows.append({'id':f'codex:{minutes:g}','label':label,'window_minutes':minutes,
                        'used_percent':used,'resets_at':reset})
    if not windows:
        raise CollectorError('接口未返回可用的实时额度窗口；不会把缺失值当成 0')
    return {'account':account,'observed_at':observed_at,'report_id':str(uuid.uuid4()),
            'source':'macos-collector' if sys.platform=='darwin' else 'windows-collector','windows':windows}, warnings


def collect_snapshot(home, account, include_analytics=True, expected_identity=None, progress=lambda message:None):
    account = validate_account(account)
    home = Path(home).expanduser().resolve()
    tokens = load_login(home)
    identity = login_fingerprint(tokens)
    if expected_identity and expected_identity != identity:
        raise CollectorError('此账号名称绑定的 Codex 登录已变化。请改用该登录对应的账号名称，避免混合历史。')
    reports, warnings = {}, []
    progress('正在读取实时额度…')
    observed = utcnow()
    try:
        raw = http_json('https://chatgpt.com/backend-api/wham/usage',headers={
            'Authorization':'Bearer '+tokens['access_token'],'ChatGPT-Account-Id':tokens['account_id']})
        reports['usage'], notes = convert_quota(raw,account,observed)
        warnings.extend(notes)
    except Exception as exc:
        warnings.append('实时额度：'+error_text(exc))
    if include_analytics:
        progress('正在读取 30 天分析和聊天排行，可能需要一些时间…')
        try:
            if login_fingerprint(load_login(home)) != identity:
                raise CollectorError('采集中登录账号发生变化，请重新采集')
            reports['analytics'], notes = collect_analytics(home,account)
            warnings.extend('分析：'+s for s in notes)
        except Exception as exc:
            warnings.append('分析：'+error_text(exc))
    if login_fingerprint(load_login(home)) != identity:
        raise CollectorError('采集中登录账号发生变化，本次数据已放弃，请重新采集')
    if not reports:
        raise CollectorError('；'.join(warnings) or '没有取得可上传的数据')
    return {'version':1,'account':account,'collected_at':observed,'identity':identity,
            'reports':reports,'warnings':warnings}


def check_service(base):
    base = normalize_url(base)
    try:
        data = http_json(base+'/healthz',timeout=12)
        if data.get('service')!='codex-quota-monitor' or data.get('status')!='ok':
            raise CollectorError('此地址没有返回额度服务的健康状态，请检查网址')
    except urllib.error.HTTPError as exc:
        raise CollectorError('服务连通检查返回 HTTP '+str(exc.code)+'；请检查网址和 Nginx 配置') from None
    return base


def upload_snapshot(snapshot, base, token, progress=lambda message:None, verify_service=True):
    if not token:
        raise CollectorError('请填写上传密钥 UPLOAD_TOKEN；它不是 OpenAI API Key 或网页只读密钥')
    base = check_service(base) if verify_service else normalize_url(base)
    results = {}
    for section, payload in snapshot['reports'].items():
        if section not in ('usage','analytics'):
            raise CollectorError('待上传文件包含未知类型')
        progress('正在上传'+('实时额度' if section=='usage' else '用量分析')+'…')
        try:
            response = http_json(base+'/api/'+section,token,payload)
            status = response.get('status')
            if status not in ('accepted','duplicate','out_of_order'):
                raise CollectorError('服务器未确认接收，保留原数据以便重试')
            old = status=='out_of_order' or any(r.get('status')=='out_of_order' for r in response.get('results',[]))
            results[section]={'ok':True,'status':'out_of_order' if old else status}
        except urllib.error.HTTPError as exc:
            hints={401:'上传密钥无效；请使用 UPLOAD_TOKEN',403:'访问被服务端拒绝',404:'接口不存在，请检查网址',
                   409:'上报内容冲突，请重新采集',413:'数据超过服务器大小限制',422:'数据格式未通过服务器校验'}
            results[section]={'ok':False,'error':hints.get(exc.code,'服务器返回 HTTP '+str(exc.code))}
        except Exception as exc:
            results[section]={'ok':False,'error':error_text(exc)}
    return results


class DATA_BLOB(ctypes.Structure):
    _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]


def crypt_secret(value, decrypt=False):
    if os.name!='nt':
        raise CollectorError('密钥加密保存仅支持 Windows')
    raw=base64.b64decode(value,validate=True) if decrypt else value.encode('utf8')
    buf=ctypes.create_string_buffer(raw)
    src=DATA_BLOB(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    dest=DATA_BLOB()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes=[ctypes.POINTER(DATA_BLOB),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(DATA_BLOB)]
    fn.restype=wintypes.BOOL
    if not fn(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dest)):
        raise CollectorError('Windows 无法'+('解密' if decrypt else '加密')+'上传密钥，请重新填写')
    try:
        result=ctypes.string_at(dest.pbData,dest.cbData)
        return result.decode('utf8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel.LocalFree(dest.pbData)


def atomic_json(path, value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def save_settings(path, settings):
    data={k:v for k,v in settings.items() if k not in ('token','secret')}
    if settings.get('remember') and settings.get('token'):
        data['secret']=crypt_secret(settings['token'])
    atomic_json(path,data)


def load_settings(path):
    if not Path(path).exists():
        return {}, None
    try:
        data=json.loads(Path(path).read_text(encoding='utf8'))
        warning=None
        try:
            data['token']=crypt_secret(data.pop('secret'),True) if data.get('secret') else ''
        except Exception:
            data.pop('secret',None)
            data['token']=''
            warning='保存的密钥无法解密（例如换了 Windows 用户），请重新填写上传密钥'
        return data,warning
    except (ValueError,OSError,AttributeError):
        return {},'设置文件无法读取，已使用默认值；原文件未删除'
