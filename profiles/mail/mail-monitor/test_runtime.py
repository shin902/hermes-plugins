import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
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
    def pub(self,b,sender=None,ack=None):
        return r.publish(self.home,b,'テストの要約','fixture-run','discord:123',sender=sender or (lambda *a:{'success':True,'message_id':'123456'}),ack=ack or (lambda *a:None),thread_resolver=lambda *a:'456')
    def test_empty(self): self.assertEqual(r.prepare(self.home),r.EMPTY)
    def test_retry_nonce(self):
        self.seed(); a=r.prepare(self.home); b=r.prepare(self.home); self.assertNotEqual(a,b); self.assertEqual(json.loads(a)['batch_id'],json.loads(b)['batch_id'])
    def test_publish(self):
        b=self.seed(); self.assertTrue(self.pub(b)['acknowledged']); self.assertEqual(r.prepare(self.home),r.EMPTY); self.assertTrue(self.pub(b)['already_delivered'])
    def test_unknown(self):
        b=self.seed()
        with self.assertRaises(RuntimeError): self.pub(b,sender=lambda *a:{'success':False})
        with self.assertRaises(RuntimeError): r.prepare(self.home)
        with self.assertRaises(ValueError): self.pub(b)
    def test_ack_fail_no_resend(self):
        b=self.seed()
        def fail(*a): raise RuntimeError('fixture')
        self.assertFalse(self.pub(b,ack=fail)['acknowledged']); self.assertTrue(self.pub(b,sender=fail)['already_delivered'])
    def test_ack_retry(self):
        b=self.seed()
        def fail(*a): raise RuntimeError('fixture')
        self.pub(b,ack=fail); ids=[]
        r.collect(self.home,fetch=lambda *a:{'value':[]},ack=lambda h,m:ids.append(m))
        self.assertEqual(ids,['test-mail'])
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
