import os
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import urllib.error

import collector_core as c

NOW='2026-09-22T12:00:00+00:00'
RAW={'rate_limit':{'primary_window':{'used_percent':84,'limit_window_seconds':604800,'reset_at':1790410319},'secondary_window':None}}
TOKEN={'access_token':'secret-openai-token','account_id':'account-A'}

class CoreTests(unittest.TestCase):
    def test_weekly_primary_not_mislabelled(self):
        report,notes=c.convert_quota(RAW,'personal',NOW)
        self.assertEqual(report['windows'][0]['id'],'codex:10080')
        self.assertEqual(report['windows'][0]['used_percent'],84)
        self.assertEqual(len(report['windows']),1)
        self.assertFalse(notes)

    def test_two_windows_and_zero(self):
        raw=copy.deepcopy(RAW)
        raw['rate_limit']['secondary_window']={'used_percent':0,'limit_window_seconds':18000,'reset_at':1790100000}
        report,_=c.convert_quota(raw,'personal',NOW)
        self.assertEqual([w['window_minutes'] for w in report['windows']],[10080,300])
        self.assertEqual(report['windows'][1]['used_percent'],0)

    def test_missing_invalid_and_duplicate(self):
        for value in [None,True,-1,101,float('nan')]:
            raw=copy.deepcopy(RAW)
            raw['rate_limit']['primary_window']['used_percent']=value
            with self.assertRaises(c.CollectorError):c.convert_quota(raw,'a',NOW)
        raw=copy.deepcopy(RAW)
        raw['rate_limit']['secondary_window']=raw['rate_limit']['primary_window']
        with self.assertRaises(c.CollectorError):c.convert_quota(raw,'a',NOW)

    def test_missing_reset_not_guessed(self):
        raw=copy.deepcopy(RAW)
        raw['rate_limit']['primary_window']['reset_at']=None
        raw['rate_limit']['primary_window']['reset_after_seconds']=100
        report,_=c.convert_quota(raw,'a',NOW)
        self.assertIsNone(report['windows'][0]['resets_at'])

    def test_urls_and_prefix(self):
        self.assertEqual(c.normalize_url(' https://example.com/quota/ '),'https://example.com/quota')
        for value in ['', 'file:///tmp/a','https://user:password@example.com','https://example.com?q=x','http://x:bad','http://x/api/usage','http://x/../y','http://x\n']:
            # Surrounding whitespace is accepted; embedded CR/LF is not.
            if value=='http://x\n':continue
            with self.assertRaises(c.CollectorError,msg=value):c.normalize_url(value)

    def test_bound_account_refuses_changed_login(self):
        with patch.object(c,'load_login',return_value=TOKEN),patch.object(c,'http_json') as get:
            with self.assertRaises(c.CollectorError):c.collect_snapshot('.', 'a',False,'different')
            get.assert_not_called()

    def test_account_changes_during_capture(self):
        with patch.object(c,'load_login',side_effect=[TOKEN,dict(TOKEN,account_id='B')]),patch.object(c,'http_json',return_value=RAW):
            with self.assertRaises(c.CollectorError):c.collect_snapshot('.','a',False)

    def test_capture_no_credentials_exported(self):
        with patch.object(c,'load_login',return_value=TOKEN),patch.object(c,'http_json',return_value=RAW):
            snap=c.collect_snapshot('.','a',False)
        encoded=json.dumps(snap)
        self.assertNotIn(TOKEN['access_token'],encoded)
        self.assertNotIn(TOKEN['account_id'],encoded)
        self.assertEqual(list(snap['reports']),['usage'])

    def test_partial_analytics_keeps_usage(self):
        with patch.object(c,'load_login',return_value=TOKEN),patch.object(c,'http_json',return_value=RAW),patch.object(c,'collect_analytics',side_effect=TimeoutError()):
            snap=c.collect_snapshot('.','a',True)
        self.assertIn('usage',snap['reports'])
        self.assertEqual(len(snap['warnings']),1)

    @unittest.skipUnless(os.name=='nt','Windows DPAPI round-trip')
    def test_dpapi_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json'
            settings={'server':'https://example.com','account':'工作','token':'dummy-upload-secret','remember':True,'bindings':{'工作':'hash'}}
            c.save_settings(path,settings)
            self.assertNotIn(settings['token'],path.read_text())
            loaded,warning=c.load_settings(path)
            self.assertEqual(loaded['token'],settings['token'])
            self.assertIsNone(warning)
            settings['remember']=False
            c.save_settings(path,settings)
            self.assertNotIn('secret',json.loads(path.read_text()))
            self.assertEqual(c.load_settings(path)[0]['token'],'')

    def test_invalid_settings_and_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'settings.json'
            path.write_text('invalid')
            self.assertIsNotNone(c.load_settings(path)[1])
            with self.assertRaises(c.CollectorError):c.load_login(tmp)
            (Path(tmp)/'auth.json').write_text(json.dumps({'OPENAI_API_KEY':'not-a-chatgpt-login'}))
            with self.assertRaises(c.CollectorError):c.load_login(tmp)


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests=[]
        cls.mode='ok'
        cls.seen=set()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def respond(self,data,status=200):
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            def do_GET(self):
                if self.path=='/prefix/healthz':self.respond({'service':'codex-quota-monitor','status':'ok'})
                elif self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/stolen');self.end_headers()
                else:
                    cls.requests.append(('unexpected',self.path,self.headers.get('Authorization')))
                    self.respond({},404)
            def do_POST(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
                payload=json.loads(raw)
                cls.requests.append((self.path,payload,self.headers.get('Authorization')))
                if self.headers.get('Authorization')!='Bearer test-upload-key':return self.respond({},401)
                if cls.mode=='partial' and self.path.endswith('analytics'):return self.respond({},503)
                if cls.mode=='unknown':return self.respond({'message':'ok'})
                if cls.mode=='old':return self.respond({'status':'out_of_order'})
                rid=payload['report_id']
                duplicate=rid in cls.seen
                cls.seen.add(rid)
                self.respond({'status':'duplicate' if duplicate else 'accepted'},200 if duplicate else 201)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        type(self).requests.clear();type(self).mode='ok'
        report,_=c.convert_quota(RAW,'a',NOW)
        self.snap={'reports':{'usage':report,'analytics':{'account':'a','report_id':report['report_id']+'-analytics','analytics':{}}}}

    def test_upload_and_idempotent_retry(self):
        original=copy.deepcopy(self.snap)
        first=c.upload_snapshot(self.snap,self.base+'/prefix','test-upload-key')
        again=c.upload_snapshot(self.snap,self.base+'/prefix','test-upload-key')
        self.assertTrue(all(r['ok'] for r in first.values()))
        self.assertTrue(all(r['status']=='duplicate' for r in again.values()))
        self.assertEqual(self.snap,original)
        self.assertTrue(all(r[2]=='Bearer test-upload-key' for r in self.requests))

    def test_partial_failure_and_bad_key(self):
        type(self).mode='partial'
        result=c.upload_snapshot(self.snap,self.base+'/prefix','test-upload-key')
        self.assertTrue(result['usage']['ok'])
        self.assertFalse(result['analytics']['ok'])
        result=c.upload_snapshot(self.snap,self.base+'/prefix','wrong')
        self.assertTrue(all(not r['ok'] for r in result.values()))

    def test_no_false_success(self):
        type(self).mode='unknown'
        result=c.upload_snapshot(self.snap,self.base+'/prefix','test-upload-key')
        self.assertTrue(all(not r['ok'] for r in result.values()))
        type(self).mode='old'
        result=c.upload_snapshot(self.snap,self.base+'/prefix','test-upload-key')
        self.assertTrue(all(r['status']=='out_of_order' for r in result.values()))

    def test_redirect_never_forwards_key(self):
        with self.assertRaises(urllib.error.HTTPError):c.http_json(self.base+'/redirect','test-upload-key')
        self.assertFalse(self.requests)

    def test_payload_limit_and_token_newline(self):
        for payload,token in [({'large':'x'*300000},'test-upload-key'),({},'bad\nkey')]:
            with self.assertRaises(c.CollectorError):c.http_json(self.base+'/prefix/api/usage',token,payload)
        self.assertFalse(self.requests)


if __name__=='__main__':unittest.main(verbosity=2)
