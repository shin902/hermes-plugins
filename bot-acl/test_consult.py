"""Consultation modes, live ACL, CLI results and bounded process lifetime."""
import ast
import json
import os
import sqlite3
import select
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from contextlib import closing

from test_acl import acl
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

consult = sys.modules[acl.__name__ + '.ask_bot']
peers = sys.modules[acl.__name__ + '.list_bot_peers']


class ConsultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config.yaml').write_text('{}\n')
        for name in ('coding','mail','local'):
            home = self.root / 'profiles' / name
            home.mkdir(parents=True)
            (home / 'profile.yaml').write_text('{}\n')
        token = set_hermes_home_override(self.root / 'profiles/coding')
        self.addCleanup(reset_hermes_home_override, token)
        self.domains = {'main': {'coding','mail','default'}, 'local': {'local'}}
        (self.root / 'profile.yaml').write_text('{}\n')
        self.args = {'target':'mail','message':'A literal $(false); `false` question'}
        self.output = json.dumps({'type':'result','exit_code':0,'text':'Actual transport fixture reply','session_id':'test-recipient'})
        self.env_patch = patch.dict(os.environ, {consult.DEPTH_ENV:'0'})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def call(self, args=None, transport=None):
        with patch.object(consult, '_run', return_value=transport or (0,self.output,'',False)) as run, patch.object(consult.shutil,'which',return_value='/cli/hermes'):
            response = json.loads(consult.ask(self.domains, self.args if args is None else args))
            return response,run

    def test_new_session_cli_and_literal_payload(self):
        result,run = self.call()
        self.assertEqual(result['status'],'completed')
        self.assertFalse(result['resident'])
        argv,payload,env,timeout,scratch=run.call_args.args
        self.assertEqual(argv[:4],['/cli/hermes','-p','mail','chat'])
        self.assertNotIn('--continue',argv)
        self.assertIn('--format',argv)
        self.assertEqual(argv[argv.index('--source')+1], 'ask_bot')
        from hermes_state_sessions import INTERNAL_LISTING_SOURCES
        self.assertNotIn('ask_bot', INTERNAL_LISTING_SOURCES)
        self.assertEqual(argv[argv.index('--query-file')+1],'-')
        self.assertIn(self.args['message'],payload)
        self.assertIn('@coding',payload)
        self.assertEqual(env['HERMES_HOME'],str(self.root/'profiles/mail'))
        self.assertEqual(env[consult.DEPTH_ENV],'1')
        self.assertEqual(timeout,0)
        self.assertEqual(argv[argv.index('--run-budget')+1], '0')
        self.assertEqual(argv[argv.index('--max-turns')+1], '250')
        self.assertEqual(scratch,self.root/'profiles/coding/cache/scratch')

    def test_bot_chat_mode_is_rejected_before_launch(self):
        result, run = self.call({**self.args, 'mode': 'bot_chat'})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('mode must be new_session or resume', result['error'])
        run.assert_not_called()

    def recipient_session(self, target='mail', sid='test-recipient'):
        home = self.root if target == 'default' else self.root / 'profiles' / target
        with closing(sqlite3.connect(home / 'state.db')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO sessions VALUES (?)', (sid,))
            db.commit()
        return sid

    def test_resume_exact_id_and_profile(self):
        for target in ('mail', 'default'):
            sid = self.recipient_session(target)
            result, run = self.call({**self.args, 'target': target, 'mode': 'resume', 'session_id': sid})
            self.assertEqual(result['status'], 'completed', result)
            argv, _, env, _, _ = run.call_args.args
            self.assertEqual(argv[-1], '--resume=' + sid)
            self.assertEqual(argv[2], target)
            self.assertNotIn('--continue', argv)
            self.assertNotIn('--create-if-missing', argv)
            self.assertEqual(result['session_id'], sid)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(env['HERMES_HOME'], str(self.root if target == 'default' else self.root / 'profiles' / target))

    def test_resume_rejects_untrusted_and_wrong_mode_ids(self):
        for sid in (None, '', 'latest', 'LATEST', '--provider=evil', '../state', 'x;false', 'x\n-y', 'x'*129, 123, ' Bot Chat'):
            result, run = self.call({**self.args, 'mode': 'resume', 'session_id': sid})
            self.assertEqual(result['status'], 'failed', sid)
            run.assert_not_called()
        for mode in ('new_session', 'bot_chat'):
            result, run = self.call({**self.args, 'mode': mode, 'session_id': 'test-recipient'})
            self.assertEqual(result['status'], 'failed')
            run.assert_not_called()

    def test_resume_exact_lookup_and_acl_fail_closed(self):
        self.recipient_session('default', 'other-profile')
        self.recipient_session('mail')
        for target, sid, status in [('mail', 'other-profile', 'failed'), ('mail', 'missing-title', 'failed'), ('local', 'test-recipient', 'blocked')]:
            result, run = self.call({**self.args, 'target': target, 'mode': 'resume', 'session_id': sid})
            self.assertEqual(result['status'], status)
            run.assert_not_called()
        (self.root / 'profiles/mail/state.db').unlink()
        result, run = self.call({**self.args, 'mode': 'resume', 'session_id': 'test-recipient'})
        self.assertEqual(result['status'], 'failed')
        run.assert_not_called()
        self.assertFalse((self.root / 'profiles/mail/state.db').exists())

    def test_resume_busy_timeout_and_admission(self):
        sid = self.recipient_session()
        args = {**self.args, 'mode': 'resume', 'session_id': sid}
        for transport, status in [((1, '', 'hermes-refusal-reason: SESSION_NOT_OWNED\n', False), 'target_busy'), ((-9, '', '', True), 'timeout_unknown')]:
            result, run = self.call(args, transport)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['session_id'], sid)
            run.assert_called_once()
        with consult._receiver_lock(self.root, 'mail', sid):
            result, run = self.call(args)
            self.assertEqual(result['status'], 'target_busy')
            run.assert_not_called()
            self.recipient_session('mail', 'independent')
            result, run = self.call({**args, 'session_id': 'independent'})
            self.assertEqual(result['status'], 'completed')
        result, _ = self.call(args)
        self.assertEqual(result['status'], 'completed')

    def test_resume_discovery(self):
        modes = consult.ASK_SCHEMA['parameters']['properties']['mode']['enum']
        self.assertEqual(modes, ['new_session', 'resume'])
        self.assertNotIn('bot_chat', modes)
        self.assertNotIn('timeout_seconds', consult.ASK_SCHEMA['parameters']['properties'])
        self.assertIn('session_id', consult.ASK_SCHEMA['parameters']['properties'])
        self.assertEqual(json.loads(peers.list_peers(self.domains, {}))['modes'],
                         ['new_session', 'resume'])

    def test_alias_default_and_at_id(self):
        for name,canonical in [('@mail','mail'),('hermes','default')]:
            result,run=self.call({**self.args,'target':name})
            self.assertEqual(result['target'],canonical)
            self.assertEqual(run.call_args.args[0][2],canonical)

    def test_acl_denies_without_starting_process(self):
        for target in ('local','coding','peer/mail','missing','Mail','../../mail'):
            result,run=self.call({**self.args,'target':target})
            self.assertEqual(result['status'],'blocked',target)
            run.assert_not_called()

    def test_hook_covers_new_name(self):
        with patch.object(acl,'DOMAINS',self.domains):
            self.assertEqual(acl.check_bot_acl('ask_bot',{'target':'local'})['action'],'block')
            self.assertIsNone(acl.check_bot_acl('ask_bot',{'target':'mail'}))

    def test_live_peer_collision(self):
        (self.root/'config.yaml').write_text('bot_peers: {mail: {}}\n')
        result,run=self.call()
        self.assertEqual(result['status'],'blocked')
        run.assert_not_called()
        listing=json.loads(peers.list_peers(self.domains,{}))
        self.assertNotIn('mail',listing['peers'])

    def test_listing_same_group_only(self):
        listing=json.loads(peers.list_peers(self.domains,{}))
        self.assertEqual(listing['peers'],['default','mail'])
        self.assertEqual(listing['source'],'coding')

    def test_invalid_parameters(self):
        for extra in ({'mode':'unknown'},{'message':''},{'message':'x'*16001}):
            result,run=self.call({**self.args,**extra})
            self.assertEqual(result['status'],'failed')
            run.assert_not_called()

    def test_timeout_seconds_is_explicitly_rejected(self):
        for timeout in (0, 30, 300, -1, True, 1.5, None):
            result, run = self.call({**self.args, 'timeout_seconds': timeout})
            self.assertEqual(result['status'], 'failed', timeout)
            self.assertIn('timeout_seconds is not supported', result['error'])
            run.assert_not_called()

    def test_busy_gives_guidance_without_auto_retry(self):
        result,run=self.call(self.args,(1,'','hermes-refusal-reason: SESSION_NOT_OWNED\n',False))
        self.assertEqual(result['status'],'target_busy')
        self.assertIn('Do not automatically retry',result['next_step'])
        run.assert_called_once()

    def test_model_text_does_not_fake_busy(self):
        quoted=json.dumps({'type':'result','exit_code':0,'text':'hermes-refusal-reason: SESSION_NOT_OWNED'})
        result,_=self.call(transport=(0,quoted,'',False))
        self.assertEqual(result['status'],'completed')

    def test_timeout_and_failures_do_not_claim_delivery(self):
        for transport,status in [((-9,'','',True),'timeout_unknown'),((1,'','provider failed',False),'failed'),((0,'not json','',False),'failed'),((0,json.dumps({'type':'result','exit_code':1,'error':'quota'}),'',False),'failed')]:
            result,run=self.call(transport=transport)
            self.assertEqual(result['status'],status)
            self.assertNotIn('reply',result)
            run.assert_called_once()

    def test_nonzero_summary_preserved_as_partial(self):
        output = json.dumps({'type':'result','exit_code':1,'text':'Research summary',
                             'session_id':'recipient-summary'})
        result,run = self.call(transport=(1,output,'\nsession_id: recipient-summary\n',False))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['partial_reply'],'Research summary')
        self.assertEqual(result['session_id'],'recipient-summary')
        self.assertNotIn('reply',result)
        self.assertIn('incomplete',result['error'])
        run.assert_called_once()

    def test_provider_error_preserves_evidence_without_success(self):
        output = json.dumps({'type':'result','exit_code':1,'error':'quota',
                             'text':'Partial findings','session_id':'provider-error'})
        result,_ = self.call(transport=(1,output,'',False))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error'],'quota')
        self.assertEqual(result['partial_reply'],'Partial findings')
        self.assertNotIn('reply',result)

    def test_timeout_preserves_terminal_evidence_as_unknown(self):
        result,_ = self.call(transport=(-9,self.output,'',True))
        self.assertEqual(result['status'],'timeout_unknown')
        self.assertEqual(result['session_id'],'test-recipient')
        self.assertIn('partial_reply',result)
        self.assertNotIn('reply',result)

    def test_stderr_session_id_without_terminal_result(self):
        result,_ = self.call(transport=(1,'','\nsession_id: persisted-session\n',False))
        self.assertEqual(result['session_id'],'persisted-session')
        self.assertNotIn('session_id:',result['error'])
        self.assertNotIn('partial_reply',result)

    def test_success_keeps_stderr_session_id_fallback(self):
        output = json.dumps({'type':'result','exit_code':0,'text':'Success'})
        result,_ = self.call(transport=(0,output,'\nsession_id: fallback-id\n',False))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['session_id'],'fallback-id')

    def test_partial_summary_is_bounded(self):
        output = json.dumps({'type':'result','exit_code':1,'text':'x'*32001})
        result,_ = self.call(transport=(1,output,'',False))
        self.assertEqual(len(result['partial_reply']),32000)
        self.assertTrue(result['truncated'])

    def test_empty_or_invalid_success_reply_is_failure(self):
        for text in ('', '   ', None, 3):
            output = json.dumps({'type':'result','exit_code':0,'text':text})
            result,_ = self.call(transport=(0,output,'',False))
            self.assertEqual(result['status'],'failed')

    def test_nested_calls_disabled(self):
        with patch.dict(os.environ,{consult.DEPTH_ENV:'1'}):
            result,run=self.call()
            self.assertEqual(result['status'],'blocked')
            self.assertFalse(consult.available())
            run.assert_not_called()

    def test_standard_registration_prompt_and_imports(self):
        manager=PluginManager(str(self.root))
        context=PluginContext(PluginManifest(name='bot-acl'),manager)
        consult.register(context,self.domains)
        consult.register_prompt(context)
        sections=manager.render_system_prompt_sections({'session_id':'ordinary','title':'ordinary'})
        self.assertTrue(sections)
        tree=ast.parse(Path(__file__).with_name('ask_bot.py').read_text())
        core=[(node.module,item.name) for node in ast.walk(tree) if isinstance(node,ast.ImportFrom) and node.module and node.module.startswith(('hermes','tools','agent')) for item in node.names]
        self.assertEqual(core,[('hermes_constants','get_hermes_home')])

    def test_real_one_shot_process_exits(self):
        code,out,err,timed_out=consult._run([sys.executable,'-c','import sys; print(sys.stdin.read()); print("fixture stderr",file=sys.stderr)'],'literal $(false)',dict(os.environ),5,self.root)
        self.assertEqual(code,0)
        self.assertIn('literal $(false)',out)
        self.assertIn('fixture stderr',err)
        self.assertFalse(timed_out)

    @unittest.skipUnless(os.name=='posix','process-group check is POSIX')
    def test_real_timeout_stops_parent_and_child(self):
        script='import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); print(p.pid,flush=True); sys.stdin.read(); time.sleep(60)'
        code,out,err,timed_out=consult._run([sys.executable,'-c',script],'test',dict(os.environ),0.5,self.root)
        self.assertTrue(timed_out)
        self.assertNotEqual(code,0)
        child=int(out.strip())
        # SIGKILL delivery to the grandchild can lag the leader's wait().
        if hasattr(os, 'pidfd_open'):
            try:
                fd = os.pidfd_open(child)
            except ProcessLookupError:
                return
            try:
                self.assertTrue(select.select([fd], [], [], 5)[0])
            finally:
                os.close(fd)
        stat=Path(f'/proc/{child}/stat')
        try:
            state = stat.read_text().rsplit(')',1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            pass  # Already reaped before opening or reading procfs.
        else:
            self.assertIn(state, ('Z','X'))

    def test_stderr_substring_does_not_fake_busy(self):
        result,_=self.call(transport=(1,'','diagnostic quote: hermes-refusal-reason: SESSION_NOT_OWNED suffix\n',False))
        self.assertEqual(result['status'],'failed')

    def test_reaped_leader_does_not_signal_reused_group(self):
        proc=Mock()
        proc.poll.return_value=0
        with patch.object(consult.os,'killpg') as kill:
            consult._stop_group(proc)
            kill.assert_not_called()
            proc.wait.assert_called_once_with(timeout=10)

    def test_cleanup_failure_preserves_interruption(self):
        proc=Mock()
        proc.communicate.side_effect=KeyboardInterrupt()
        with patch.object(consult.subprocess,'Popen',return_value=proc),patch.object(consult,'_stop_group',side_effect=RuntimeError('cleanup error')),self.assertLogs(consult.__name__,level='ERROR'):
            with self.assertRaises(KeyboardInterrupt):
                consult._run(['/cli/hermes'],'test',dict(os.environ),5,self.root)

    def test_interruption_cleans_up(self):
        proc=Mock()
        proc.communicate.side_effect=KeyboardInterrupt()
        with patch.object(consult.subprocess,'Popen',return_value=proc),patch.object(consult,'_stop_group') as stop:
            with self.assertRaises(KeyboardInterrupt):
                consult._run(['/cli/hermes'],'test',dict(os.environ),5,self.root)
            stop.assert_called_once_with(proc)
