import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('rss_v3',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
pkg=importlib.util.module_from_spec(spec)
sys.modules['rss_v3']=pkg
spec.loader.exec_module(pkg)
from rss_v3 import runtime as r

ITEM={'id':'new','url':'https://example.com/new','title':'New','published':'today','description':'Actual fixture'}
TARGET='discord:123:456'
TEXT='検証済みの記事について説明します。出典と元の記事URLはこちらです。 https://example.com/new'

class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home=Path(self.directory.name)
        self.path=self.home/'cron/rss-monitor/job.json'
        r.write(self.path,r.initial('feed',[]))
    def pending(self):
        with patch.object(r,'fetch_feed',return_value=[ITEM]):
            self.assertTrue(r.collect(self.home,'job','feed')['ok'])
        return json.loads(r.prepare(self.home,'job'))['batch_id']
    def sender(self,home,target,text):
        return {'success':True,'platform':'discord','chat_id':'123','message_id':'confirmed'}
    def test_no_items_is_stable_and_no_fetch_in_monitor(self):
        with patch.object(r,'fetch_feed',side_effect=AssertionError('must not fetch')):
            self.assertEqual(r.prepare(self.home,'job'),r.EMPTY_OUTPUT)
            self.assertEqual(r.prepare(self.home,'job'),r.EMPTY_OUTPUT)
    def test_retry_nonce_preserves_batch_after_pre_agent_snapshot(self):
        batch=self.pending()
        first=r.read(self.path)['output']
        second=r.prepare(self.home,'job')
        self.assertNotEqual(first,second)
        self.assertEqual(json.loads(second)['batch_id'],batch)
        self.assertEqual(json.loads(second)['items'],[ITEM])
    def test_success_ack_idempotence_and_stable_monitor(self):
        batch=self.pending()
        output=r.read(self.path)['output']
        sends=[]
        def send(*args):
            sends.append(args)
            self.assertEqual(r.read(self.path)['current']['status'],'sending')
            return self.sender(*args)
        result=r.publish(self.home,'job','execution',batch,TEXT,TARGET,sender=send)
        self.assertTrue(result['success'])
        self.assertIsNone(r.read(self.path)['current'])
        self.assertEqual(r.prepare(self.home,'job'),output)
        self.assertTrue(r.publish(self.home,'job','execution',batch,TEXT,TARGET,sender=send)['already_delivered'])
        self.assertEqual(len(sends),1)
    def test_ambiguous_failure_never_resends(self):
        batch=self.pending()
        with self.assertRaises(TimeoutError):
            r.publish(self.home,'job','execution',batch,TEXT,TARGET,sender=lambda *args: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(r.read(self.path)['current']['status'],'unknown')
        with self.assertRaises(ValueError): r.prepare(self.home,'job')
        with self.assertRaises(ValueError): r.publish(self.home,'job','execution',batch,TEXT,TARGET,sender=self.sender)
    def test_partial_chunks_preserve_receipts(self):
        batch=self.pending()
        responses=iter([self.sender(None,None,None),{'success':False,'error':'failed'}])
        with self.assertRaises(RuntimeError):
            r.publish(self.home,'job','execution',batch,TEXT+'a'*3500,TARGET,sender=lambda *args:next(responses))
        current=r.read(self.path)['current']
        self.assertEqual(current['status'],'unknown')
        self.assertEqual(current['message_ids'],['confirmed'])
    def test_unconfirmed_receipts_do_not_ack(self):
        for response in [{'success':True,'queued':True}, {'success':True,'message_id':'id','platform':'discord','warnings':['partial']}, {'success':True,'skipped':True}]:
            with self.subTest(response=response):
                r.write(self.path,r.initial('feed',[]))
                batch=self.pending()
                with self.assertRaises(RuntimeError):
                    r.publish(self.home,'job','execution',batch,TEXT,TARGET,sender=lambda *args:response)
                self.assertIsNotNone(r.read(self.path)['current'])
    def test_bad_summary_has_no_transport_side_effect(self):
        batch=self.pending()
        for text in ['missing original URL but long enough summary content',TEXT+' MEDIA:/secret',TEXT+' @everyone']:
            with self.assertRaises(ValueError):
                r.publish(self.home,'job','execution',batch,text,TARGET,sender=lambda *args: self.fail('must not send'))
        self.assertEqual(r.read(self.path)['current']['status'],'ready')
    def test_collection_failure_preserves_article_state_and_outage_policy(self):
        self.pending()
        before=self.path.read_bytes()
        with patch.object(r,'fetch_feed',side_effect=r.TransientFetchError('HTTP 404')) as fetch:
            self.assertFalse(r.collect(self.home,'job','feed',now=100)['alert'])
            self.assertTrue(r.collect(self.home,'job','feed',now=10900)['alert'])
            self.assertFalse(r.collect(self.home,'job','feed',now=20000)['alert'])
            self.assertEqual(fetch.call_count,3)
        self.assertEqual(before,self.path.read_bytes())
        with patch.object(r,'fetch_feed',return_value=[ITEM]):r.collect(self.home,'job','feed')
        self.assertEqual(json.loads(self.path.with_name('job-health.json').read_text())['status'],'ok')
    def test_native_send_public_cli_and_profile_scope(self):
        import subprocess
        good=subprocess.CompletedProcess([],0,json.dumps(self.sender(None,None,None)),'')
        with patch.object(r.subprocess,'run',return_value=good) as run:
            self.assertTrue(r.send(self.home,TARGET,TEXT)['success'])
        self.assertEqual(run.call_args.args[0],['hermes','send','--to',TARGET,'--json','--file','-'])
        self.assertEqual(run.call_args.kwargs['env']['HERMES_HOME'],str(self.home))
    def test_tool_uses_current_execution_not_user_target(self):
        from types import SimpleNamespace
        class Context:
            run=None
            def current_cron_execution(self): return self.run
            def register_tool(self,**kwargs): self.handler=kwargs['handler']
            def register_cli_command(self,*args): pass
        ctx=Context()
        with patch.object(pkg,'settings',return_value=({'job':'feed'},{'job':TARGET})), patch.object(pkg,'home',return_value=self.home), patch.object(r,'publish',return_value={'success':True}) as publish:
            pkg.register(ctx)
            self.assertFalse(json.loads(ctx.handler({'batch_id':'any','summary':TEXT}))['success'])
            ctx.run=SimpleNamespace(job_id='other',execution_id='e')
            self.assertFalse(json.loads(ctx.handler({'batch_id':'any','summary':TEXT}))['success'])
            publish.assert_not_called()
            ctx.run=SimpleNamespace(job_id='job',execution_id='e')
            self.assertTrue(json.loads(ctx.handler({'batch_id':'any','summary':TEXT,'target':'evil'}))['success'])
            self.assertEqual(publish.call_args.args[-1],TARGET)
    def test_malformed_state_and_non_string_receipt_fail_closed(self):
        state=r.read(self.path)
        state['pending']=[{'id':'new','url':1}]
        r.write(self.path,state)
        with self.assertRaises(ValueError):r.prepare(self.home,'job')
        r.write(self.path,r.initial('feed',[]))
        batch=self.pending()
        with self.assertRaises(RuntimeError):r.publish(self.home,'job','e',batch,TEXT,TARGET,sender=lambda *a:{'success':True,'platform':'discord','message_id':True})
        self.assertEqual(r.read(self.path)['current']['status'],'unknown')
    def test_failed_notification_is_recorded_and_surfaces_native_failure(self):
        r.write(self.path.with_name('job-health.json'),{'status':'fetch_failed','outage':{'first_failure_at':1,'failures':4,'alert_attempted':False}})
        with patch.object(pkg,'settings',return_value=({'job':'feed'},{'job':TARGET})), patch.object(pkg,'home',return_value=self.home), patch.object(r,'fetch_feed',side_effect=r.TransientFetchError('HTTP 404')), patch.object(r,'send',side_effect=TimeoutError('Unknown transport')):
            self.assertEqual(pkg.collect_all(None),1)
        health=json.loads(self.path.with_name('job-health.json').read_text())
        self.assertTrue(health['outage']['alert_attempted'])
        self.assertEqual(health['outage']['notification']['status'],'unknown')
    def test_monitor_compatibility_entry_never_fetches(self):
        from rss_v3 import monitor
        with patch.object(r,'prepare',return_value=r.EMPTY_OUTPUT),patch.object(monitor,'fetch_feed',side_effect=AssertionError('must not fetch')),patch('sys.stdout',new_callable=io.StringIO) as output:
            monitor.run_monitor(self.home,'job','feed')
            self.assertEqual(output.getvalue().strip(),r.EMPTY_OUTPUT)
    def test_diagnostics_remove_credentials_and_error_body(self):
        from rss_v3 import monitor
        path=self.home/'test.jsonl'
        monitor.record_fetch(path,{'source_url':'https://user:password@example.com/feed?token=secret&channel_id=public','body_excerpt':'secrets'})
        data=json.loads(path.read_text())
        self.assertNotIn('body_excerpt',data)
        self.assertNotIn('password',data['source_url'])
        self.assertNotIn('secret',data['source_url'])
        self.assertIn('channel_id=public',data['source_url'])
    def test_profile_configs_and_runtime_state_are_isolated(self):
        homes = {name: self.home / name for name in ('my-dot', 'note-writing')}
        for name, profile_home in homes.items():
            config = profile_home / 'plugin-config/rss-monitor'
            config.mkdir(parents=True)
            (config / 'feeds.json').write_text(json.dumps({name: f'https://{name}.example/feed'}))
            (config / 'delivery.json').write_text(json.dumps({name: f'discord:{1 if name == "my-dot" else 2}'}))
            (config / 'policies.json').write_text(json.dumps({name: {
                'exclude_youtube_shorts': True, 'batch_limit': 1 if name == 'my-dot' else 6}}))
            path = profile_home / 'cron/rss-monitor' / f'{name}.json'
            state = r.initial(f'https://{name}.example/feed', [])
            state['pending'] = [dict(ITEM, id=f'{name}-{n}', url=f'https://example.com/{name}/{n}')
                                for n in range(2)]
            r.write(path, state)

        for name, profile_home in homes.items():
            with patch.object(pkg, 'home', return_value=profile_home):
                feeds, targets = pkg.settings()
                self.assertEqual(set(feeds), {name})
                self.assertEqual(set(targets), {name})
                self.assertTrue(r.excludes_shorts(name))
                self.assertFalse(r.excludes_shorts('other'))
                output = json.loads(r.prepare(profile_home, name))
                self.assertEqual(len(output['items']), 1 if name == 'my-dot' else 2)
                self.assertTrue((profile_home / 'cron/rss-monitor' / f'{name}.json').exists())
                self.assertFalse((homes['note-writing' if name == 'my-dot' else 'my-dot']
                                  / 'cron/rss-monitor' / f'{name}.json').exists())

    def test_invalid_batch_limit_preserves_pending(self):
        config = self.home / 'plugin-config/rss-monitor'
        config.mkdir(parents=True)
        path = self.home / 'cron/rss-monitor/job.json'
        state = r.initial('https://example.com/feed', [])
        state['pending'] = [ITEM]
        r.write(path, state)
        for limit in (0, 7, True, '6'):
            (config / 'policies.json').write_text(json.dumps({'job': {'batch_limit': limit}}))
            with patch.object(pkg, 'home', return_value=self.home):
                with self.assertRaises(ValueError): r.prepare(self.home, 'job')
            self.assertEqual(r.read(path)['pending'], [ITEM])

    def test_utf16_chunks_fit_discord(self):
        text='😀'*4000
        parts=r.chunks(text)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=1800 for p in parts))

if __name__=='__main__':unittest.main(verbosity=2)
