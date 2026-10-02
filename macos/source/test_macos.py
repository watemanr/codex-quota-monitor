import json
import tempfile
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'windows'/'source'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collector_core
from collect_licenses import is_license_file
import mac_settings
from mac_settings import MacSettingsStore
from mac_tray import MacStatusTray,menu_title
from quota_display import quota_tooltip

class MemoryKeychain:
    def __init__(self):self.items={};self.reads=[]
    def get(self,key):self.reads.append(key);return self.items.get(key)
    def set(self,key,value):self.items[key]=value
    def delete(self,key):self.items.pop(key,None)

class MacSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'settings.json'
        self.keychain=MemoryKeychain();self.store=MacSettingsStore(self.keychain)
        self.settings=dict(account='personal',server='https://quota.example.com',remember=True,token='test-upload-secret')
    def tearDown(self):self.tmp.cleanup()
    def test_secret_only_in_keychain_and_reused_without_creating_extra_items(self):
        self.store.save(self.path,self.settings)
        first=self.path.read_text();self.assertNotIn(self.settings['token'],first)
        loaded,warning=self.store.load(self.path)
        self.assertIsNone(warning);self.assertEqual(loaded['token'],self.settings['token'])
        self.store.save(self.path,self.settings)
        self.assertEqual(first,self.path.read_text());self.assertEqual(len(self.keychain.items),1)
    def test_rotating_secret_removes_old_item_after_settings_commit(self):
        self.store.save(self.path,self.settings);old=next(iter(self.keychain.items))
        self.settings['token']='replacement-upload-secret';self.store.save(self.path,self.settings)
        self.assertNotIn(old,self.keychain.items);self.assertEqual(len(self.keychain.items),1)
        self.assertEqual(self.store.load(self.path)[0]['token'],self.settings['token'])
    def test_forget_removes_key_and_reference(self):
        self.store.save(self.path,self.settings);self.settings['remember']=False
        self.store.save(self.path,self.settings)
        self.assertEqual(self.keychain.items,{})
        self.assertNotIn('keychain_ref',json.loads(self.path.read_text()))
        self.assertEqual(self.store.load(self.path)[0]['token'],'')
    def test_atomic_save_failure_preserves_original_key_and_settings(self):
        self.store.save(self.path,self.settings);original=self.path.read_text();items=dict(self.keychain.items)
        self.settings['token']='replacement-upload-secret'
        with patch.object(mac_settings,'atomic_json',side_effect=OSError('simulated disk full')):
            with self.assertRaises(OSError):self.store.save(self.path,self.settings)
        self.assertEqual(self.path.read_text(),original);self.assertEqual(self.keychain.items,items)
    def test_keychain_save_failure_never_falls_back_to_plaintext(self):
        with patch.object(self.keychain,'set',side_effect=collector_core.CollectorError('denied')):
            with self.assertRaises(collector_core.CollectorError):self.store.save(self.path,self.settings)
        self.assertFalse(self.path.exists())
    def test_other_profile_reference_is_not_read_or_deleted(self):
        self.store.save(self.path,self.settings);items=dict(self.keychain.items)
        other=self.path.with_name('other-settings.json');other.write_text(self.path.read_text())
        self.keychain.reads=[];loaded,warning=self.store.load(other)
        self.assertEqual(loaded['token'],'');self.assertTrue(warning);self.assertEqual(self.keychain.reads,[])
        self.store.save(other,dict(self.settings,remember=False))
        self.assertEqual(items,self.keychain.items)
    def test_missing_key_and_malformed_settings_give_recoverable_warning(self):
        self.store.save(self.path,self.settings);self.keychain.items.clear()
        self.assertTrue(self.store.load(self.path)[1])
        self.path.write_text('[]');self.assertTrue(self.store.load(self.path)[1])
    def test_legacy_plaintext_key_is_never_loaded(self):
        self.path.write_text(json.dumps(self.settings))
        self.assertEqual(self.store.load(self.path)[0]['token'],'')

class MacDisplayTests(unittest.TestCase):
    def test_weekly_preferred_and_missing_windows_not_fabricated(self):
        snapshot={'account':'one','reports':{'usage':{'windows':[{'window_minutes':300,'used_percent':25},{'window_minutes':10080,'used_percent':86}]}}}
        tooltip=quota_tooltip(snapshot)
        self.assertEqual(menu_title(tooltip),'14% W')
        self.assertIn('75%/5h',tooltip)
        snapshot['reports']['usage']['windows']=snapshot['reports']['usage']['windows'][:1]
        self.assertEqual(menu_title(quota_tooltip(snapshot)),'75% H')
        self.assertEqual(menu_title(quota_tooltip(None)),'Q')
    def test_mac_report_source(self):
        raw={'rate_limit':{'primary_window':{'limit_window_seconds':18000,'used_percent':12}}}
        with patch.object(collector_core.sys,'platform','darwin'):
            report,_=collector_core.convert_quota(raw,'a','2026-09-23T00:00:00Z')
        self.assertEqual(report['source'],'macos-collector')

class MacPackagingTests(unittest.TestCase):
    def test_only_text_license_files_are_collected(self):
        for name in ('LICENSE','LICENSE.txt','COPYING.md','license-apache.rst'):
            self.assertTrue(is_license_file(name),name)
        for name in ('copying.cpython-313-darwin.so','copying.cpython-313-darwin.so.yml',
                     'LICENSE.dll','license.json'):
            self.assertFalse(is_license_file(name),name)

class MacNotificationTests(unittest.TestCase):
    def tray(self):
        class Center:
            def requestAuthorizationWithOptions_completionHandler_(self,options,callback):self.callback=callback
        class Notifications:
            UNAuthorizationOptionAlert=1;UNAuthorizationOptionSound=2
        tray=MacStatusTray.__new__(MacStatusTray)
        tray.center=Center();tray.notifications=Notifications();tray.closed=False
        tray.lock=threading.Lock();tray.permission=None;tray.pending=[];tray.requesting=False
        tray.delivered=[];tray.fallback=[]
        tray._send=lambda title,text:tray.delivered.append((title,text))
        tray.callback=tray.fallback.append
        return tray
    def test_permission_grant_delivers_pending_notifications_once(self):
        tray=self.tray();self.assertTrue(tray.notify('one','a'));self.assertTrue(tray.notify('two','b'))
        self.assertEqual(tray.delivered,[])
        tray.center.callback(True,None)
        self.assertEqual(tray.delivered,[('one','a'),('two','b')]);self.assertEqual(tray.fallback,[])
        self.assertTrue(tray.notify('three','c'));self.assertEqual(len(tray.delivered),3)
    def test_permission_denial_routes_pending_to_main_thread_popup(self):
        tray=self.tray();tray.notify('one','a');tray.center.callback(False,None)
        self.assertEqual(tray.fallback,[('notification_fallback','one','a')])
        self.assertFalse(tray.notify('two','b'));self.assertEqual(tray.delivered,[])
    def test_quit_drops_pending_callback(self):
        tray=self.tray();tray.notify('one','a');tray.closed=True
        tray.center.callback(False,None)
        self.assertEqual(tray.fallback,[]);self.assertFalse(tray.notify('two','b'))

if __name__=='__main__':unittest.main()
