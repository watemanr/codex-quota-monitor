"""Persistent browser passwords and revocable server-side sessions."""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit
from flask import g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

COOKIE='codex_quota_session'

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def password_hash(value):return generate_password_hash(value,method='pbkdf2:sha256:600000')

def register_browser_auth(app,db):
    configured=app.config.get('WEB_PASSWORD','')
    configured_hash=app.config.get('WEB_PASSWORD_HASH','')
    if configured and not 12<=len(configured)<=256:
        raise RuntimeError('WEB_PASSWORD 必须为 12–256 个字符')
    fingerprint=digest('plain:'+configured) if configured else digest('hash:'+configured_hash) if configured_hash else None
    with db() as conn:
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS web_settings(id INTEGER PRIMARY KEY CHECK(id=1),
            password_hash TEXT NOT NULL,config_fingerprint TEXT NOT NULL,version TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS web_sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,
            expires_at REAL NOT NULL,password_version TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS web_sessions_expiry ON web_sessions(expires_at);
          CREATE TABLE IF NOT EXISTS web_login_attempts(client TEXT PRIMARY KEY,started REAL NOT NULL,attempts INTEGER NOT NULL);
        """)
        conn.execute('BEGIN IMMEDIATE')
        stored=conn.execute('SELECT * FROM web_settings WHERE id=1').fetchone()
        if not stored and not fingerprint:
            raise RuntimeError('首次启动请配置 WEB_PASSWORD 或 WEB_PASSWORD_HASH')
        # Only a changed deployment setting overrides a password saved in the browser.
        if fingerprint and (not stored or fingerprint!=stored['config_fingerprint']):
            active_hash=password_hash(configured) if configured else configured_hash
            conn.execute('INSERT INTO web_settings VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash,config_fingerprint=excluded.config_fingerprint,version=excluded.version',
                         (active_hash,fingerprint,digest(active_hash)))
            conn.execute('DELETE FROM web_sessions')
    ttl=app.config['WEB_SESSION_TTL_SECONDS']

    def active_settings(conn):return conn.execute('SELECT * FROM web_settings WHERE id=1').fetchone()

    def read_session():
        if hasattr(g,'quota_web_session'):return g.quota_web_session
        token=request.cookies.get(COOKIE,'');g.quota_web_session=None
        if not token or len(token)>128:return None
        with db() as conn:
            row=conn.execute('SELECT s.* FROM web_sessions s JOIN web_settings w ON w.id=1 AND s.password_version=w.version WHERE s.token_hash=? AND s.expires_at>?',
                             (digest(token),time.time())).fetchone()
        g.quota_web_session=dict(row) if row else None
        return g.quota_web_session

    def secure_request():
        return request.is_secure or request.headers.get('X-Forwarded-Proto','').split(',')[-1].strip()=='https'

    def same_origin():
        if request.headers.get('Sec-Fetch-Site')=='cross-site':return False
        origin=request.headers.get('Origin')
        if not origin:return True
        try:
            p=urlsplit(origin)
            expected=urlsplit(('https' if secure_request() else 'http')+'://'+request.host)
            port=lambda u:u.port or (443 if u.scheme=='https' else 80)
            return p.scheme==expected.scheme and p.hostname==expected.hostname and port(p)==port(expected) and not p.username and not p.password
        except ValueError:return False

    def clear_cookie(response):
        response.delete_cookie(COOKIE,path='/',secure=secure_request(),httponly=True,samesite='Lax')
        return response

    def csrf_valid(session):
        return hmac.compare_digest(request.headers.get('X-CSRF-Token','').encode(),session['csrf'].encode())

    def parse_body():
        if not request.is_json:return None,(jsonify(error='请使用 JSON 提交密码'),415)
        if request.content_length is not None and request.content_length>4096:return None,(jsonify(error='请求过大'),413)
        body=request.get_json()
        if not isinstance(body,dict):return None,(jsonify(error='请求格式不正确'),422)
        return body,None

    def attempt_limit(kind):
        now=time.time();period=300;window=int(now//period)*period
        client=digest(kind+':'+(request.remote_addr or 'unknown'))
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM web_login_attempts WHERE started<?',(window,))
            row=conn.execute('SELECT attempts FROM web_login_attempts WHERE client=?',(client,)).fetchone()
            if row and row['attempts']>=10:
                retry=max(1,int(window+period-now)+1)
                response=jsonify(error='尝试次数过多，请稍后再试',retry_after=retry)
                response.headers['Retry-After']=str(retry)
                return client,(response,429)
            conn.execute('INSERT INTO web_login_attempts VALUES(?,?,1) ON CONFLICT(client) DO UPDATE SET attempts=attempts+1',(client,window))
        return client,None

    @app.get('/api/session')
    def session_status():
        session=read_session()
        if not session:return jsonify(authenticated=False)
        return jsonify(authenticated=True,csrf_token=session['csrf'],expires_at=session['expires_at'])

    @app.post('/api/login')
    def login():
        if not same_origin():return jsonify(error='登录请求来源不正确，请从本站登录页操作'),403
        payload,error=parse_body()
        if error:return error
        password=payload.get('password')
        if not isinstance(password,str) or not 1<=len(password)<=256:return jsonify(error='请输入登录密码'),422
        client,error=attempt_limit('login')
        if error:return error
        with db() as conn:settings=dict(active_settings(conn))
        if not check_password_hash(settings['password_hash'],password):return jsonify(error='密码不正确'),401
        now=time.time();token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);expires=now+ttl
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if active_settings(conn)['version']!=settings['version']:
                return jsonify(error='密码刚刚被修改，请重新登录'),409
            conn.execute('DELETE FROM web_login_attempts WHERE client=?',(client,))
            conn.execute('DELETE FROM web_sessions WHERE expires_at<=? OR password_version<>?',(now,settings['version']))
            old=request.cookies.get(COOKIE,'')
            if old and len(old)<=128:conn.execute('DELETE FROM web_sessions WHERE token_hash=?',(digest(old),))
            conn.execute('INSERT INTO web_sessions VALUES(?,?,?,?)',(digest(token),csrf,expires,settings['version']))
        response=jsonify(authenticated=True,csrf_token=csrf,expires_at=expires)
        response.set_cookie(COOKIE,token,max_age=ttl,path='/',secure=secure_request(),httponly=True,samesite='Lax')
        return response

    @app.post('/api/password')
    def change_password():
        if not same_origin():return jsonify(error='改密请求来源不正确'),403
        session=read_session()
        if not session:return jsonify(error='请先登录网页'),401
        if not csrf_valid(session):return jsonify(error='登录状态已变化，请刷新后重试'),403
        payload,error=parse_body()
        if error:return error
        current=payload.get('current_password');new=payload.get('new_password');confirmation=payload.get('confirm_password')
        if not isinstance(current,str) or not 1<=len(current)<=256:return jsonify(error='请输入当前密码'),422
        if not isinstance(new,str) or not 12<=len(new)<=256:return jsonify(error='新密码必须为 12–256 个字符'),422
        if new!=confirmation:return jsonify(error='两次新密码输入不一致'),422
        client,error=attempt_limit('change')
        if error:return error
        with db() as conn:settings=dict(active_settings(conn))
        if settings['version']!=session['password_version']:return jsonify(error='请重新登录'),401
        if not check_password_hash(settings['password_hash'],current):return jsonify(error='当前密码不正确'),422
        updated_hash=password_hash(new)
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if active_settings(conn)['version']!=settings['version']:return jsonify(error='密码刚刚被修改，请重新登录'),409
            conn.execute('UPDATE web_settings SET password_hash=?,version=? WHERE id=1',(updated_hash,digest(updated_hash)))
            conn.execute('DELETE FROM web_sessions')
            conn.execute('DELETE FROM web_login_attempts WHERE client=?',(client,))
        return clear_cookie(jsonify(authenticated=False,message='密码已修改，请使用新密码重新登录'))

    @app.post('/api/logout')
    def logout():
        if not same_origin():return jsonify(error='退出请求来源不正确'),403
        session=read_session()
        if session:
            if not csrf_valid(session):return jsonify(error='登录状态已变化，请刷新后退出'),403
            with db() as conn:conn.execute('DELETE FROM web_sessions WHERE token_hash=?',(session['token_hash'],))
        return clear_cookie(jsonify(authenticated=False))

    def require_management_write():
        if not same_origin():
            return jsonify(error='操作请求来源不正确'), 403
        session = read_session()
        if not session:
            return jsonify(error='请先登录网页'), 401
        if not csrf_valid(session):
            return jsonify(error='登录状态已变化，请刷新后重试'), 403
        return None

    return lambda:read_session() is not None, require_management_write
