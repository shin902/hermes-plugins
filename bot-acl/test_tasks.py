"""Parent ownership and safe JSON-RPC task behavior."""
import json
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock
from test_acl import acl

tasks = sys.modules[acl.__name__ + '.bot_task']

class TaskTests(unittest.TestCase):
    def setUp(self):
        self.saved = tasks._TASKS.copy()
        tasks._TASKS.clear()
        self.addCleanup(lambda: (tasks._TASKS.clear(), tasks._TASKS.update(self.saved)))
        self.child = Mock(closed=False, sid='child-only', reply='result', deadline=time.monotonic()+30, outcome='completed')
        self.child.done = threading.Event()
        tasks._TASKS['t'] = {'id':'t', 'owner':('/parent', 'parent'), 'target':'coding', 'child':self.child, 'status':'running', 'lock':threading.RLock()}
        self.home = patch.object(tasks.ask_bot, 'get_hermes_home', return_value='/parent')
        self.acl = patch.object(tasks, 'check', return_value=None)
        self.depth = patch.dict(tasks.os.environ, {tasks.ask_bot.DEPTH_ENV:'0'})
        for p in (self.home,self.acl,self.depth):
            p.start(); self.addCleanup(p.stop)
        ctx = Mock()
        acl.register(ctx)
        registrations = [c for c in ctx.register_tool.call_args_list if c.args[0] == 'bot_task']
        self.assertEqual(len(registrations), 1)
        self.callback = registrations[0].args[3]

    def call(self, action, **args):
        return json.loads(self.callback({'action':action, **args}, session_id='parent'))

    def test_no_arbitrary_session(self):
        r = self.call('status', task_id='t', session_id='victim')
        self.assertEqual(r['status'], 'failed')
        self.child.rpc.assert_not_called()

    def test_other_parent_cannot_read(self):
        r=json.loads(tasks.handle({}, {'action':'status','task_id':'t'}, session_id='other'))
        self.assertEqual(r['status'],'failed')
        self.child.rpc.assert_not_called()

    def test_missing_parent_rejected(self):
        self.assertEqual(json.loads(tasks.handle({}, {'action':'list'}))['status'],'failed')

    def test_steer_is_not_claimed_delivered(self):
        self.child.rpc.return_value={'status':'queued'}
        r=self.call('steer',task_id='t',message='correction')
        self.assertEqual(r['status'],'accepted_unconfirmed')
        self.assertFalse(r['active_turn_delivery_verified'])
        self.child.rpc.assert_called_once_with('session.steer',{'session_id':'child-only','text':'correction'})

    def test_status_waits_for_settled_turn(self):
        self.child.done.set()
        self.child.rpc.return_value={'running':False}
        self.assertEqual(self.call('status',task_id='t')['status'],'completed')
        self.child.close.assert_not_called()

    def test_stop_reaps_even_rpc_failure(self):
        self.child.rpc.side_effect=RuntimeError('broken')
        self.call('stop',task_id='t')
        self.child.close.assert_called_once()

    def test_revoked_acl_hides_list_and_blocks_control(self):
        with patch.object(tasks,'check',return_value={'message':'revoked'}):
            self.assertEqual(self.call('list')['tasks'],[])
            self.assertEqual(self.call('stop',task_id='t')['status'],'blocked')
            self.child.rpc.assert_not_called()

    def test_nested_tasks_blocked(self):
        with patch.dict(tasks.os.environ,{tasks.ask_bot.DEPTH_ENV:'1'}):
            self.assertEqual(self.call('list')['status'],'failed')

    def test_registration(self):
        ctx=Mock()
        tasks.register(ctx,{})
        self.assertEqual(ctx.register_tool.call_args.args[:3],('bot_task','bot_consult',tasks.SCHEMA))
