import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('route_test_mail',Path(__file__).with_name('__init__.py'),submodule_search_locations=[str(Path(__file__).parent)])
assert spec and spec.loader
pkg=importlib.util.module_from_spec(spec); sys.modules[spec.name]=pkg; spec.loader.exec_module(pkg)
from route_test_mail import runtime, routing

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']); self.home=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def mail(self,subject='Re: [Owner/Repo] title (PR #19)'):
        return {'from':{'address':'notifications@github.com'},'subject':subject,'headers':[{'name':'List-ID','value':'Owner/Repo <repo.github.com>'}]}
    def bindings(self,key='mail:a@example.com',tid='456'):
        runtime.write(self.home/'cron/mail-monitor/threads.json',{'version':1,'parent_id':'123','bindings':{key:{'status':'ready','thread_id':tid}}})
    def test_email(self): self.assertEqual(routing.route_key(self.home,{'from':{'address':' A@Example.COM '}}),'mail:a@example.com')
    def test_pr(self): self.assertEqual(routing.route_key(self.home,self.mail()),'github:owner/repo:item:19')
    def test_issue(self): self.assertEqual(routing.route_key(self.home,self.mail('Re: title (#2)')),'github:owner/repo:item:2')
    def test_spoof(self):
        m=self.mail(); m['from']['address']='other@example.com'
        self.assertEqual(routing.route_key(self.home,m),'mail:other@example.com')
    def test_commit(self):
        m=self.mail('[Owner/Repo] PR run failed: CI (abc1234)')
        self.assertEqual(routing.route_key(self.home,m,lambda *a:[{'number':19,'base':{'repo':{'full_name':'Owner/Repo'}}}]),'github:owner/repo:item:19')
    def test_ambiguous_commit(self):
        m=self.mail('[Owner/Repo] PR run failed: CI (abc1234)')
        self.assertEqual(routing.route_key(self.home,m,lambda *a:[{'number':n,'base':{'repo':{'full_name':'Owner/Repo'}}} for n in [1,2]]),'mail:notifications@github.com')
    def test_reuse(self):
        self.bindings(); calls=[]
        def api(h,p,method='GET',body=None):
            calls.append(method); return {'id':'456','parent_id':'123','type':11}
        self.assertEqual(routing.ensure_thread(self.home,'mail:a@example.com','123',api),'456'); self.assertEqual(calls,['GET'])
    def test_create_and_persist(self):
        calls=[]
        def api(h,p,method='GET',body=None):
            calls.append(method); return {'id':'456','parent_id':'123','type':11}
        self.assertEqual(routing.ensure_thread(self.home,'mail:a@example.com','123',api),'456')
        self.assertEqual(calls,['POST','GET']); self.assertEqual(json.loads((self.home/'cron/mail-monitor/threads.json').read_text())['bindings']['mail:a@example.com']['thread_id'],'456')
    def test_archive(self):
        self.bindings(); calls=[]
        def api(h,p,method='GET',body=None):
            calls.append(method); return {'id':'456','parent_id':'123','type':11,'thread_metadata':{'archived':method=='GET'}}
        self.assertEqual(routing.ensure_thread(self.home,'mail:a@example.com','123',api),'456'); self.assertEqual(calls,['GET','PATCH'])
    def test_unknown_fence(self):
        def fail(*a): raise TimeoutError('fixture')
        with self.assertRaises(TimeoutError): routing.ensure_thread(self.home,'mail:a@example.com','123',fail)
        with self.assertRaises(RuntimeError): routing.ensure_thread(self.home,'mail:a@example.com','123',fail)
    def test_wrong_parent(self):
        self.bindings()
        with self.assertRaises(ValueError): routing.ensure_thread(self.home,'mail:a@example.com','123',lambda *a:{'id':'456','parent_id':'789','type':11})
    def test_deleted_recreate(self):
        self.bindings(); calls=[]
        def api(h,p,method='GET',body=None):
            calls.append((p,method))
            if p=='/channels/456': raise routing.DiscordHTTPError(404)
            return {'id':'457','parent_id':'123','type':11}
        self.assertEqual(routing.ensure_thread(self.home,'mail:a@example.com','123',api),'457')
    def test_thread_name(self): self.assertEqual(routing.thread_name('github:owner/repo:item:19'),'owner/repo #19')

if __name__=='__main__': unittest.main()
