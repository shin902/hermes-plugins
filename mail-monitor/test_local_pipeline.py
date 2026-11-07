"""Isolated fixtures for no-notification publication and LAN-only processing."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
spec=importlib.util.spec_from_file_location('local_test_mail',Path(__file__).with_name('__init__.py'),submodule_search_locations=[str(Path(__file__).parent)])
pkg=importlib.util.module_from_spec(spec); sys.modules[spec.name]=pkg; spec.loader.exec_module(pkg)
from local_test_mail import runtime as r
from local_test_mail.local_pipeline import LocalLLM, run

class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.home=Path(self.tmp.name)
        self.cfg={'discord_notifications':False,'local_endpoint':'http://127.0.0.1:1/v1','local_model':'synthetic',
                  'target':'discord:123','dashboard_root':os.path.expanduser('~/.hermes/sites/personal-dashboard'),
                  'dashboard_db':str(self.home/'dashboard.db')}
    def tearDown(self): self.tmp.cleanup()
    def seed(self):
        def fetch(home,path):
            if 'mailFolders' in path: return {'value':[{'id':'synthetic','subject':'Test','receivedDateTime':'2026-10-27T00:00:00Z','from':{}}]}
            return {'body':{'contentType':'text','content':'Synthetic original'}}
        r.collect(self.home,fetch=fetch)
    def test_no_transport_and_reader_contract(self):
        self.seed(); current=json.loads(r.prepare(self.home))
        forbidden=Mock(side_effect=AssertionError('Network forbidden'))
        with patch.object(r,'acknowledge',forbidden):
            result=r.publish(self.home,current['batch_id'],'合成要約','test','discord:123',
                             sender=forbidden,thread_resolver=forbidden,notifications=False)
        self.assertTrue(result['success']); forbidden.assert_not_called()
        retry=r.publish(self.home,current['batch_id'],'合成要約','test','discord:123',
                        sender=forbidden,thread_resolver=forbidden,notifications=False)
        self.assertTrue(retry['already_delivered'])
        with r.locked(self.home) as (_,s):
            receipt=s['delivered']['synthetic']
            self.assertEqual(receipt['record']['summary'],'合成要約')
            self.assertFalse(receipt['read']); self.assertIsNone(receipt['message_id'])
        sys.path.insert(0,self.cfg['dashboard_root'])
        from dashboard.mail_adapter import MailAdapter
        view=MailAdapter(self.home/'cron/mail-monitor/state.json',self.home/'none').collect()
        self.assertEqual(len(view['mails']),1)
    def test_retry_restart_idle_no_inference(self):
        self.seed()
        bad=Mock(side_effect=RuntimeError('synthetic'))
        self.assertFalse(run(self.home,self.cfg,bad,limit=1)['success'])
        good=Mock(return_value={'summary':'合成要約'})
        result=run(Path(str(self.home)),self.cfg,good,limit=1)
        self.assertEqual(result['summaries_saved'],1)
        result=run(self.home,self.cfg,good,limit=1)
        self.assertEqual(result['summaries_saved'],0); self.assertEqual(good.call_count,1)
        self.assertEqual(result['total_summaries_saved'],1)
    def test_unknown_is_not_retried(self):
        self.seed(); r.prepare(self.home)
        with r.locked(self.home) as (p,s):
            s['current']['status']='unknown'; r.write(p,s)
        model=Mock(side_effect=AssertionError())
        self.assertFalse(run(self.home,self.cfg,model)['success']); model.assert_not_called()
    def test_local_errors_are_sanitized_and_classified(self):
        import urllib.error
        client=LocalLLM('http://127.0.0.1:1/v1','test')
        for code,expected in ((400,ValueError),(413,ValueError),(422,ValueError),(429,RuntimeError),(503,RuntimeError)):
            error=urllib.error.HTTPError('http://127.0.0.1:1/v1',code,'SYNTHETIC_PRIVATE_SENTINEL',{},None)
            with patch.object(client.opener,'open',side_effect=error):
                with self.assertRaises(expected) as raised: client('synthetic',{})
            self.assertNotIn('SENTINEL',str(raised.exception))
            error.close()
    def test_cloud_and_dns_rejected(self):
        for endpoint in ['https://api.openai.com/v1','http://example.com/v1','http://8.8.8.8/v1',
                         'http://user:secret@127.0.0.1/v1','http://127.0.0.1/v1?cloud=yes']:
            with self.assertRaises(ValueError): LocalLLM(endpoint,'test')
        LocalLLM('http://192.168.1.10:8080/v1','test')

if __name__=='__main__': unittest.main()
