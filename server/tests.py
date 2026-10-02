import copy
import json
import os
import tempfile
import time
import unittest
import secrets
import hashlib
import sqlite3
from contextlib import closing
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from concurrent.futures import ThreadPoolExecutor

from app import create_app
from detection import classify, normalize
from bark import send_bark
from tibo import parse_feed, quota_related


TEST_PASSWORD=secrets.token_urlsafe(24)
TEST_PASSWORD_HASH=generate_password_hash(TEST_PASSWORD,method='pbkdf2:sha256:600000')

NOW = 1790040000.0


def snapshot(t=NOW, used=80, reset=NOW+7200, minutes=300):
    return dict(observed_at=t, used_percent=used, resets_at=reset, window_minutes=minutes)


class DetectionTests(unittest.TestCase):
    def kind(self, a, b):
        return classify(a, b)['kind']

    def test_first_sample(self):
        self.assertEqual(self.kind(None, snapshot()), 'baseline')

    def test_scheduled_reset(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+7300, 5, NOW+25300)), 'scheduled_reset')

    def test_early_reset(self):
        e=classify(snapshot(), snapshot(NOW+600, 0, NOW+18600))
        self.assertEqual(e['kind'], 'unexpected_reset')
        self.assertEqual(e['confidence'], 'high')

    def test_drop_without_deadline_shift_is_suspected(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+600, 4)), 'unexpected_reset')

    def test_small_rounding_drop(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+600, 79)), 'measurement_adjustment')

    def test_boundary_is_uncertain(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+7150, 0)), 'uncertain')

    def test_missing_deadline(self):
        self.assertEqual(self.kind(snapshot(reset=None), snapshot(NOW+600, 0)), 'uncertain')

    def test_stale_deadline(self):
        self.assertEqual(self.kind(snapshot(reset=NOW-1), snapshot(NOW+600, 0)), 'uncertain')

    def test_multiple_period_sampling_gap(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+40000, 0)), 'uncertain')

    def test_schedule_only_advance_early(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+600, 82, NOW+18000)), 'schedule_change')

    def test_idle_deadline_slide_does_not_spam_events(self):
        self.assertIsNone(classify(snapshot(used=0), snapshot(NOW+301, 0, NOW+7501)))

    def test_idle_deadline_crossing_still_marks_cycle(self):
        self.assertEqual(self.kind(snapshot(used=0, reset=NOW+300),
                                   snapshot(NOW+600, 0, NOW+18300)), 'scheduled_rollover')

    def test_schedule_only_crossing(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+7300, 82, NOW+25300)), 'scheduled_rollover')

    def test_consumption_is_not_event(self):
        self.assertIsNone(classify(snapshot(), snapshot(NOW+600, 90)))

    def test_configuration_change(self):
        self.assertEqual(self.kind(snapshot(), snapshot(NOW+600, 0, minutes=10080)), 'configuration_change')

    def test_duration_not_primary_slot_identifies_week(self):
        raw={'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':76,'windowDurationMins':10080,'resetsAt':NOW+50000},'secondary':None}}}
        windows=normalize(raw,NOW)['windows']
        self.assertEqual(len(windows),1)
        self.assertEqual(windows[0]['window_id'],'codex:10080')

    def test_null_usage_not_zero(self):
        with self.assertRaises(ValueError):
            normalize({'rateLimits':{'primary':{'usedPercent':None,'windowDurationMins':300}}},NOW)

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            normalize({'observed_at':'2026-09-22T10:00:00','windows':[]},NOW)

    def test_invalid_percentage_and_nan_rejected(self):
        for val in [float('nan'),float('inf'),101,-1,True,'80']:
            with self.assertRaises(ValueError):
                normalize({'windows':[{'window_minutes':300,'used_percent':val}]},NOW)


class APITests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.config={'TESTING':True,'DB_PATH':os.path.join(self.directory.name,'test.sqlite'),
                     'UPLOAD_TOKEN':'u'*40,'READ_TOKEN':'r'*40,'WEB_PASSWORD':'','WEB_PASSWORD_HASH':TEST_PASSWORD_HASH}
        self.app=create_app(self.config)
        self.client=self.app.test_client()
        self.upload={'Authorization':'Bearer '+'u'*40}
        self.read={'Authorization':'Bearer '+'r'*40}
        self.t=time.time()-8000

    def tearDown(self):
        self.directory.cleanup()

    def payload(self, offset=0, used=80):
        return {'account':'test','observed_at':self.t+offset,'report_id':str(offset),
                'windows':[{'id':'codex:300','window_minutes':300,'used_percent':used,'resets_at':self.t+7200}]}

    def send(self, data):
        return self.client.post('/api/usage',json=data,headers=self.upload)

    def dashboard(self):
        return self.client.get('/api/dashboard?account=test',headers=self.read).json

    def login(self,client=None,headers=None):
        return (client or self.client).post('/api/login',json={'password':TEST_PASSWORD},headers=headers or {})

    def test_browser_password_login_reads_all_views(self):
        self.send(self.payload())
        self.assertEqual(self.client.get('/api/session').json,{'authenticated':False})
        response=self.login()
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['authenticated'])
        self.assertTrue(self.client.get('/api/session').json['authenticated'])
        for path in ('/api/accounts','/api/overview','/api/dashboard?account=test','/api/analytics?account=test','/api/reset-status?account=test'):
            self.assertEqual(self.client.get(path).status_code,200,path)
        body=response.get_data(as_text=True)
        self.assertNotIn(TEST_PASSWORD,body)
        self.assertNotIn(self.config['UPLOAD_TOKEN'],body)
        self.assertNotIn(self.config['READ_TOKEN'],body)

    def test_browser_session_cannot_upload_without_key(self):
        self.login()
        self.assertEqual(self.client.post('/api/usage',json=self.payload()).status_code,401)
        self.assertEqual(self.client.post('/api/analytics',json={}).status_code,401)
        self.assertEqual(self.send(self.payload()).status_code,201)
        self.assertEqual(self.client.get('/api/accounts',headers={'Authorization':'Bearer incorrect'}).status_code,401)

    def test_wrong_password_and_validation(self):
        self.assertEqual(self.client.post('/api/login',json={'password':'not-the-password'}).status_code,401)
        self.assertFalse(self.client.get('/api/session').json['authenticated'])
        for payload in ({},{'password':None},{'password':123},{'password':''},{'password':'x'*257},[]):
            self.assertEqual(self.client.post('/api/login',json=payload).status_code,422)
        self.assertEqual(self.client.post('/api/login',data='password=x').status_code,415)
        self.assertEqual(self.client.post('/api/login',json={'password':'x'*5000}).status_code,413)

    def test_browser_logout_revokes_copied_cookie(self):
        response=self.login();token=self.client.get_cookie('codex_quota_session').value
        other=self.app.test_client();other.set_cookie('codex_quota_session',token)
        self.assertEqual(other.get('/api/accounts').status_code,200)
        self.assertEqual(self.client.post('/api/logout',headers={'X-CSRF-Token':response.json['csrf_token']}).status_code,200)
        self.assertEqual(other.get('/api/accounts').status_code,401)
        self.assertIsNone(self.client.get_cookie('codex_quota_session'))

    def test_csrf_and_cross_origin_rejected(self):
        self.assertEqual(self.login(headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.login(headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
        response=self.login()
        self.assertEqual(self.client.post('/api/logout').status_code,403)
        self.assertEqual(self.client.post('/api/logout',headers={'X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/api/logout',headers={'X-CSRF-Token':response.json['csrf_token'],'Origin':'https://evil.example'}).status_code,403)
        self.assertTrue(self.client.get('/api/session').json['authenticated'])

    def test_cookie_flags_and_https_proxy(self):
        response=self.login(headers={'Origin':'http://localhost'})
        header=response.headers['Set-Cookie']
        self.assertIn('HttpOnly',header);self.assertIn('SameSite=Lax',header)
        self.assertIn('Path=/',header);self.assertNotIn('; Secure',header)
        response=self.login(headers={'Origin':'https://localhost','X-Forwarded-Proto':'https'})
        self.assertEqual(response.status_code,200)
        self.assertIn('; Secure',response.headers['Set-Cookie'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_session_expiration_and_forged_cookie(self):
        response=self.login()
        with patch('browser_auth.time.time',return_value=response.json['expires_at']+1):
            self.assertEqual(self.client.get('/api/accounts').status_code,401)
        self.client.set_cookie('codex_quota_session','forged-value')
        self.assertFalse(self.client.get('/api/session').json['authenticated'])

    def test_session_survives_restart_and_password_change_revokes_it(self):
        self.login();token=self.client.get_cookie('codex_quota_session').value
        restarted=create_app(self.config).test_client();restarted.set_cookie('codex_quota_session',token)
        self.assertEqual(restarted.get('/api/accounts').status_code,200)
        changed=create_app(dict(self.config,WEB_PASSWORD_HASH=generate_password_hash(secrets.token_urlsafe(24)))).test_client()
        changed.set_cookie('codex_quota_session',token)
        self.assertEqual(changed.get('/api/accounts').status_code,401)

    def test_cookie_rotation_and_hash_storage(self):
        self.login();old=self.client.get_cookie('codex_quota_session').value
        self.login();new=self.client.get_cookie('codex_quota_session').value
        self.assertNotEqual(old,new)
        other=self.app.test_client();other.set_cookie('codex_quota_session',old)
        self.assertEqual(other.get('/api/accounts').status_code,401)
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            hashes=[r[0] for r in conn.execute('SELECT token_hash FROM web_sessions')]
        self.assertNotIn(new,hashes)
        self.assertIn(hashlib.sha256(new.encode()).hexdigest(),hashes)

    def test_login_rate_limit_and_retry_window(self):
        with patch('browser_auth.time.time',return_value=1800000010):
            for _ in range(10):
                self.assertEqual(self.client.post('/api/login',json={'password':'wrong'}).status_code,401)
            response=self.login();self.assertEqual(response.status_code,429)
            self.assertGreater(int(response.headers['Retry-After']),0)
        with patch('browser_auth.time.time',return_value=1800000311):
            self.assertEqual(self.login().status_code,200)

    def change(self,new,current=TEST_PASSWORD,client=None,csrf=None):
        client=client or self.client
        csrf=csrf or client.get('/api/session').json.get('csrf_token','')
        return client.post('/api/password',json=dict(current_password=current,new_password=new,confirm_password=new),headers={'X-CSRF-Token':csrf})

    def test_web_password_change_revokes_all_sessions_and_persists(self):
        self.login();other=self.app.test_client();self.login(other)
        new=secrets.token_urlsafe(30)
        response=self.change(new)
        self.assertEqual(response.status_code,200,response.json)
        self.assertIsNone(self.client.get_cookie('codex_quota_session'))
        self.assertEqual(other.get('/api/accounts').status_code,401)
        self.assertEqual(self.login().status_code,401)
        restarted=create_app(self.config).test_client()
        self.assertEqual(restarted.post('/api/login',json={'password':new}).status_code,200)
        self.assertEqual(self.send(self.payload()).status_code,201)
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            row=conn.execute('SELECT * FROM web_settings').fetchone()
            self.assertNotIn(new,row)
            self.assertNotIn(TEST_PASSWORD,row)

    def test_yaml_password_change_overrides_web_change_and_survives_restart(self):
        initial=secrets.token_urlsafe(36);updated=secrets.token_urlsafe(36);web=secrets.token_urlsafe(36)
        config=dict(self.config,WEB_PASSWORD=initial)
        client=create_app(config).test_client()
        self.assertEqual(client.post('/api/login',json={'password':initial}).status_code,200)
        self.assertEqual(self.change(web,current=initial,client=client).status_code,200)
        restarted=create_app(config).test_client()
        self.assertEqual(restarted.post('/api/login',json={'password':web}).status_code,200)
        changed=create_app(dict(config,WEB_PASSWORD=updated)).test_client()
        self.assertEqual(restarted.get('/api/accounts').status_code,401)
        self.assertEqual(changed.post('/api/login',json={'password':web}).status_code,401)
        self.assertEqual(changed.post('/api/login',json={'password':updated}).status_code,200)

    def test_existing_password_can_start_without_seed(self):
        client=create_app(dict(self.config,WEB_PASSWORD_HASH='')).test_client()
        self.assertEqual(self.login(client).status_code,200)

    def test_weak_configuration_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'12'):
            create_app(dict(self.config,WEB_PASSWORD='codex'))

    def test_password_change_requires_session_csrf_and_same_origin(self):
        new=secrets.token_urlsafe(36)
        self.assertEqual(self.change(new).status_code,401)
        self.login()
        self.assertEqual(self.change(new,csrf='wrong').status_code,403)
        csrf=self.client.get('/api/session').json['csrf_token']
        response=self.client.post('/api/password',json={},headers={'Origin':'https://evil.example','X-CSRF-Token':csrf})
        self.assertEqual(response.status_code,403)
        token_client=self.app.test_client()
        self.assertEqual(token_client.post('/api/password',json={},headers=self.upload).status_code,401)

    def test_password_change_validation_and_current_password(self):
        response=self.login();csrf={'X-CSRF-Token':response.json['csrf_token']}
        self.assertEqual(self.change('short').status_code,422)
        self.assertEqual(self.change('x'*257).status_code,422)
        self.assertEqual(self.change(secrets.token_urlsafe(36),current='wrong').status_code,422)
        self.assertEqual(self.client.post('/api/password',json=dict(current_password=TEST_PASSWORD,new_password='a'*20,confirm_password='b'*20),headers=csrf).status_code,422)
        self.assertEqual(self.login().status_code,200)

    def test_password_change_rate_limit(self):
        self.login()
        with patch('browser_auth.time.time',return_value=time.time()):
            for _ in range(10):self.assertEqual(self.change('some-new-password-123',current='wrong').status_code,422)
            self.assertEqual(self.change('some-new-password-123',current='wrong').status_code,429)

    def test_login_cannot_create_session_with_password_changed_mid_request(self):
        from browser_auth import check_password_hash as real_check
        def replace(password_hash,password):
            with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
                conn.execute("UPDATE web_settings SET version='concurrently-changed'")
                conn.commit()
            return real_check(password_hash,password)
        with patch('browser_auth.check_password_hash',side_effect=replace):
            self.assertEqual(self.login().status_code,409)
        self.assertFalse(self.client.get('/api/session').json['authenticated'])

    def test_no_default_password_without_explicit_hash(self):
        with self.assertRaisesRegex(RuntimeError,'WEB_PASSWORD_HASH'):
            create_app(dict(self.config,DB_PATH=os.path.join(self.directory.name,'fresh.sqlite'),WEB_PASSWORD_HASH=''))

    def test_tibo_feed_uses_authenticated_text_cache_and_strict_csp(self):
        response=self.client.get('/')
        page=response.get_data(as_text=True)
        self.assertIn('id="tibo-posts"',page)
        self.assertNotIn('id="tibo-feed"',page)
        self.assertNotIn('platform.twitter.com',response.headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/api/tibo').status_code,401)
        self.assertEqual(self.client.get('/api/tibo',headers=self.read).json['posts'],[])
        response.close()

    def test_tibo_cache_keeps_text_after_source_failure(self):
        sample={'id':'2103637477760311522','published_at':1790381233,
                'text':'We will reset usage limits',
                'url':'https://x.com/thsottiaux/status/2103637477760311522'}
        cache=self.app.extensions['tibo_cache']
        with patch('tibo.fetch_posts',return_value=[sample]):
            self.assertTrue(cache.refresh())
        fresh=self.client.get('/api/tibo',headers=self.read).json
        self.assertEqual(fresh['posts'][0]['text'],sample['text'])
        self.assertIsNotNone(fresh['fetched_at'])
        with patch('tibo.fetch_posts',side_effect=OSError('RSS unavailable')):
            with patch.object(self.app.logger,'warning'):
                self.assertFalse(cache.refresh())
        stale=self.client.get('/api/tibo',headers=self.read).json
        self.assertEqual(stale['posts'],fresh['posts'])
        self.assertEqual(stale['fetched_at'],fresh['fetched_at'])
        self.assertTrue(stale['last_error'])

    def test_tibo_rss_parser_checks_author_link_and_deduplicates(self):
        xml=(b'<?xml version="1.0"?><rss><channel><title>Tibo / @thsottiaux</title>'
             b'<item><title>Old update</title><link>https://x.noodl3.net/thsottiaux/status/2103375711797211460#m</link><pubDate>Fri, 25 Sep 2026 06:47:00 GMT</pubDate></item>'
             b'<item><title>  Reset   usage  </title><link>https://x.noodl3.net/thsottiaux/status/2103637477760311522#m</link><pubDate>Sat, 26 Sep 2026 00:07:13 GMT</pubDate></item>'
             b'<item><title>Reset usage</title><link>https://x.noodl3.net/thsottiaux/status/2103637477760311522#m</link><pubDate>Sat, 26 Sep 2026 00:07:13 GMT</pubDate></item>'
             b'<item><title>Other author</title><link>https://x.noodl3.net/other/status/2103637477760311523#m</link><pubDate>Sat, 26 Sep 2026 00:09:13 GMT</pubDate></item>'
             b'</channel></rss>')
        posts=parse_feed(xml)
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[0]['text'],'Reset usage')
        self.assertEqual(posts[0]['url'],'https://x.com/thsottiaux/status/2103637477760311522')
        with self.assertRaises(ValueError):
            parse_feed(xml.replace(b'@thsottiaux',b'@someoneelse'))

    def test_auth_and_read_only_separation(self):
        self.assertEqual(self.client.get('/healthz').status_code,200)
        self.assertEqual(self.client.get('/api/accounts').status_code,401)
        self.assertEqual(self.client.post('/api/usage',json=self.payload(),headers=self.read).status_code,401)
        self.assertEqual(self.client.get('/api/accounts',headers={'Authorization':'Bearer 中文'}).status_code,401)

    def test_end_to_end_unexpected_event(self):
        self.assertEqual(self.send(self.payload()).status_code,201)
        response=self.send(self.payload(600,0))
        self.assertEqual(response.json['results'][0]['event']['kind'],'unexpected_reset')
        data=self.dashboard()
        self.assertEqual(data['latest'][0]['remaining_percent'],100)
        self.assertEqual(data['counts']['unexpected_reset'],1)

    def test_idle_deadline_slide_hides_legacy_noise(self):
        first=self.payload(0,0)
        second=self.payload(301,0);second['windows'][0]['resets_at']=self.t+7501
        self.assertEqual(self.send(first).status_code,201)
        response=self.send(second)
        self.assertEqual(response.status_code,201)
        self.assertIsNone(response.json['results'][0]['event'])
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            sid=conn.execute('SELECT id FROM samples WHERE observed_at=?',(self.t+301,)).fetchone()[0]
            conn.execute('INSERT INTO events(sample_id,account,window_id,label,observed_at,interval_start,expected_reset,next_reset,before_used,after_used,kind,confidence,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (sid,'test','codex:300','5 小时额度',self.t+301,self.t,self.t+7200,self.t+7501,0,0,'schedule_change','low','legacy idle deadline'))
            conn.commit()
        dashboard=self.dashboard()
        overview=self.client.get('/api/overview',headers=self.read).json
        self.assertEqual(dashboard['counts'],{'baseline':1})
        self.assertEqual(overview['counts'],{'baseline':1})
        self.assertFalse(any(e['kind']=='schedule_change' for e in dashboard['events']))
        self.assertFalse(any(e['kind']=='schedule_change' for e in overview['events']))

    def test_last_reset_kept_outside_selected_range(self):
        self.t=time.time()-400000
        self.send(self.payload())
        self.send(self.payload(600,0))
        self.send(self.payload(399000,20))
        data=self.client.get('/api/dashboard?account=test&days=1',headers=self.read).json
        self.assertFalse(any(e['kind']=='unexpected_reset' for e in data['events']))
        last=data['latest'][0]['last_reset']
        self.assertEqual(last['kind'],'unexpected_reset')
        self.assertEqual(last['window_type'],'5h')
        self.assertEqual(last['detected_at'],self.t+600)
        overview=self.client.get('/api/overview?days=1',headers=self.read).json
        self.assertEqual(overview['accounts'][0]['latest'][0]['last_reset'],last)

    def test_reset_status_is_authenticated_and_isolated(self):
        self.send(self.payload());self.send(self.payload(600,0))
        self.assertEqual(self.client.get('/api/reset-status?account=test').status_code,401)
        data=self.client.get('/api/reset-status?account=test',headers=self.upload).json
        self.assertEqual(data['last_resets']['codex:300']['type_label'],'疑似突发重置')
        self.assertEqual(self.client.get('/api/reset-status?account=other',headers=self.read).json['last_resets'],{})

    def test_baseline_and_uncertain_are_not_last_reset(self):
        a=self.payload();a['windows'][0]['resets_at']=None;self.send(a)
        b=self.payload(600,0);b['windows'][0]['resets_at']=None;self.send(b)
        self.assertIsNone(self.dashboard()['latest'][0]['last_reset'])

    def test_weekly_and_five_hour_last_resets_are_separate(self):
        for minutes in (300,10080):
            a=self.payload();a['report_id']='base-'+str(minutes)
            a['windows'][0].update(id='codex:'+str(minutes),window_minutes=minutes)
            self.send(a)
            b=self.payload(7300 if minutes==300 else 600,0);b['report_id']='reset-'+str(minutes)
            b['windows'][0].update(id='codex:'+str(minutes),window_minutes=minutes)
            self.send(b)
        windows={w['window_minutes']:w for w in self.dashboard()['latest']}
        self.assertEqual(windows[300]['last_reset']['kind'],'scheduled_reset')
        self.assertEqual(windows[10080]['last_reset']['kind'],'unexpected_reset')
        self.assertEqual(windows[10080]['last_reset']['window_type'],'weekly')

    def test_idempotent_retries(self):
        self.send(self.payload())
        response=self.send(self.payload())
        self.assertEqual(response.json['status'],'duplicate')
        self.assertEqual(len(self.dashboard()['latest'][0]['history']),1)

    def test_idempotency_conflict(self):
        self.send(self.payload())
        self.assertEqual(self.send(self.payload(0,60)).status_code,409)
        self.assertEqual(self.dashboard()['latest'][0]['used_percent'],80)

    def test_late_sample_does_not_rewrite_latest(self):
        self.send(self.payload(600,0))
        response=self.send(self.payload(0,80))
        self.assertEqual(response.json['results'][0]['status'],'out_of_order')
        self.assertEqual(self.dashboard()['latest'][0]['used_percent'],0)
        self.assertEqual(self.dashboard()['counts'],{'baseline':1})

    def test_conflicting_batch_is_atomic(self):
        self.send(self.payload())
        p=self.payload(0,50);p['report_id']='different'
        p['windows'].insert(0,{'id':'codex:10080','window_minutes':10080,'used_percent':45})
        self.assertEqual(self.send(p).status_code,409)
        self.assertEqual(len(self.dashboard()['latest']),1)

    def test_multiple_accounts_do_not_mix(self):
        self.send(self.payload())
        p=self.payload(600,0);p['account']='another';self.send(p)
        self.assertEqual(self.dashboard()['counts'],{'baseline':1})

    def test_overview_auth_empty_and_invalid_range(self):
        self.assertEqual(self.client.get('/api/overview').status_code, 401)
        empty = self.client.get('/api/overview', headers=self.read).json
        self.assertEqual(empty['accounts'], [])
        self.assertEqual(empty['counts'], {})
        self.assertEqual(self.client.get('/api/overview?days=x', headers=self.read).status_code, 422)

    def test_overview_latest_and_counts_are_isolated(self):
        self.send(self.payload())
        self.send(self.payload(600, 0))
        other = self.payload(0, 40)
        other['account'] = 'second'
        other['windows'][0]['id'] = 'codex:10080'
        other['windows'][0]['window_minutes'] = 10080
        self.send(other)
        self.send(self.payload(-600, 90))
        result = self.client.get('/api/overview', headers=self.read).json
        by_account = {a['account']: a for a in result['accounts']}
        self.assertEqual(set(by_account), {'test', 'second'})
        self.assertEqual(by_account['test']['latest'][0]['remaining_percent'], 100)
        self.assertEqual(by_account['second']['latest'][0]['remaining_percent'], 60)
        self.assertEqual(by_account['test']['counts']['unexpected_reset'], 1)
        self.assertNotIn('unexpected_reset', by_account['second']['counts'])
        self.assertEqual(result['counts']['unexpected_reset'], 1)
        self.assertEqual({e['account'] for e in result['events']}, {'test', 'second'})
        self.assertNotIn('history', by_account['test']['latest'][0])

    def test_overview_range_filters_events_but_keeps_latest(self):
        old = self.payload(-200000, 70)
        self.send(old)
        result = self.client.get('/api/overview?days=1', headers=self.read).json
        self.assertEqual(len(result['accounts']), 1)
        self.assertEqual(result['accounts'][0]['latest'][0]['remaining_percent'], 30)
        self.assertTrue(result['accounts'][0]['latest'][0]['stale'])
        self.assertEqual(result['events'], [])
        self.assertEqual(result['counts'], {})

    def test_collector_upload_status_and_forecast_are_account_scoped(self):
        self.assertEqual(self.client.get('/api/forecast').status_code, 401)
        for offset, used in ((6600, 10), (7200, 20), (7800, 30)):
            item = self.payload(offset, used)
            item['source'] = 'windows-collector'
            item['windows'][0]['resets_at'] = self.t + 16000
            self.assertEqual(self.send(item).status_code, 201)
        manual = self.payload(0, 40)
        manual['account'] = 'manual'
        self.assertEqual(self.send(manual).status_code, 201)
        overview = self.client.get('/api/overview', headers=self.read).json
        statuses = {row['account']: row for row in overview['collectors']}
        self.assertEqual(statuses['test']['status'], 'recent')
        self.assertEqual(statuses['test']['upload_count'], 3)
        self.assertEqual(statuses['manual']['status'], 'never')
        forecast = self.client.get('/api/forecast?account=test', headers=self.read).json
        self.assertEqual([row['account'] for row in forecast['forecasts']], ['test'])
        window = forecast['forecasts'][0]['windows'][0]
        self.assertEqual(window['status'], 'before_reset')
        self.assertGreater(window['estimated_at'], window['observed_at'])
        self.assertLess(window['estimated_at'], window['resets_at'])
        self.assertEqual(self.client.get('/api/forecast', headers=self.read).status_code, 200)
        self.assertEqual(self.client.get('/api/dashboard?account=manual', headers=self.read).json['collectors'][0]['status'], 'never')

    def test_forecast_never_crosses_a_reset_or_claims_exhaustion_after_reset(self):
        from insights import estimate_window
        now = time.time()
        latest = dict(window_id='codex:10080', label='每周额度', window_minutes=10080,
                      observed_at=now, used_percent=44, resets_at=now+7200)
        slow = [dict(observed_at=now-7200, used_percent=40),
                dict(observed_at=now-3600, used_percent=42),
                dict(observed_at=now, used_percent=44)]
        self.assertEqual(estimate_window(slow, latest, now)['status'], 'after_reset')
        reset_segment = [dict(observed_at=now-10800, used_percent=90),
                         dict(observed_at=now-7200, used_percent=5),
                         dict(observed_at=now, used_percent=44)]
        self.assertEqual(estimate_window(reset_segment, latest, now)['status'], 'insufficient')
        self.assertEqual(estimate_window([], dict(latest, observed_at=now-4000, used_percent=100), now)['status'], 'stale')

    def test_forecast_stale_and_reset_timeline(self):
        self.send(self.payload(0, 80))
        self.send(self.payload(600, 0))
        dashboard = self.dashboard()
        self.assertEqual(dashboard['collectors'][0]['status'], 'never')
        self.assertTrue(any(e['kind'] == 'unexpected_reset' for e in dashboard['timeline_events']))
        forecast = self.client.get('/api/forecast?account=test', headers=self.read).json['forecasts'][0]['windows'][0]
        self.assertEqual(forecast['status'], 'stale')
        self.assertIsNone(forecast['estimated_at'])

    def analytics_payload(self, offset=0):
        import datetime
        day=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return {'account':'test','observed_at':self.t+offset,'report_id':'analytics-'+str(offset),'analytics':{
            'daily':{'units':'percent','data':[{'date':day,'attribution':[
                {'thread_source':'user','model':'m1','surface':'desktop_app','turn_trigger':'composer','value':80},
                {'thread_source':'guardian_review','model':'review','surface':'desktop_app','turn_trigger':'guardian_review','value':20}]}]},
            'periods':{'data_as_of':self.t+offset,'coverage_complete':False,'periods':[
                {'id':'week-1','window_minutes':10080,'starts_at':self.t-86400,'ends_at':self.t+6*86400,
                 'plan_type':'pro','used_basis_points':5577.5,'accounting_complete':False,
                 'breakdowns':[{'dimension':'thread_source','rows':[{'key':'user','basis_points':5500}]}]}]},
            'chats':{'data_as_of':self.t+offset,'rows':[
                {'thread_id':'known','title':'Known chat','data_status':'available','weekly_limit_percent':126.4,'balance_usage_credits':0,'groups':[]},
                {'thread_id':'unknown','title':'Unknown chat','data_status':'unavailable','weekly_limit_percent':0,'groups':[]} ]}}}

    def put_analytics(self, payload):
        return self.client.post('/api/analytics',json=payload,headers=self.upload)

    def get_analytics(self, query='account=test'):
        return self.client.get('/api/analytics?'+query,headers=self.read)

    def test_analytics_units_and_feature_shares(self):
        self.assertEqual(self.put_analytics(self.analytics_payload()).status_code,201)
        data=self.get_analytics().json
        self.assertEqual(data['daily']['total'],100)
        self.assertEqual(data['daily']['series'][0]['share'],.8)
        self.assertEqual(data['daily']['missing_days'],6)
        self.assertAlmostEqual(data['periods']['periods'][0]['used_percent'],55.775)
        self.assertEqual(data['periods']['periods'][0]['rows'][0]['percent'],55)

    def test_analytics_unknown_not_zero_and_lifetime_not_clamped(self):
        self.put_analytics(self.analytics_payload())
        rows=self.get_analytics().json['chats']['rows']
        self.assertEqual(rows[0]['weekly_limit_percent'],126.4)
        self.assertEqual(rows[0]['balance_usage_credits'],0)
        self.assertIsNone(rows[1]['weekly_limit_percent'])
        self.assertIsNone(rows[1]['balance_usage_credits'])

    def test_analytics_idempotency_and_out_of_order(self):
        p=self.analytics_payload();self.put_analytics(p)
        self.assertEqual(self.put_analytics(p).json['status'],'duplicate')
        p['analytics']['daily']['data'][0]['attribution'][0]['value']=1
        self.assertEqual(self.put_analytics(p).status_code,409)
        self.assertEqual(self.put_analytics(self.analytics_payload(-1)).json['status'],'out_of_order')
        self.assertEqual(self.get_analytics().json['daily']['total'],100)

    def test_analytics_merges_history_and_preserves_missing_sections(self):
        import datetime
        p=self.analytics_payload()
        old=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=10)).date().isoformat()
        p['analytics']['daily']['data'][0]['date']=old
        self.put_analytics(p)
        new=self.analytics_payload(600);del new['analytics']['chats'];del new['analytics']['periods']
        self.put_analytics(new)
        data=self.get_analytics('account=test&days=30').json
        self.assertEqual(data['daily']['total'],200)
        self.assertEqual(len(data['chats']['rows']),2)
        self.assertEqual(len(data['periods']['periods']),1)

    def test_analytics_account_discovery_and_isolation(self):
        self.put_analytics(self.analytics_payload())
        accounts=self.client.get('/api/accounts',headers=self.read).json['accounts']
        self.assertEqual(accounts[0]['account'],'test')
        overview=self.client.get('/api/overview',headers=self.read).json
        self.assertEqual(overview['accounts'][0]['account'],'test')
        self.assertEqual(overview['accounts'][0]['latest'],[])
        self.assertFalse(self.get_analytics('account=another').json['available'])

    def test_analytics_auth_invalid_and_empty(self):
        self.assertEqual(self.client.get('/api/analytics').status_code,401)
        self.assertEqual(self.client.post('/api/analytics',json=self.analytics_payload(),headers=self.read).status_code,401)
        self.assertEqual(self.get_analytics('account=test&days=6').status_code,422)
        self.assertFalse(self.get_analytics().json['available'])
        p=self.analytics_payload();p['analytics']['daily']['data'][0]['attribution'][0]['value']=-1
        self.assertEqual(self.put_analytics(p).status_code,422)
        self.assertFalse(self.get_analytics().json['available'])

    def test_analytics_missing_attribution_does_not_invent_features(self):
        p=self.analytics_payload();p['analytics']['daily']['data'][0]={'date':p['analytics']['daily']['data'][0]['date'],'models':[{'model':'m','credits':20}]}
        self.put_analytics(p)
        self.assertEqual(self.get_analytics().json['daily']['missing_days'],7)
        self.assertEqual(self.get_analytics('account=test&group_by=model').json['daily']['total'],20)

    def test_analytics_source_regression_does_not_overwrite_newer_period(self):
        self.put_analytics(self.analytics_payload())
        p=self.analytics_payload(600);p['analytics']['periods']['data_as_of']=self.t-1
        p['analytics']['periods']['periods'][0]['used_basis_points']=0
        self.put_analytics(p)
        self.assertAlmostEqual(self.get_analytics().json['periods']['periods'][0]['used_percent'],55.775)

    def test_unexpected_weekly_reset_splits_period_and_preserves_old_values(self):
        first=self.analytics_payload()
        self.assertEqual(self.put_analytics(first).status_code,201)
        base=self.payload()
        base['windows'][0].update(id='codex:10080',window_minutes=10080,
                                  resets_at=self.t+6*86400)
        self.send(base)
        reset=self.payload(600,0)
        reset['windows'][0].update(id='codex:10080',window_minutes=10080,
                                   resets_at=self.t+7*86400)
        self.assertEqual(self.send(reset).json['results'][0]['event']['kind'],'unexpected_reset')
        periods=self.get_analytics().json['periods']['periods']
        self.assertEqual(len(periods),2)
        newer,older=periods
        self.assertEqual((older['starts_at'],older['ends_at']), (self.t-86400,self.t+600))
        self.assertEqual((newer['starts_at'],newer['ends_at']), (self.t+600,self.t+7*86400))
        self.assertAlmostEqual(older['used_percent'],55.775)
        self.assertEqual(older['rows'][0]['percent'],55)
        self.assertIsNone(newer['used_percent'])
        self.assertEqual(newer['rows'],[])
        self.assertEqual(older['reset_at_end']['kind'],'unexpected_reset')
        self.assertTrue(older['reset_at_end']['estimated'])
        self.assertEqual(older['reset_at_end']['interval_start'],self.t)
        self.assertEqual(newer['reset_at_start']['boundary'],self.t+600)
        self.assertIsNone(self.get_analytics('account=other').json['periods'])

        # A later revision of the same raw period must not rewrite the closed cycle.
        revised=self.analytics_payload(700)
        revised['analytics']['periods']['periods'][0]['used_basis_points']=1234
        self.assertEqual(self.put_analytics(revised).status_code,201)
        periods=self.get_analytics().json['periods']['periods']
        self.assertAlmostEqual(periods[1]['used_percent'],55.775)
        self.assertIsNone(periods[0]['used_percent'])

        # Once the source reports a separate new period, its own values are shown.
        next_report=self.analytics_payload(800)
        next_report['analytics']['periods']['periods']=[{
            'id':'week-2','window_minutes':10080,'starts_at':self.t+600,
            'ends_at':self.t+7*86400,'plan_type':'pro','used_basis_points':500,
            'accounting_complete':True,
            'breakdowns':[{'dimension':'thread_source','rows':[{'key':'user','basis_points':500}]}]}]
        self.assertEqual(self.put_analytics(next_report).status_code,201)
        periods=self.get_analytics().json['periods']['periods']
        self.assertEqual(len(periods),2)
        self.assertEqual(periods[0]['starts_at'],self.t+600)
        self.assertEqual(periods[0]['used_percent'],5)
        self.assertEqual(periods[1]['ends_at'],self.t+600)
        self.assertAlmostEqual(periods[1]['used_percent'],55.775)
        reopened=create_app(self.config).test_client()
        persisted=reopened.get('/api/analytics?account=test',headers=self.read).json['periods']['periods']
        self.assertAlmostEqual(persisted[1]['used_percent'],55.775)

    def test_scheduled_reset_uses_expected_boundary(self):
        p=self.analytics_payload()
        item=p['analytics']['periods']['periods'][0]
        item.update(id='five-1',window_minutes=300,starts_at=self.t-17000,
                    ends_at=self.t+1000)
        self.put_analytics(p)
        before=self.payload()
        before['windows'][0]['resets_at']=self.t+1000
        self.send(before)
        after=self.payload(1200,0)
        after['windows'][0]['resets_at']=self.t+19000
        self.assertEqual(self.send(after).json['results'][0]['event']['kind'],'scheduled_reset')
        periods=self.get_analytics().json['periods']['periods']
        self.assertEqual((periods[1]['ends_at'],periods[0]['starts_at'],periods[0]['ends_at']),
                         (self.t+1000,self.t+1000,self.t+19000))
        self.assertEqual(periods[1]['reset_at_end']['kind'],'scheduled_reset')
        self.assertFalse(periods[1]['reset_at_end']['estimated'])
        self.assertAlmostEqual(periods[1]['used_percent'],55.775)
        self.assertIsNone(periods[0]['used_percent'])

    def manage(self, action, data, csrf):
        return self.client.post('/api/accounts/'+action,json=data,
                                headers={'X-CSRF-Token':csrf})

    def test_account_management_requires_browser_session_and_csrf(self):
        self.send(self.payload())
        self.assertEqual(self.client.get('/api/accounts/manage').status_code,401)
        request={'account':'test','new_name':'personal'}
        self.assertEqual(self.client.post('/api/accounts/rename',json=request,headers=self.upload).status_code,401)
        self.assertEqual(self.client.post('/api/accounts/release-alias',
                         json={'account':'personal','alias':'test'},headers=self.upload).status_code,401)
        csrf=self.login().json['csrf_token']
        self.assertEqual(self.client.post('/api/accounts/rename',json=request).status_code,403)
        self.assertEqual(self.client.post('/api/accounts/rename',json=request,
                         headers={'X-CSRF-Token':csrf,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/accounts/manage').json['accounts'][0]['account'],'test')
        self.assertEqual(self.client.get('/api/accounts').json['accounts'][0]['account'],'test')

    def test_account_rename_moves_all_history_and_routes_old_uploads(self):
        self.send(self.payload())
        self.assertEqual(self.put_analytics(self.analytics_payload()).status_code,201)
        csrf=self.login().json['csrf_token']
        response=self.manage('rename',{'account':'test','new_name':'personal'},csrf)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['moved']['reports'],1)
        names=[row['account'] for row in self.client.get('/api/accounts').json['accounts']]
        self.assertEqual(names,['personal'])
        self.assertEqual(len(self.client.get('/api/dashboard?account=personal').json['latest']),1)
        self.assertTrue(self.client.get('/api/analytics?account=personal').json['available'])
        for endpoint in ('dashboard','analytics','reset-status'):
            self.assertEqual(self.client.get('/api/'+endpoint+'?account=test').json['account'],'personal')
        self.assertEqual(self.send(self.payload()).json['status'],'duplicate')
        new=self.payload(600,70)
        self.assertEqual(self.send(new).json['account'],'personal')
        later=self.analytics_payload(700)
        self.assertEqual(self.put_analytics(later).json['account'],'personal')
        self.assertEqual(len(self.client.get('/api/dashboard?account=personal').json['latest'][0]['history']),2)
        listing=self.client.get('/api/accounts/manage').json['accounts']
        self.assertEqual(listing[0]['aliases'],['test'])
        self.assertEqual(self.manage('rename',{'account':'personal','new_name':'test'},csrf).status_code,200)
        self.assertEqual([row['account'] for row in self.client.get('/api/accounts').json['accounts']],
                         ['test'])
        self.assertEqual(self.client.get('/api/accounts/manage').json['accounts'][0]['aliases'],
                         ['personal'])
        self.assertEqual(self.client.get('/api/dashboard?account=personal').json['account'],'test')
        self.assertEqual(self.send(self.payload(800,65)|{'account':'personal'}).json['account'],'test')

    def test_account_alias_release_preserves_history_and_frees_name(self):
        self.send(self.payload())
        csrf=self.login().json['csrf_token']
        self.assertEqual(self.manage('rename',{'account':'test','new_name':'personal'},csrf).status_code,200)
        request={'account':'personal','alias':'test'}
        self.assertEqual(self.manage('release-alias',{'account':'wrong','alias':'test'},csrf).status_code,404)
        self.assertEqual(self.manage('release-alias',request,csrf).status_code,200)
        self.assertEqual(self.client.get('/api/accounts/manage').json['accounts'][0]['aliases'],[])
        self.assertEqual(self.client.get('/api/dashboard?account=personal').json['latest'][0]['history'][0]['used_percent'],80)
        self.assertEqual(self.manage('release-alias',request,csrf).status_code,404)
        self.assertEqual(self.send(self.payload(600,65)).json['account'],'test')
        self.assertEqual({row['account'] for row in self.client.get('/api/accounts').json['accounts']},
                         {'personal','test'})
        self.assertEqual(len(self.client.get('/api/dashboard?account=personal').json['latest'][0]['history']),1)
        self.assertEqual(len(self.client.get('/api/dashboard?account=test').json['latest'][0]['history']),1)

    def test_account_merge_combines_history_and_analysis_without_adding_percentages(self):
        source=self.payload()
        source['account']='source'
        self.send(source)
        import datetime
        older=self.analytics_payload()
        older['account']='source'
        previous=copy.deepcopy(older['analytics']['daily']['data'][0])
        previous['date']=(datetime.datetime.now(datetime.timezone.utc).date()-datetime.timedelta(days=1)).isoformat()
        older['analytics']['daily']['data'].append(previous)
        self.put_analytics(older)
        target=self.payload(600,60)
        target['account']='target'
        self.send(target)
        newer=self.analytics_payload(600)
        newer['account']='target'
        newer['analytics']['periods']['periods'][0].update(
            id='week-2',starts_at=self.t-8*86400,ends_at=self.t-86400)
        newer['analytics']['daily']['data'][0]['attribution'][0]['value']=40
        for row in newer['analytics']['chats']['rows']:
            row['thread_id'] += '-target'
        self.put_analytics(newer)
        csrf=self.login().json['csrf_token']
        self.assertEqual(self.manage('rename',{'account':'source','new_name':'renamed'},csrf).status_code,200)
        response=self.manage('merge',{'source':'renamed','target':'target'},csrf)
        self.assertEqual(response.status_code,200)
        self.assertEqual([r['account'] for r in self.client.get('/api/accounts').json['accounts']],['target'])
        self.assertEqual(len(self.client.get('/api/dashboard?account=target').json['latest'][0]['history']),2)
        analytics=self.client.get('/api/analytics?account=target&days=7').json
        self.assertEqual(analytics['daily']['total'],160)
        self.assertEqual(len(analytics['chats']['rows']),4)
        self.assertEqual(len(analytics['periods']['periods']),2)
        self.assertEqual(len(self.client.get('/api/accounts/manage').json['accounts'][0]['aliases']),2)
        self.assertEqual(self.send(self.payload(1200,50)|{'account':'source'}).json['account'],'target')

    def test_account_merge_conflict_is_atomic(self):
        self.send(self.payload()|{'account':'left'})
        self.send(self.payload(0,30)|{'account':'right','report_id':'right-report'})
        csrf=self.login().json['csrf_token']
        result=self.manage('merge',{'source':'left','target':'right'},csrf)
        self.assertEqual(result.status_code,409)
        self.assertIn('采样时间',result.json['error'])
        self.assertEqual({r['account'] for r in self.client.get('/api/accounts').json['accounts']},
                         {'left','right'})

    def test_account_merge_rejects_incompatible_analysis_units(self):
        left=self.analytics_payload()
        left['account']='left'
        self.put_analytics(left)
        right=self.analytics_payload(600)
        right['account']='right'
        right['analytics']['daily']['units']='credits'
        self.put_analytics(right)
        csrf=self.login().json['csrf_token']
        result=self.manage('merge',{'source':'left','target':'right'},csrf)
        self.assertEqual(result.status_code,409)
        self.assertIn('单位',result.json['error'])
        self.assertEqual({r['account'] for r in self.client.get('/api/accounts').json['accounts']},
                         {'left','right'})

    def test_account_merge_report_id_conflict_is_atomic(self):
        self.send(self.payload()|{'account':'left'})
        self.send(self.payload(600,50)|{'account':'right','report_id':'0'})
        csrf=self.login().json['csrf_token']
        result=self.manage('merge',{'source':'left','target':'right'},csrf)
        self.assertEqual(result.status_code,409)
        self.assertIn('上报 ID',result.json['error'])
        self.assertEqual({r['account'] for r in self.client.get('/api/accounts').json['accounts']},
                         {'left','right'})

    def test_account_delete_blocks_reuploads_and_unblock_allows_new_data(self):
        self.send(self.payload())
        self.put_analytics(self.analytics_payload())
        csrf=self.login().json['csrf_token']
        self.manage('rename',{'account':'test','new_name':'personal'},csrf)
        self.assertEqual(self.manage('delete',{'account':'personal','confirm_name':'wrong'},csrf).status_code,422)
        result=self.manage('delete',{'account':'personal','confirm_name':'personal'},csrf)
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.client.get('/api/accounts').json['accounts'],[])
        self.assertEqual(self.send(self.payload(700,50)).status_code,410)
        self.assertEqual(self.put_analytics(self.analytics_payload(700)).status_code,410)
        listing=self.client.get('/api/accounts/manage').json
        self.assertEqual(listing['deleted_names'],['personal','test'])
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            for table in ('reports','samples','events','analytics_state','analytics_reports','analytics_period_snapshots'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0,table)
        self.assertEqual(self.manage('unblock',{'account':'test'},csrf).status_code,200)
        self.assertEqual(self.send(self.payload(700,50)).status_code,201)
        self.assertEqual(self.client.get('/api/accounts').json['accounts'][0]['account'],'test')

    def test_restart_persistence(self):
        self.send(self.payload())
        other=create_app(self.config).test_client()
        result=other.get('/api/dashboard?account=test',headers=self.read).json
        self.assertEqual(result['latest'][0]['used_percent'],80)

    def test_concurrent_duplicate_requests(self):
        p=self.payload()
        def submit(_):
            with self.app.test_client() as client:
                return client.post('/api/usage',json=p,headers=self.upload).json['status']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(submit,range(8)))
        self.assertEqual(results.count('accepted'),1)
        self.assertEqual(results.count('duplicate'),7)

    def test_request_body_limit(self):
        result=self.client.post('/api/usage',data='x'*262145,content_type='application/json',headers=self.upload)
        self.assertEqual(result.status_code,413)

    def test_invalid_json(self):
        self.assertEqual(self.client.post('/api/usage',data='{',content_type='application/json',headers=self.upload).status_code,400)

    def test_future_time_and_invalid_days(self):
        p=self.payload();p['observed_at']=time.time()+10000
        self.assertEqual(self.send(p).status_code,422)
        self.assertEqual(self.client.get('/api/dashboard?account=test&days=x',headers=self.read).status_code,422)


class BarkTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.config={'TESTING':True,'DB_PATH':os.path.join(self.directory.name,'bark.sqlite'),
                     'UPLOAD_TOKEN':'u'*40,'READ_TOKEN':'r'*40,
                     'WEB_PASSWORD':'','WEB_PASSWORD_HASH':TEST_PASSWORD_HASH}
        self.app=create_app(self.config)
        self.client=self.app.test_client()
        self.upload={'Authorization':'Bearer '+'u'*40}
        self.t=time.time()-1000
        self.client.post('/api/usage',json=self.sample(0,90),headers=self.upload)
        self.csrf=self.client.post('/api/login',json={'password':TEST_PASSWORD}).json['csrf_token']

    def tearDown(self):
        self.directory.cleanup()

    def sample(self, offset, used, reset_offset=7200):
        return {'account':'test','observed_at':self.t+offset,'report_id':f'bark-{offset}',
                'windows':[{'id':'codex:300','window_minutes':300,
                            'used_percent':used,'resets_at':self.t+reset_offset}]}

    def save(self, **changes):
        data=dict(account='test',server_url='http://127.0.0.1:40011',
                  device_key='A'*24,group_name='Codex额度',enabled=True,
                  notify_5h_reset=True,notify_weekly_reset=True,
                  notify_5h_low=True,notify_weekly_low=True,
                  notify_tibo_posts=False,low_threshold=5,report_interval_minutes=0)
        data.update(changes)
        return self.client.post('/api/bark/settings',json=data,
                                headers={'X-CSRF-Token':self.csrf})

    def outbox(self):
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            return conn.execute('SELECT kind,status,attempts FROM bark_outbox ORDER BY id').fetchall()

    def test_bark_uses_post_json_without_key_in_url(self):
        seen=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,limit): return b'{"code":200}'
        class Opener:
            def open(self,request,timeout):
                seen.append((request,timeout))
                return Response()
        with patch('bark.urllib.request.build_opener',return_value=Opener()):
            send_bark({'server_url':'http://example.test:40011',
                       'device_key':'A'*24,'group_name':'Codex额度'},'标题','内容')
        request,timeout=seen[0]
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(request.full_url,'http://example.test:40011/push')
        self.assertNotIn('A'*24,request.full_url)
        self.assertEqual(json.loads(request.data)['device_key'],'A'*24)
        self.assertEqual(timeout,5)

    def test_tibo_quota_post_bootstrap_new_post_and_deduplication(self):
        self.assertTrue(quota_related('Global reset landing tomorrow for paid ChatGPT accounts.'))
        self.assertTrue(quota_related('Codex usage limits increase today.'))
        self.assertFalse(quota_related('GPT-6.1 Sol speed is improving after more capacity came online.'))
        self.assertEqual(self.save(notify_tibo_posts=True).status_code,200)
        cache=self.app.extensions['tibo_cache']
        now=time.time()
        def post(identifier,text,offset=0):
            return dict(id=identifier,published_at=now+offset,text=text,
                        url='https://x.com/thsottiaux/status/'+identifier)
        first=post('2105843926221660585','Global reset landing tomorrow for paid ChatGPT accounts.',-60)
        unrelated=post('2105843926221660586','Damn WiFi',-30)
        with patch('tibo.fetch_posts',return_value=[unrelated,first]):
            self.assertTrue(cache.refresh())
            self.assertTrue(cache.refresh())
        self.assertEqual([row[0] for row in self.outbox()],['tibo'])
        second=post('2105843926221660587','Codex usage limits increased today.',30)
        with patch('tibo.fetch_posts',return_value=[second,unrelated,first]):
            self.assertTrue(cache.refresh())
            self.assertTrue(cache.refresh())
        self.assertEqual([row[0] for row in self.outbox()],['tibo','tibo'])
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            bodies=[row[0] for row in conn.execute('SELECT body FROM bark_outbox ORDER BY id')]
        self.assertIn(first['url'],bodies[0])
        self.assertIn('北京时间',bodies[0])

    def test_tibo_alert_respects_switch_and_deduplicates_shared_bark_device(self):
        self.assertEqual(self.save(notify_tibo_posts=True).status_code,200)
        target=self.sample(100,70)
        target['account']='target'
        target['report_id']='target-base'
        self.client.post('/api/usage',json=target,headers=self.upload)
        self.assertEqual(self.save(account='target',notify_tibo_posts=True).status_code,200)
        cache=self.app.extensions['tibo_cache']
        post=dict(id='2105843926221660585',published_at=time.time()-60,
                  text='Global reset for paid ChatGPT accounts',
                  url='https://x.com/thsottiaux/status/2105843926221660585')
        with patch('tibo.fetch_posts',return_value=[post]):
            self.assertTrue(cache.refresh())
        self.assertEqual([row[0] for row in self.outbox()],['tibo'])
        # The alphabetically first account owns the shared-device queue item.
        self.assertEqual(self.save(account='target',notify_tibo_posts=False).status_code,200)
        self.assertEqual(self.outbox()[0][1],'canceled')
        sent=[]
        self.assertFalse(self.app.extensions['bark'].process_once(
            lambda setting,title,body: sent.append(body)))
        self.assertEqual(sent,[])

    def test_bark_existing_database_adds_tibo_switch_disabled(self):
        path=os.path.join(self.directory.name,'legacy.sqlite')
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('''CREATE TABLE bark_settings (
                account TEXT PRIMARY KEY,server_url TEXT NOT NULL,device_key TEXT NOT NULL,
                group_name TEXT NOT NULL,enabled INTEGER NOT NULL,notify_5h_reset INTEGER NOT NULL,
                notify_weekly_reset INTEGER NOT NULL,notify_5h_low INTEGER NOT NULL,
                notify_weekly_low INTEGER NOT NULL,low_threshold REAL NOT NULL,
                report_interval_minutes INTEGER NOT NULL,next_report_at REAL,updated_at REAL NOT NULL)''')
            conn.execute('INSERT INTO bark_settings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         ('test','http://127.0.0.1:40011','A'*24,'Codex额度',1,1,1,1,1,5,0,None,time.time()))
            conn.commit()
        create_app(dict(self.config,DB_PATH=path))
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('SELECT enabled,notify_tibo_posts FROM bark_settings').fetchone(),(1,0))

    def test_weekly_reset_alert_but_no_schedule_only_alert(self):
        self.assertEqual(self.save().status_code,200)
        weekly=self.sample(0,80)
        weekly['report_id']='weekly-base'
        weekly['windows'][0].update(id='codex:10080',window_minutes=10080)
        self.assertEqual(self.client.post('/api/usage',json=weekly,headers=self.upload).status_code,201)
        shifted=self.sample(100,82,18000)
        shifted['report_id']='weekly-shift'
        shifted['windows'][0].update(id='codex:10080',window_minutes=10080)
        self.client.post('/api/usage',json=shifted,headers=self.upload)
        self.assertEqual(self.outbox(),[])
        reset=self.sample(200,0,604800)
        reset['report_id']='weekly-reset'
        reset['windows'][0].update(id='codex:10080',window_minutes=10080)
        self.client.post('/api/usage',json=reset,headers=self.upload)
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            title=conn.execute('SELECT title FROM bark_outbox').fetchone()[0]
        self.assertIn('weekly',title)

    def test_settings_require_login_csrf_and_do_not_reveal_key(self):
        other=self.app.test_client()
        self.assertEqual(other.get('/api/bark/settings').status_code,401)
        self.assertEqual(other.post('/api/bark/settings',json={}).status_code,401)
        self.assertEqual(self.client.post('/api/bark/settings',json={}).status_code,403)
        response=self.save()
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('A'*24,response.get_data(as_text=True))
        listing=self.client.get('/api/bark/settings').get_data(as_text=True)
        self.assertNotIn('A'*24,listing)
        self.assertIn('key_configured',listing)
        self.assertEqual(self.save(device_key='').status_code,200)

    def test_low_crossing_reset_and_idempotency(self):
        self.assertEqual(self.save().status_code,200)
        self.assertEqual(self.client.post('/api/usage',json=self.sample(100,96),
                                          headers=self.upload).status_code,201)
        self.assertEqual(self.client.post('/api/usage',json=self.sample(100,96),
                                          headers=self.upload).json['status'],'duplicate')
        self.client.post('/api/usage',json=self.sample(200,97),headers=self.upload)
        reset=self.client.post('/api/usage',json=self.sample(300,0,18300),
                               headers=self.upload)
        self.assertEqual(reset.json['results'][0]['event']['kind'],'unexpected_reset')
        self.assertEqual([r[0] for r in self.outbox()],['low','reset'])
        sent=[]
        service=self.app.extensions['bark']
        self.assertTrue(service.process_once(lambda setting,title,body: sent.append((title,body))))
        self.assertTrue(service.process_once(lambda setting,title,body: sent.append((title,body))))
        self.assertFalse(service.process_once(lambda *_: None))
        self.assertIn('额度低',sent[0][0])
        self.assertIn('额度重置',sent[1][0])
        self.assertEqual([r[1] for r in self.outbox()],['sent','sent'])

    def test_low_threshold_rounding_jitter_does_not_repeat_alert(self):
        self.assertEqual(self.save().status_code,200)
        self.client.post('/api/usage',json=self.sample(100,96),headers=self.upload)
        self.client.post('/api/usage',json=self.sample(200,94.5),headers=self.upload)
        self.client.post('/api/usage',json=self.sample(300,96),headers=self.upload)
        self.assertEqual([row[0] for row in self.outbox()],['low'])

    def test_newly_enabled_low_quota_alerts_once_on_next_sample(self):
        self.client.post('/api/usage',json=self.sample(100,96),headers=self.upload)
        self.assertEqual(self.save().status_code,200)
        self.client.post('/api/usage',json=self.sample(200,97),headers=self.upload)
        self.client.post('/api/usage',json=self.sample(300,98),headers=self.upload)
        self.assertEqual([row[0] for row in self.outbox()],['low'])
        self.client.post('/api/usage',json=self.sample(400,0,18300),headers=self.upload)
        self.client.post('/api/usage',json=self.sample(500,96,18300),headers=self.upload)
        self.assertEqual([row[0] for row in self.outbox()],['low','reset','low'])

    def test_periodic_report_and_zero_disables_it(self):
        self.assertEqual(self.save(report_interval_minutes=5).status_code,200)
        service=self.app.extensions['bark']
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            due=conn.execute('SELECT next_report_at FROM bark_settings').fetchone()[0]
        self.assertEqual(service.schedule_reports_once(now=due-1),0)
        self.assertEqual(service.schedule_reports_once(now=due+1),1)
        self.assertEqual(service.schedule_reports_once(now=due+2),0)
        messages=[]
        self.assertTrue(service.process_once(lambda setting,title,body: messages.append(body),now=due+1))
        self.assertIn('采样',messages[0])
        self.assertEqual(self.save(device_key='',report_interval_minutes=0).status_code,200)
        self.assertEqual(service.schedule_reports_once(now=due+10000),0)

    def test_invalid_url_and_key_are_rejected(self):
        for address in ('file:///etc/passwd','http://user:pass@example.com',
                        'http://example.com/key/title/body','http://example.com/push',
                        'http://example.com?key=secret'):
            response=self.save(server_url=address)
            self.assertEqual(response.status_code,422,address)
        self.assertEqual(self.save(device_key='bad/key').status_code,422)
        self.assertEqual(self.save(report_interval_minutes=1).status_code,422)
        self.assertEqual(self.save(low_threshold=51).status_code,422)
        self.assertEqual(self.client.get('/api/bark/settings').json['settings'],{})

    def test_account_rename_and_delete_move_bark_data(self):
        self.assertEqual(self.save().status_code,200)
        self.client.post('/api/usage',json=self.sample(100,96),headers=self.upload)
        headers={'X-CSRF-Token':self.csrf}
        renamed=self.client.post('/api/accounts/rename',json={'account':'test','new_name':'renamed'},headers=headers)
        self.assertEqual(renamed.status_code,200,renamed.json)
        settings=self.client.get('/api/bark/settings').json['settings']
        self.assertIn('renamed',settings)
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            self.assertEqual(conn.execute('SELECT account FROM bark_outbox').fetchone()[0],'renamed')
        deleted=self.client.post('/api/accounts/delete',
                                 json={'account':'renamed','confirm_name':'renamed'},headers=headers)
        self.assertEqual(deleted.status_code,200,deleted.json)
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bark_settings').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bark_outbox').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bark_low_state').fetchone()[0],0)

    def test_merge_preserves_single_bark_config_and_rejects_two(self):
        target=self.sample(100,70)
        target['account']='target'
        target['report_id']='target-base'
        self.client.post('/api/usage',json=target,headers=self.upload)
        self.assertEqual(self.save().status_code,200)
        self.assertEqual(self.save(account='target',device_key='B'*24).status_code,200)
        headers={'X-CSRF-Token':self.csrf}
        conflict=self.client.post('/api/accounts/merge',
                                  json={'source':'test','target':'target'},headers=headers)
        self.assertEqual(conflict.status_code,409)
        self.assertIn('Bark',conflict.json['error'])
        self.assertEqual(self.client.post('/api/bark/remove',json={'account':'target'},
                                          headers=headers).status_code,200)
        merged=self.client.post('/api/accounts/merge',
                                json={'source':'test','target':'target'},headers=headers)
        self.assertEqual(merged.status_code,200,merged.json)
        settings=self.client.get('/api/bark/settings').json['settings']
        self.assertIn('target',settings)
        self.assertNotIn('test',settings)

    def test_failed_delivery_retries_without_losing_queue_item(self):
        self.assertEqual(self.save().status_code,200)
        self.client.post('/api/usage',json=self.sample(100,96),headers=self.upload)
        service=self.app.extensions['bark']
        def fail(*args):
            raise RuntimeError('Bark HTTP 503')
        self.assertTrue(service.process_once(fail))
        self.assertEqual(self.outbox()[0][:],('low','pending',1))
        with closing(sqlite3.connect(self.config['DB_PATH'])) as conn:
            due=conn.execute('SELECT next_attempt_at FROM bark_outbox').fetchone()[0]
        self.assertTrue(service.process_once(lambda *_: None,now=due+1))
        self.assertEqual(self.outbox()[0][:],('low','sent',2))

    def test_test_push_is_explicit_and_rate_limited(self):
        self.assertEqual(self.save(enabled=False).status_code,200)
        headers={'X-CSRF-Token':self.csrf}
        self.assertEqual(self.client.post('/api/bark/test',json={'account':'test'},headers=headers).status_code,200)
        self.assertEqual(self.client.post('/api/bark/test',json={'account':'test'},headers=headers).status_code,429)
        sent=[]
        self.assertTrue(self.app.extensions['bark'].process_once(
            lambda setting,title,body: sent.append(title)))
        self.assertEqual(sent,['Codex Bark 测试'])


if __name__=='__main__':
    unittest.main(verbosity=2)
