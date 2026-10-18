"""Provider-payload and protocol-preservation regressions for roster hiding."""
import ast
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_acl import acl
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

roster = sys.modules[acl.__name__ + '.roster']
PROTOCOL = (
    '## Messaging other agents\n'
    'Delivery is FIRE-AND-FORGET. COMPOSE your own message.\n'
    'You are `@coding`. Your teammates (live roster; roles from their profiles):\n'
    '- `@hermes` — Main bot\n- `@mail` — Mail role\n- `@local` — Local role\n'
    '- `@logbook` — Private role\n'
    '\nTeammates on OTHER connected machines:\n- `@remote` — Other machine\n'
    '\nTeammates on OTHER machines: peer gateways `peer`.\n'
    '\nCapability epoch: abcdef123456\n\nPlain terminal instruction without heading.\n'
    '\n## Skills\nKeep skills.\n'
)


class RosterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config.yaml').write_text('{}\n')
        self.homes = {}
        for name in ('default', 'coding', 'mail', 'local', 'logbook'):
            home = self.root if name == 'default' else self.root / 'profiles' / name
            home.mkdir(parents=True, exist_ok=True)
            (home / 'profile.yaml').write_text('{}\n')
            self.homes[name] = home
        self.domains = {'main': {'default','coding','mail'}, 'local': {'local','logbook'}}
        patcher = patch.object(acl, 'DOMAINS', self.domains)
        patcher.start()
        self.addCleanup(patcher.stop)
        token = set_hermes_home_override(self.homes['coding'])
        self.addCleanup(reset_hermes_home_override, token)
        roster._visible_ids.cache_clear()

    def apply(self, request, session='test-session'):
        result = acl.filter_bot_roster(request, session_id=session)
        return result['request'] if result else request

    def check_prompt(self, text):
        self.assertIn('`@default` — Main bot', text)
        self.assertIn('`@mail` — Mail role', text)
        for hidden in ('`@local`','`@logbook`','`@remote`','`peer`'):
            self.assertNotIn(hidden, text)
        self.assertIn('FIRE-AND-FORGET', text)
        self.assertIn('COMPOSE your own message', text)
        self.assertIn('Capability epoch: abcdef123456', text)
        self.assertIn('Plain terminal instruction without heading.', text)
        self.assertIn('Keep skills.', text)

    def test_chat_completions_preserves_history_and_source(self):
        request = {'messages':[{'role':'system','content':PROTOCOL},
                               {'role':'user','content':PROTOCOL},
                               {'role':'assistant','content':'Earlier @logbook'},
                               {'role':'tool','content':'Earlier @local'}], 'tools':[{'name':'message_agent'}]}
        original = copy.deepcopy(request)
        output = self.apply(request)
        self.check_prompt(output['messages'][0]['content'])
        self.assertEqual(output['messages'][1:], original['messages'][1:])
        self.assertEqual(output['tools'], original['tools'])
        self.assertEqual(request, original)

    def test_provider_text_block_shapes(self):
        for field, role, kind in [('messages','system','text'), ('messages','developer','text'), ('input','developer','input_text')]:
            with self.subTest(field=field, role=role):
                request = {field:[{'role':role,'content':[{'type':kind,'text':PROTOCOL,'cache_control':{'type':'ephemeral'}}]}]}
                part = self.apply(request)[field][0]['content'][0]
                self.check_prompt(part['text'])
                self.assertEqual(part['cache_control'], {'type':'ephemeral'})

    def test_responses_instructions(self):
        output = self.apply({'instructions':PROTOCOL,'input':[{'role':'user','content':'Ask @local'}]})
        self.check_prompt(output['instructions'])
        self.assertEqual(output['input'][0]['content'], 'Ask @local')

    def test_local_group_is_isolated(self):
        token = set_hermes_home_override(self.homes['local'])
        try:
            text = self.apply({'instructions':PROTOCOL})['instructions']
            self.assertIn('`@logbook`', text)
            self.assertNotIn('`@mail`', text)
            self.assertNotIn('`@default`', text)
        finally:
            reset_hermes_home_override(token)

    def test_config_only_local_profile_is_supported(self):
        (self.homes['local'] / 'profile.yaml').unlink()
        (self.homes['local'] / 'config.yaml').write_text('{}\n')
        token = set_hermes_home_override(self.homes['local'])
        try:
            self.assertIsNone(acl.check_bot_acl('message_agent', {'target':'logbook'}))
            text = self.apply({'instructions':PROTOCOL})['instructions']
            self.assertIn('`@logbook`', text)
        finally:
            reset_hermes_home_override(token)
        token = set_hermes_home_override(self.homes['logbook'])
        try:
            self.assertIsNone(acl.check_bot_acl('message_agent', {'target':'local'}))
        finally:
            reset_hermes_home_override(token)

    def test_session_roster_is_frozen_but_sending_remains_live(self):
        request = {'instructions':PROTOCOL}
        before = self.apply(request)
        (self.root / 'config.yaml').write_text('bot_peers: {mail: {}}\n')
        self.assertEqual(self.apply(request), before)
        self.assertEqual(acl.check_bot_acl('message_agent', {'target':'mail'})['action'], 'block')
        self.assertNotIn('`@mail`', self.apply(request, session='new-session')['instructions'])

    def test_scope_error_and_unknown_group_hide_all(self):
        with patch('hermes_constants.get_hermes_home', side_effect=RuntimeError('unavailable')), self.assertLogs(roster.logger, level='ERROR'):
            text = self.apply({'instructions':PROTOCOL})['instructions']
            self.assertIn('(no permitted teammates)', text)
            self.assertNotIn('`@logbook`', text)
        with patch.object(acl, 'DOMAINS', {}):
            self.assertIn('(no permitted teammates)', self.apply({'instructions':PROTOCOL})['instructions'])

    def test_no_roster_is_no_op(self):
        self.assertIsNone(acl.filter_bot_roster({'instructions':'No roster here'}, session_id='test'))
        self.assertIsNone(acl.filter_bot_roster({'input':'User input string'}, session_id='test'))

    def test_crlf_and_unrelated_instruction_after_roster(self):
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=newline):
                prompt = PROTOCOL.replace('\nTeammates on OTHER connected machines:',
                    '\nKeep this independent instruction.\n\nTeammates on OTHER connected machines:')
                prompt = prompt.replace('\n', newline)
                text = self.apply({'instructions':prompt})['instructions']
                self.check_prompt(text)
                self.assertIn('Keep this independent instruction.', text)

    def test_filter_is_idempotent(self):
        once = self.apply({'instructions':PROTOCOL})
        self.assertEqual(self.apply(once), once)

    def test_standard_registration_and_no_private_imports(self):
        manager = PluginManager(str(self.root))
        acl.register(PluginContext(PluginManifest(name='bot-acl'), manager))
        results = manager.invoke_middleware('llm_request', request={'instructions':PROTOCOL},session_id='registered')
        self.check_prompt(results[0]['request']['instructions'])
        tree = ast.parse(Path(__file__).with_name('roster.py').read_text())
        core_imports = [(node.module, item.name) for node in ast.walk(tree)
                        if isinstance(node,ast.ImportFrom) and node.module
                        and node.module.startswith(('hermes','tools','agent')) for item in node.names]
        self.assertEqual(core_imports, [('hermes_constants','get_hermes_home')])
