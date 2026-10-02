import copy
import tempfile
import time
from pathlib import Path
import unittest
from usage_monitor import UsageMonitor,notification_text


def snap(when,used,reset,minutes=300,account='a',identity='identity'):
    return dict(account=account,identity=identity,reports={'usage':dict(account=account,observed_at=when,
        windows=[dict(id=f'codex:{minutes}',window_minutes=minutes,used_percent=used,resets_at=reset)])})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'monitor.sqlite3'
        self.monitor=UsageMonitor(self.path)
        self.t=time.time()-10000
    def tearDown(self):self.tmp.cleanup()

    def test_depleted_once_until_recovery_and_restart(self):
        t=self.t
        self.assertEqual(self.monitor.observe(snap(t,99,t+9000)),[])
        events=self.monitor.observe(snap(t+60,100,t+9000))
        self.assertEqual([e['kind'] for e in events],['depleted'])
        reopened=UsageMonitor(self.path)
        self.assertEqual(reopened.observe(snap(t+60,100,t+9000)),[])
        self.assertEqual(reopened.observe(snap(t+120,100,t+9000)),[])
        reopened.observe(snap(t+180,98,t+9000))
        self.assertEqual(reopened.observe(snap(t+240,100,t+9000))[0]['kind'],'depleted')

    def test_first_observation_empty_not_reset(self):
        events=self.monitor.observe(snap(self.t,100,self.t+9000))
        self.assertEqual([e['kind'] for e in events],['depleted'])
        self.assertEqual(self.monitor.latest('a','identity'),{})
        self.assertEqual(self.monitor.observe(snap(self.t+60,100,self.t+9000)),[])

    def test_scheduled_and_unexpected_windows_independent(self):
        t=self.t
        self.monitor.observe(snap(t,95,t+1800))
        self.monitor.observe(snap(t,90,t+100000,10080))
        a=self.monitor.observe(snap(t+1900,5,t+19800))
        b=self.monitor.observe(snap(t+2000,0,t+600000,10080))
        self.assertEqual(a[0]['kind'],'scheduled_reset')
        self.assertEqual(b[0]['kind'],'unexpected_reset')
        latest=UsageMonitor(self.path).latest('a','identity')
        self.assertEqual(latest['codex:300']['window_type'],'5h')
        self.assertEqual(latest['codex:10080']['window_type'],'weekly')
        self.assertEqual(latest['codex:300']['detected_at'],t+1900)
        self.assertEqual(latest['codex:300']['expected_reset'],t+1800)
        title,body=notification_text(a+b)
        self.assertIn('5h',body);self.assertIn('weekly',body);self.assertIn('疑似突发重置',body)

    def test_isolated_accounts_and_logins(self):
        t=self.t
        self.monitor.observe(snap(t,95,t+9000))
        self.assertEqual(self.monitor.observe(snap(t+100,0,t+9000,account='b')),[])
        self.assertEqual(self.monitor.observe(snap(t+100,0,t+9000,identity='different')),[])
        self.assertEqual(self.monitor.latest('b','identity'),{})
        self.assertEqual(self.monitor.latest('a','different'),{})

    def test_late_duplicate_and_missing_usage_do_not_alert(self):
        t=self.t
        self.monitor.observe(snap(t,90,t+9000))
        self.assertEqual(self.monitor.observe(snap(t-60,0,t+9000)),[])
        self.assertEqual(self.monitor.observe(snap(t,0,t+9000)),[])
        missing={'account':'a','identity':'identity','reports':{'analytics':{}}}
        self.assertEqual(self.monitor.observe(missing),[])
        self.assertEqual(self.monitor.latest('a','identity'),{})

    def test_uncertain_not_promoted_to_reset(self):
        t=self.t
        self.monitor.observe(snap(t,90,None))
        events=self.monitor.observe(snap(t+60,0,None))
        self.assertEqual(events[0]['kind'],'uncertain')
        self.assertEqual(self.monitor.latest('a','identity'),{})

    def test_rollover_and_schedule_change(self):
        t=self.t
        self.monitor.observe(snap(t,50,t+1800))
        self.assertEqual(self.monitor.observe(snap(t+100,52,t+2100)),[])
        self.assertEqual(self.monitor.observe(snap(t+2200,53,t+20100))[0]['kind'],'scheduled_rollover')

    def test_remote_history_persists_without_replaying_alerts(self):
        t=self.t
        self.monitor.observe(snap(t,90,t+9000))
        self.monitor.observe(snap(t+100,0,t+10000))
        local=self.monitor.latest('a','identity')['codex:300']
        self.monitor.merge_remote('a','identity',{'codex:300':dict(local,detected_at=t+300,kind='scheduled_reset')})
        self.monitor.merge_remote('a','identity',{'codex:300':local})
        self.assertEqual(UsageMonitor(self.path).latest('a','identity')['codex:300']['detected_at'],t+300)
        self.assertEqual(self.monitor.observe(snap(t+400,10,t+10000)),[])

    def test_remote_invalid_data_not_kept(self):
        self.monitor.merge_remote('a','identity',{'codex:300':{'kind':'depleted','detected_at':self.t},
            'bad':{'kind':'scheduled_reset','detected_at':float('nan'),'window_minutes':300}})
        self.assertEqual(self.monitor.latest('a','identity'),{})

if __name__=='__main__':unittest.main()
