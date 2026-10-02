import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automatic import IntervalSchedule,Outbox,flush_outbox,interval_minutes
from collector_core import CollectorError

BASE='https://quota.example.com'


def snapshot(rid='one',account='a',observed='2026-09-22T12:00:00+00:00'):
    common={'account':account,'observed_at':observed}
    return {'account':account,'identity':'fingerprint','collected_at':observed,'warnings':[],
            'reports':{'usage':dict(common,report_id=rid+'-u',windows=[{'used_percent':25}]),
                       'analytics':dict(common,report_id=rid+'-a',analytics={'daily':{}})}}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.s=IntervalSchedule(lambda:self.now)

    def test_independent_deadlines(self):
        self.s.start(5,10)
        self.assertEqual(self.s.due(),'read')
        self.s.begin('read');self.now+=20;self.s.finish()
        self.assertEqual(self.s.next_at,{'read':1320,'upload':1600})
        self.now=1320;self.assertEqual(self.s.due(),'read')
        self.s.begin('read');self.s.finish()
        self.now=1600;self.assertEqual(self.s.due(),'upload')

    def test_no_overlapping_or_catchup_storm(self):
        self.s.start(1,2)
        self.s.begin('read')
        self.now+=7200
        self.assertIsNone(self.s.due())
        self.s.finish()
        self.assertEqual(self.s.due(),'upload')
        self.s.begin('upload');self.s.finish()
        self.assertIsNone(self.s.due())
        self.assertEqual(self.s.next_at['read'],self.now+60)

    def test_stop_during_request_does_not_restart(self):
        self.s.start(5,10);self.s.begin('read');self.s.stop();self.s.finish()
        self.assertFalse(self.s.active)
        self.assertEqual(self.s.next_at,{})
        self.assertIsNone(self.s.due())

    def test_read_only_upload_only_and_validation(self):
        self.s.start(5,10,True,False)
        self.assertEqual(set(self.s.next_at),{'read'})
        self.s.stop();self.s.start(5,10,False,True)
        self.assertIsNone(self.s.due())
        self.now+=600;self.assertEqual(self.s.due(),'upload')
        self.s.start(5,10,False,False)
        self.assertFalse(self.s.active)
        for value in [-1,1441,'1.5','',True,'nan']:
            with self.assertRaises(CollectorError):interval_minutes(value)
        self.assertEqual(interval_minutes('1440'),1440)
        self.assertEqual(interval_minutes('0'),0)

    def test_zero_read_only_upload_runs(self):
        self.s.start(0,2)
        self.assertEqual(self.s.seconds,{'upload':120})
        self.assertIsNone(self.s.due())
        self.now+=120
        self.assertEqual(self.s.due(),'upload')
        self.s.begin('upload');self.s.finish()
        self.assertEqual(self.s.next_at,{'upload':self.now+120})

    def test_zero_upload_only_read_runs(self):
        self.s.start(2,0)
        self.assertEqual(self.s.seconds,{'read':120})
        self.assertEqual(self.s.due(),'read')
        self.s.begin('read');self.s.finish()
        self.assertIsNone(self.s.due())
        self.now+=120
        self.assertEqual(self.s.due(),'read')
        self.assertNotIn('upload',self.s.next_at)

    def test_both_zero_stays_idle_and_can_reenable(self):
        self.s.start(1,1)
        self.s.stop();self.s.start(0,0)
        self.assertFalse(self.s.active)
        self.assertEqual(self.s.seconds,{})
        self.assertEqual(self.s.next_at,{})
        self.now+=100000
        self.assertIsNone(self.s.due())
        self.s.start(1,0)
        self.assertTrue(self.s.active)
        self.assertEqual(self.s.due(),'read')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'pending.sqlite3'
        self.q=Outbox(self.path)

    def tearDown(self):self.temp.cleanup()

    def send(self,outcome=None):
        return patch('automatic.upload_snapshot',side_effect=outcome or (lambda snap,*a,**k:{kind:{'ok':True,'status':'accepted'} for kind in snap['reports']}))

    def test_persistent_and_chronological(self):
        self.q.add(snapshot('later',observed='2026-09-22T12:10:00+00:00'),BASE)
        self.q.add(snapshot('earlier'),BASE)
        reopened=Outbox(self.path)
        self.assertEqual(reopened.scope('a',BASE),(4,0))
        self.assertEqual([p['report_id'] for _,p in reopened.entries('a',BASE,'usage')],['earlier-u','later-u'])

    def test_idempotent_enqueue_and_conflict_atomicity(self):
        sample=snapshot()
        self.assertEqual(self.q.add(sample,BASE),2)
        self.assertEqual(self.q.add(sample,BASE),0)
        changed=copy.deepcopy(sample);changed['reports']['analytics']['analytics']={'different':True}
        with self.assertRaises(CollectorError):self.q.add(changed,BASE)
        self.assertEqual(self.q.scope('a',BASE),(2,0))

    def test_account_and_destination_isolation(self):
        self.q.add(snapshot('a'),BASE)
        self.q.add(snapshot('b','b'),BASE)
        self.q.add(snapshot('other-site'),'https://other.example.com')
        self.q.add(snapshot('unbound'),'')
        with patch('automatic.check_service'),self.send():
            result=flush_outbox(self.q,'a',BASE,'key')
        self.assertEqual(result['sent'],4)
        self.assertEqual(result['remaining'],0)
        self.assertEqual(self.q.scope('b',BASE)[0],2)
        self.assertEqual(self.q.scope('a','https://other.example.com')[0],2)

    def test_empty_queue_no_network(self):
        with patch('automatic.check_service') as check,self.send() as send:
            result=flush_outbox(self.q,'a',BASE,'key')
        check.assert_not_called();send.assert_not_called()
        self.assertEqual(result['sent'],0)

    def test_network_failure_preserves_unbound_destination(self):
        self.q.add(snapshot(),'')
        with patch('automatic.check_service',side_effect=CollectorError('offline')):
            with self.assertRaises(CollectorError):flush_outbox(self.q,'a',BASE,'key')
        self.assertEqual(self.q.scope('a','')[0],2)

    def test_partial_failure_blocks_newer_of_same_kind(self):
        self.q.add(snapshot('older'),BASE)
        self.q.add(snapshot('newer',observed='2026-09-22T12:10:00+00:00'),BASE)
        sent=[]
        def fake(snap,*args,**kwargs):
            kind,payload=next(iter(snap['reports'].items()));sent.append(payload['report_id'])
            return {kind:{'ok':False,'error':'offline'} if kind=='usage' else {'ok':True,'status':'accepted'}}
        with patch('automatic.check_service'),self.send(fake):result=flush_outbox(self.q,'a',BASE,'key')
        self.assertEqual(sent,['older-u','older-a','newer-a'])
        self.assertEqual(result['remaining'],2)
        self.assertEqual(result['sent'],2)

    def test_unacknowledged_retry_uses_same_id_and_time(self):
        sample=snapshot();self.q.add(sample,BASE)
        calls=[]
        def fake(snap,*args,**kwargs):
            kind,payload=next(iter(snap['reports'].items()));calls.append(payload)
            return {kind:{'ok':True,'status':'duplicate'}}
        with patch('automatic.check_service'),self.send(fake),patch.object(self.q,'acknowledge',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):flush_outbox(self.q,'a',BASE,'key')
        with patch('automatic.check_service'),self.send(fake):result=flush_outbox(self.q,'a',BASE,'key')
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(result['duplicate'],2)
        self.assertEqual(result['remaining'],0)

    def test_legacy_migration_only_once_after_upload(self):
        sample=snapshot()
        self.assertEqual(self.q.migrate_legacy(sample,BASE),2)
        with patch('automatic.check_service'),self.send():flush_outbox(self.q,'a',BASE,'key')
        reopened=Outbox(self.path)
        self.assertEqual(reopened.migrate_legacy(sample,BASE),0)
        self.assertEqual(reopened.scope('a',BASE),(0,0))

    def test_capacity_preserves_existing_rows(self):
        self.q.add(snapshot(),BASE)
        self.q.MAX_ROWS=3
        with self.assertRaises(CollectorError):self.q.add(snapshot('second'),BASE)
        self.assertEqual(self.q.scope('a',BASE),(2,0))

    def test_wrong_account_rejected_without_partial_insert(self):
        sample=snapshot();sample['reports']['analytics']['account']='different'
        with self.assertRaises(CollectorError):self.q.add(sample,BASE)
        self.assertEqual(self.q.scope('a',BASE),(0,0))

    def test_old_reports_are_acknowledged_and_labelled(self):
        self.q.add(snapshot(),BASE)
        with patch('automatic.check_service'),self.send(lambda snap,*a,**k:{kind:{'ok':True,'status':'out_of_order'} for kind in snap['reports']}):
            result=flush_outbox(self.q,'a',BASE,'key')
        self.assertEqual(result['stale'],2)
        self.assertEqual(result['remaining'],0)

    def test_time_budget_keeps_unsent_for_next_round(self):
        self.q.add(snapshot(),BASE)
        with patch('automatic.check_service'),self.send() as send:
            result=flush_outbox(self.q,'a',BASE,'key',max_seconds=0)
        send.assert_not_called()
        self.assertEqual(result['remaining'],2)


if __name__=='__main__':unittest.main(verbosity=2)
