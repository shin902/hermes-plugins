import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('tested_mail',Path(__file__).with_name('__init__.py'),submodule_search_locations=[str(Path(__file__).parent)])
assert spec and spec.loader
pkg=importlib.util.module_from_spec(spec); sys.modules[spec.name]=pkg; spec.loader.exec_module(pkg)
from tested_mail import runtime as r

class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']); self.home=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def seed(self):
        def fetch(home,path):
            if 'mailFolders' in path: return {'value':[{'id':'test-mail','subject':'test','receivedDateTime':'2026-01-01','from':{}}]}
            return {'body':{'contentType':'text','content':'fixture'}}
        r.collect(self.home,fetch=fetch)
        return json.loads(r.prepare(self.home))['batch_id']
    def pub(self,b,sender=None):
        return r.publish(self.home,b,'テストの要約','fixture-run','discord:123',sender=sender or (lambda *a:{'success':True,'message_id':'123456'}),thread_resolver=lambda *a:'456',notifications=True)
    def test_empty(self): self.assertEqual(r.prepare(self.home),r.EMPTY)
    def test_retry_nonce(self):
        self.seed(); a=r.prepare(self.home); b=r.prepare(self.home); self.assertNotEqual(a,b); self.assertEqual(json.loads(a)['batch_id'],json.loads(b)['batch_id'])
    def test_publish_defaults_unread(self):
        b=self.seed(); result=self.pub(b)
        self.assertFalse(result['read'])
        self.assertEqual(r.prepare(self.home),r.EMPTY)
        retry=self.pub(b); self.assertTrue(retry['already_delivered']); self.assertFalse(retry['read'])
        with r.locked(self.home) as (_,state):
            receipt=state['delivered']['test-mail']
            self.assertIs(receipt['read'],False)
            self.assertIs(receipt['acknowledged'],False)
            record=receipt['record']
            self.assertEqual(record['summary'],'テストの要約')
            self.assertEqual(record['mail']['body'],'fixture')
    def test_full_body_preserved(self):
        self.seed()
        with r.locked(self.home) as (path,state):
            state['current']['mail']['body']='x'*25000
            r.write(path,state)
        prepared=json.loads(r.prepare(self.home))
        self.assertLess(len(prepared['mail']['body']),25000)
        self.pub(prepared['batch_id'])
        with r.locked(self.home) as (_,state):
            self.assertEqual(len(state['delivered']['test-mail']['record']['mail']['body']),25000)
    def test_unknown(self):
        b=self.seed()
        with self.assertRaises(RuntimeError): self.pub(b,sender=lambda *a:{'success':False})
        with self.assertRaises(RuntimeError): r.prepare(self.home)
        with self.assertRaises(ValueError): self.pub(b)
    def test_collect_and_publish_never_acknowledge(self):
        with patch.object(r,'acknowledge',side_effect=AssertionError('background ACK is forbidden')) as ack:
            b=self.seed()
            self.pub(b)
            r.collect(self.home,fetch=lambda *a:{'value':[{'id':'test-mail'}]})
        ack.assert_not_called()
    def test_old_unacknowledged_receipt_not_retried(self):
        b=self.seed(); self.pub(b)
        with r.locked(self.home) as (path,state):
            state['delivered']['test-mail']['acknowledged']=False
            state['delivered']['test-mail']['read']=False
            r.write(path,state)
        with patch.object(r,'acknowledge',side_effect=AssertionError('retry ACK is forbidden')) as ack:
            for _ in range(2):
                r.collect(self.home,fetch=lambda *a:{'value':[{'id':'test-mail'}]})
        ack.assert_not_called()
        with r.locked(self.home) as (_,state):
            self.assertIs(state['delivered']['test-mail']['acknowledged'],False)
            self.assertIs(state['delivered']['test-mail']['read'],False)
            self.assertEqual(state['pending'],[])
            self.assertEqual(len(state['delivered']),1)
    def test_repeat_unread_collect_no_duplicates(self):
        def fetch(home,path):
            if 'mailFolders' in path: return {'value':[{'id':'repeat-mail','from':{},'receivedDateTime':'2026-01-01'}]}
            return {'body':{'contentType':'text','content':'fixture'}}
        r.collect(self.home,fetch=fetch); r.collect(self.home,fetch=fetch)
        with r.locked(self.home) as (_,state):
            self.assertEqual([m['id'] for m in state['pending']],['repeat-mail'])
            self.assertEqual(state['delivered'],{})
    def test_receipt_read_history_mapping(self):
        self.assertFalse(r.receipt_read({}))
        self.assertTrue(r.receipt_read({'acknowledged':True}))
        self.assertTrue(r.receipt_read({'acknowledged':False,'read':True}))
        self.assertFalse(r.receipt_read({'acknowledged':True,'read':False}))
    def test_mark_read_graph_verify_then_persist(self):
        b=self.seed(); self.pub(b); calls=[]
        def graph(home,path,method='GET',body=None):
            calls.append((path,method,body))
            return {} if method=='PATCH' else {'isRead':True}
        r.mark_read(self.home,'test-mail',graph_fn=graph)
        self.assertEqual([(method,body) for _,method,body in calls],[('PATCH',{'isRead':True}),('GET',None)])
        self.assertIn('?$select=isRead',calls[1][0])
        with r.locked(self.home) as (_,state):
            self.assertIs(state['delivered']['test-mail']['read'],True)
            self.assertIs(state['delivered']['test-mail']['acknowledged'],True)
    def test_mark_read_verify_failure_does_not_persist(self):
        b=self.seed(); self.pub(b); calls=[]
        def graph(home,path,method='GET',body=None):
            calls.append(method)
            return {} if method=='PATCH' else {'isRead':False}
        with self.assertRaises(RuntimeError): r.mark_read(self.home,'test-mail',graph_fn=graph)
        self.assertEqual(calls,['PATCH','GET'])
        with r.locked(self.home) as (_,state):
            self.assertIs(state['delivered']['test-mail']['read'],False)
    def test_mark_read_graph_failure_does_not_persist(self):
        b=self.seed(); self.pub(b)
        def graph(*a,**k): raise RuntimeError('fixture Graph failure')
        with self.assertRaises(RuntimeError): r.mark_read(self.home,'test-mail',graph_fn=graph)
        with r.locked(self.home) as (_,state):
            self.assertIs(state['delivered']['test-mail']['read'],False)
    def test_mark_read_unknown_never_touches_graph(self):
        with patch.object(r, 'acknowledge') as ack:
            with self.assertRaises(FileNotFoundError):
                r.mark_read(self.home, 'unknown-fixture')
        ack.assert_not_called()
    def test_bad_batch(self):
        self.seed()
        with self.assertRaises(ValueError): self.pub('wrong')
    def test_mention(self):
        b=self.seed()
        with self.assertRaises(ValueError): r.publish(self.home,b,'@everyone','run','discord:123')
    def test_dedup(self):
        self.seed()
        r.collect(self.home,fetch=lambda *a:{'value':[{'id':'test-mail'}]})
        with r.locked(self.home) as (_,s): self.assertEqual(s['pending'],[])

if __name__=='__main__': unittest.main()
