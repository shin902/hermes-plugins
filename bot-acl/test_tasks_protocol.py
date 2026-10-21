"""Protocol support for exact target-session resumption and process-shared ownership; no model calls."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_acl import acl

tasks = sys.modules[acl.__name__ + '.bot_task']


class GatewayProtocolSmoke(unittest.TestCase):
    def test_create_seeded_sessions_and_resume_in_one_process(self):
        import importlib.util
        spec = importlib.util.find_spec('tui_gateway')
        source = str(Path(spec.origin).parent.parent)
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HERMES_HOME=home, PYTHONPATH=source)
            env.pop('HERMES_TUI_ATTACH_URL', None)
            child = tasks.Child([sys.executable, '-m', 'tui_gateway.entry'], env, source)
            try:
                self.assertTrue(child.ready.wait(20))
                child.rpc('client.capabilities', {'server_requests': True})
                first = child.rpc('session.create', {
                    'source': 'bot_task', 'title': 'First stored task', 'close_on_disconnect': True,
                    'messages': [{'role': 'user', 'content': 'seed one'}]})
                second = child.rpc('session.create', {
                    'source': 'bot_task', 'title': 'Second task', 'close_on_disconnect': True,
                    'messages': [{'role': 'user', 'content': 'seed two'}]})
                self.assertNotEqual(first['session_id'], second['session_id'])
                listed = child.rpc('session.list', {'title': 'First stored task'})
                rows = listed['sessions']
                self.assertEqual(len(rows), 1)
                stored = rows[0].get('resolved_id') or rows[0]['id']
                self.assertEqual(stored, first['stored_session_id'])
                resumed = child.rpc('session.resume', {
                    'session_id': stored, 'omit_messages': True, 'close_on_disconnect': True})
                self.assertEqual(resumed.get('resumed') or resumed.get('session_key'), stored)
                active = child.rpc('session.active_list', {})
                ids = {row['id'] for row in active['sessions']}
                self.assertIn(resumed['session_id'], ids)
                self.assertIn(second['session_id'], ids)
            finally:
                child.close()


class NativeOwnershipTests(unittest.TestCase):
    def test_live_parallel_sessions_busy_and_crash_recovery(self):
        script = '''import sys,json
from hermes_cli.active_sessions import try_acquire_active_session,release_active_session
lease,error=try_acquire_active_session(session_id=sys.argv[2],surface=sys.argv[3],config={},registry_home=sys.argv[1])
print(json.dumps({"started":lease is not None}),flush=True)
if lease:
 sys.stdin.readline()
 release_active_session(lease)
'''
        with tempfile.TemporaryDirectory() as directory:
            processes=[]
            def start(sid,caller):
                proc=subprocess.Popen([sys.executable,'-c',script,directory,sid,caller],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                processes.append(proc)
                import select
                self.assertTrue(select.select([proc.stdout],[],[],5)[0])
                result=json.loads(proc.stdout.readline())
                return proc,result
            try:
                first,a=start('receiver-a','caller-one')
                second,b=start('receiver-a','caller-two')
                self.assertTrue(a['started'])
                self.assertFalse(b['started'])
                second.wait(timeout=5)
                other,c=start('receiver-b','bot_task-fixture')
                self.assertTrue(c['started'])
                self.assertIsNone(first.poll())
                self.assertIsNone(other.poll())
                first.kill()
                first.wait(timeout=5)
                recovered,d=start('receiver-a','caller-three')
                self.assertTrue(d['started'])
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        proc.communicate(input='release\n',timeout=5)
                    for stream in (proc.stdin,proc.stdout,proc.stderr):
                        stream.close()

    def test_process_exclusion_cross_caller_and_independent_sessions(self):
        # Exercise installed ownership implementation in separate OS processes.
        # This is a test-only import, NOT a plugin dependency or model invocation.
        from hermes_cli.active_sessions import try_acquire_active_session, release_active_session
        with tempfile.TemporaryDirectory() as directory:
            lease,error=try_acquire_active_session(session_id='same',surface='bot_task-fixture',config={},registry_home=directory)
            self.assertIsNone(error)
            script='''import sys,json
from hermes_cli.active_sessions import try_acquire_active_session,release_active_session
lease,error=try_acquire_active_session(session_id=sys.argv[2],surface="cli",config={},registry_home=sys.argv[1])
print(json.dumps({"started":lease is not None,"reason":str(getattr(error,"reason",""))}))
if lease: release_active_session(lease)
'''
            def call(sid):
                p=subprocess.run([sys.executable,'-c',script,directory,sid],capture_output=True,text=True,timeout=10)
                self.assertEqual(p.returncode,0,p.stderr)
                return json.loads(p.stdout)
            try:
                self.assertFalse(call('same')['started'])
                self.assertTrue(call('different')['started'])
            finally:
                release_active_session(lease)
            self.assertTrue(call('same')['started'])


if __name__ == '__main__':
    unittest.main()
