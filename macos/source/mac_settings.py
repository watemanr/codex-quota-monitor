"""Keep the upload key in macOS Keychain; JSON stores only an opaque reference."""
import hashlib
import json
from pathlib import Path
import uuid
from collector_core import CollectorError, atomic_json

SERVICE='xyz.814305.codex-quota-collector.upload'

class Keychain:
    def __init__(self):
        import Security
        self.api=Security

    def query(self,account):
        s=self.api
        return {s.kSecClass:s.kSecClassGenericPassword,s.kSecAttrService:SERVICE,s.kSecAttrAccount:account}

    def get(self,account):
        s=self.api;query=self.query(account)
        query.update({s.kSecReturnData:True,s.kSecMatchLimit:s.kSecMatchLimitOne})
        status,data=s.SecItemCopyMatching(query,None)
        if status==s.errSecItemNotFound:return None
        if status!=s.errSecSuccess:raise CollectorError('无法读取 macOS 钥匙串，请允许本程序访问已保存的上传密钥')
        return bytes(data).decode('utf8')

    def set(self,account,value):
        s=self.api;query=self.query(account);raw=value.encode('utf8')
        status=s.SecItemUpdate(query,{s.kSecValueData:raw})
        if status==s.errSecItemNotFound:
            status,_=s.SecItemAdd(query|{s.kSecValueData:raw},None)
        if status!=s.errSecSuccess:raise CollectorError('上传密钥未能保存到 macOS 钥匙串；可取消「记住密钥」后重试')

    def delete(self,account):
        s=self.api;status=s.SecItemDelete(self.query(account))
        if status not in (s.errSecSuccess,s.errSecItemNotFound):
            raise CollectorError('钥匙串中的旧上传密钥未能删除，请在「钥匙串访问」中移除本程序对应项目')

class MacSettingsStore:
    def __init__(self,keychain=None):self.keychain=keychain or Keychain()

    def prefix(self,path):
        return 'settings:'+hashlib.sha256(str(Path(path).expanduser().resolve()).encode()).hexdigest()[:24]+':'

    def read(self,path):
        value=json.loads(Path(path).read_text(encoding='utf8'))
        if not isinstance(value,dict):raise ValueError('Settings must be an object')
        return value

    def valid_ref(self,path,ref):return isinstance(ref,str) and ref.startswith(self.prefix(path)) and len(ref)<160

    def load(self,path):
        if not Path(path).exists():return {},None
        try:data=self.read(path)
        except (ValueError,OSError):return {},'设置文件无法读取，已使用默认值；原文件未删除'
        ref=data.pop('keychain_ref',None);data.pop('secret',None);data['token']='';warning=None
        if ref and data.get('remember'):
            try:
                if not self.valid_ref(path,ref):raise ValueError('Invalid keychain reference')
                value=self.keychain.get(ref)
                if value is None:raise ValueError('Missing keychain item')
                data['token']=value
            except Exception:warning='无法读取钥匙串中的上传密钥，请重新填写或允许钥匙串访问'
        return data,warning

    def save(self,path,settings):
        path=Path(path)
        try:old_ref=self.read(path).get('keychain_ref')
        except (ValueError,OSError):old_ref=None
        if not self.valid_ref(path,old_ref):old_ref=None
        data={k:v for k,v in settings.items() if k not in ('token','secret','keychain_ref')}
        new_ref=None;created=False
        if settings.get('remember') and settings.get('token'):
            token=settings['token']
            if old_ref and self.keychain.get(old_ref)==token:new_ref=old_ref
            else:
                new_ref=self.prefix(path)+uuid.uuid4().hex
                self.keychain.set(new_ref,token);created=True
            data['keychain_ref']=new_ref
        try:atomic_json(path,data)
        except Exception:
            if created:
                try:self.keychain.delete(new_ref)
                except Exception:pass
            raise
        if old_ref and old_ref!=new_ref:self.keychain.delete(old_ref)
