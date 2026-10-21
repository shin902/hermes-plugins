"""No model execution: real short children, fresh registration, public CLI contracts."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_consult import ConsultTests, consult


class HardeningTests(ConsultTests):
    def test_init_precedence_and_invalid_ids(self):
        init = lambda sid: json.dumps({'type':'system','subtype':'init','session_id':sid})
        for invalid in ('', '../bad', 'with space', 42, None, 'x'*129):
            output = '\n'.join([init('good-init'), init(invalid), '{bad', json.dumps({'type':'result','exit_code':0,'text':'ok','session_id':invalid})])
            result = consult._outcome(0, output, 'session_id: stderr-id', False, 'mail', 'new_session')
            self.assertEqual(result['session_id'], 'good-init')
        result = consult._outcome(0, init('init-id')+'\n'+self.output, '', False, 'mail', 'new_session')
        self.assertEqual(result['session_id'], 'test-recipient')

    def test_huge_stdout_init_only_timeout(self):
        script = 'import json,sys,time; print(json.dumps(dict(type="system",subtype="init",session_id="early-id")),flush=True); print("x"*900000,flush=True); sys.stdin.read(); time.sleep(5)'
        transport = consult._run([sys.executable,'-c',script], 'secret', dict(os.environ), .25, self.root)
        result = consult._outcome(*transport,'mail','new_session')
        self.assertEqual(result['session_id'],'early-id')
        self.assertEqual(result['status'],'timeout_unknown')
        self.assertNotIn('reply',result)
        self.assertLess(len(transport[1]),525000)

    def test_zero_timeout_real_child(self):
        transport = consult._run([sys.executable,'-c','import time; time.sleep(.2); print("done")'], '', dict(os.environ), 0, self.root)
        self.assertEqual(transport[0],0)
        self.assertFalse(transport[3])
        self.assertIn('done',transport[1])

    def test_run_budget_is_always_zero(self):
        result,run = self.call(self.args)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--run-budget')+1],'0')
        self.assertEqual(argv[argv.index('--max-turns')+1],'250')
        self.assertEqual(run.call_args.args[3],0)
        self.assertEqual(result['status'],'completed')

    def test_audit_no_body_and_cancel(self):
        result,_ = self.call()
        folder = self.root/'profiles/coding/state/bot-acl/consult-audit'
        path = folder/(result['request_id']+'.json')
        record = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(record['status'],'completed')
        self.assertEqual(record['session_id'],'test-recipient')
        self.assertNotIn(self.args['message'],path.read_text())
        self.assertIsNotNone(record['end'])
        with patch.object(consult,'_run',side_effect=KeyboardInterrupt),patch.object(consult.shutil,'which',return_value='/fixture/hermes'):
            with self.assertRaises(KeyboardInterrupt):
                consult.ask(self.domains,self.args)
        records=[json.loads(p.read_text()) for p in folder.glob('*.json')]
        self.assertIn('cancelled_unknown',[r['status'] for r in records])

    def test_partial_never_completed(self):
        for extra in ({'partial':True},{'completed':False},{'status':'cancelled'},{'status':'timeout'},{'status':'interrupted'}):
            record={'type':'result','exit_code':0,'text':'partial',**extra}
            result=consult._outcome(0,json.dumps(record),'',False,'mail','new_session')
            self.assertEqual(result['status'],'failed')
            self.assertNotIn('reply',result)

    def test_fresh_registered_handler_and_schema(self):
        from jsonschema import Draft202012Validator
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        manager=PluginManager(str(self.root))
        ctx=PluginContext(PluginManifest(name='bot-acl'),manager)
        registered={}
        original=ctx.register_tool
        def register(name,toolset,schema,handler,**kw):
            registered[name]=(schema,handler)
            return original(name,toolset,schema,handler,**kw)
        with patch.object(ctx,'register_tool',side_effect=register):
            consult.register(ctx,self.domains)
        schema,handler=registered['ask_bot']
        Draft202012Validator.check_schema(schema['parameters'])
        validator=Draft202012Validator(schema['parameters'])
        self.assertNotIn('timeout_seconds', schema['parameters']['properties'])
        for timeout in (0,30,300):
            self.assertTrue(list(validator.iter_errors({**self.args,'timeout_seconds':timeout})))
        with patch.object(consult,'_run',return_value=(0,self.output,'',False)),patch.object(consult.shutil,'which',return_value='/fixture/hermes'):
            self.assertEqual(json.loads(handler(self.args))['status'],'completed')
            rejected=json.loads(handler({**self.args,'timeout_seconds':0}))
            self.assertEqual(rejected['status'],'failed')
            self.assertIn('timeout_seconds is not supported',rejected['error'])

    def test_real_cancel_reaps_and_preserves_init_audit(self):
        original = subprocess.Popen
        spawned = []
        def popen(*args, **kwargs):
            proc = original(*args, **kwargs)
            spawned.append(proc)
            communicate = proc.communicate
            calls = [0]
            def interrupt(*a, **kw):
                calls[0] += 1
                if calls[0] == 2:
                    raise KeyboardInterrupt()
                return communicate(*a, **kw)
            proc.communicate = interrupt
            return proc
        actual = consult._run
        script = 'import json,time; print(json.dumps(dict(type="system",subtype="init",session_id="cancel-id")),flush=True); time.sleep(5)'
        def run(argv,payload,env,timeout,scratch):
            return actual([sys.executable,'-c',script],payload,env,0,scratch)
        with patch.object(consult,'_run',side_effect=run),patch.object(consult.subprocess,'Popen',side_effect=popen),patch.object(consult.shutil,'which',return_value='/fixture/hermes'):
            with self.assertRaises(KeyboardInterrupt):
                consult.ask(self.domains,self.args)
        self.assertIsNotNone(spawned[0].returncode)
        records=list((self.root/'profiles/coding/state/bot-acl/consult-audit').glob('*.json'))
        record=json.loads(records[0].read_text())
        self.assertEqual(record['session_id'],'cancel-id')
        self.assertEqual(record['status'],'cancelled_unknown')
        self.assertEqual(record['cleanup'],'reaped')
        self.assertEqual(record['pid'],spawned[0].pid)

    def test_max_turns_is_250(self):
        self.assertEqual(consult.MAX_TURNS, 250)


if __name__=='__main__':
    unittest.main()
