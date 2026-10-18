"""Registered public hooks/handlers and real process admission tests; no model."""
import json
import multiprocessing
from unittest.mock import patch
from test_consult import ConsultTests, consult
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class RemainingTests(ConsultTests):
    def context(self):
        return PluginContext(PluginManifest(name='bot-acl'), PluginManager(str(self.root)))

    def test_registered_handler_process_busy_independent_and_release(self):
        ctx=self.context()
        handlers={}
        original=ctx.register_tool
        def register(name,toolset,schema,handler,**kwargs):
            handlers[name]=handler
            return original(name,toolset,schema,handler,**kwargs)
        with patch.object(ctx,'register_tool',side_effect=register):
            consult.register_consult(ctx,self.domains)
        handler=handlers['ask_bot']
        context=multiprocessing.get_context('fork')
        parent,child=context.Pipe()
        def holder():
            def run(*args):
                child.send('locked')
                child.recv()
                return 0,self.output,'',False
            with patch.object(consult,'_run',side_effect=run),patch.object(consult.shutil,'which',return_value='/fixture/hermes'):
                child.send(json.loads(handler({**self.args,'mode':'bot_chat'})))
        process=context.Process(target=holder)
        process.start()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(),'locked')
            with patch.object(consult,'_run',return_value=(0,self.output,'',False)) as run,patch.object(consult.shutil,'which',return_value='/fixture/hermes'):
                self.assertEqual(json.loads(handler({**self.args,'mode':'bot_chat'}))['status'],'target_busy')
                run.assert_not_called()
                self.assertEqual(json.loads(handler(self.args))['status'],'completed')
                self.assertEqual(json.loads(handler({**self.args,'target':'default','mode':'bot_chat'}))['status'],'completed')
            parent.send('release')
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv()['status'],'completed')
            process.join(5)
            self.assertEqual(process.exitcode,0)
            result,_=self.call({**self.args,'mode':'bot_chat'})
            self.assertEqual(result['status'],'completed')
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            parent.close()
            child.close()

    def test_lock_failure_closed(self):
        with patch.object(consult.os,'open',side_effect=PermissionError('lock denied')),patch.object(consult,'_run') as run:
            # Direct _ask bypasses audit file creation to isolate admission failure.
            with patch.object(consult.shutil,'which',return_value='/fixture/hermes'):
                result=json.loads(consult._ask(self.domains,self.args))
            self.assertEqual(result['status'],'failed')
            run.assert_not_called()
