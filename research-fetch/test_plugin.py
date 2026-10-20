import importlib.util
import json
import pathlib
import shlex
import sys
import unittest
from unittest.mock import patch

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

ROOT = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location('research_fetch_test', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)
from research_fetch_test import arxiv_search as ax, common, youtube_transcript as yt

class Context:
    def __init__(self, result=None):
        self.tools = {}
        self.calls = []
        self.result = result if result is not None else {'exit_code': 0, 'output': '# Video\n[00:01] text\n'}
    def register_tool(self, **kw): self.tools[kw['name']] = kw
    def register_skill(self, name, path): pass
    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if isinstance(self.result, Exception): raise self.result
        return self.result

class Tests(unittest.TestCase):
    def test_profile_skills(self):
        # Exercise the real namespacing/path registrar without writing skill files.
        for home, present in (
            ('/profiles/default', {0, 1}),
            ('/profiles/other', set()),
            ('/profiles/partial', {1}),
        ):
            with self.subTest(home=home):
                paths = [pathlib.Path(home) / 'skills' / relative for relative in (
                    'media/youtube-transcript/SKILL.md',
                    'research/arxiv-research/SKILL.md')]
                expected = {paths[i] for i in present}
                manager = PluginManager()
                ctx = PluginContext(PluginManifest(name='research-fetch'), manager)
                with patch.dict('os.environ', {'HERMES_HOME': home}), \
                     patch.object(pathlib.Path, 'is_file', autospec=True,
                                  side_effect=lambda path: path in expected), \
                     patch.object(pathlib.Path, 'exists', autospec=True,
                                  side_effect=lambda path: path in expected), \
                     patch.object(ctx, 'register_tool') as tools:
                    p.register(ctx)
                self.assertEqual({call.kwargs['name'] for call in tools.call_args_list},
                                 {'youtube_transcript', 'arxiv_search'})
                for i, name in enumerate(('youtube-transcript', 'arxiv-research')):
                    self.assertEqual(manager.find_plugin_skill('research-fetch:' + name),
                                     paths[i] if i in present else None)

    def test_register(self):
        c = Context(); p.register(c)
        self.assertEqual(set(c.tools), {'youtube_transcript', 'arxiv_search'})
        for t in c.tools.values(): self.assertFalse(t['schema']['parameters']['additionalProperties'])
    def test_shell_injection(self):
        value = "-x '; touch /tmp/pwn; $(id)\n`id`"
        c = Context(); p.register(c)
        data = json.loads(c.tools['arxiv_search']['handler']({'query': value, 'title': value}))
        name, a = c.calls[0]; tokens = shlex.split(a['command'])
        self.assertEqual(name, 'terminal'); self.assertEqual(a['timeout'], 90)
        self.assertEqual(tokens[:3], ['python3', common.CLI, 'arxiv-search'])
        self.assertEqual(tokens[-2:], ['--', value]); self.assertIn('--title=' + value, tokens)
        self.assertIn('--no-save', tokens); self.assertIn('markdown', data)
    def test_youtube(self):
        argv = shlex.split(yt.command({'url': 'https://youtu.be/abc?x=1&y=2'}))
        self.assertIn('--subtitles', argv); self.assertNotIn('--sub-langs', argv)
        self.assertEqual(argv[-2], '--')
    def test_validation(self):
        for tool, args in [('youtube_transcript', {}), ('youtube_transcript', {'url':'https://x.com/a'}), ('arxiv_search', {'command':'id'}), ('arxiv_search', {'limit':True}), ('arxiv_search', {'limit':51}), ('arxiv_search', {'date_from':'2025-99-99'}), ('arxiv_search', {'query':'\x00'}), ('arxiv_search', {'sort':'bad'})]:
            c = Context(); self.assertIn('error', json.loads((yt if tool == 'youtube_transcript' else ax).run(c, args))); self.assertEqual(c.calls, [])
    def test_markdown_passthrough(self):
        c=Context(); self.assertEqual(json.loads(ax.run(c, {}))['markdown'], c.result['output'])
    def test_errors(self):
        for result in [RuntimeError('denied'), {'exit_code':1,'output':'API failed'}, {'error':'timeout'}, 'not json', {'exit_code':0}, []]:
            self.assertIn('error', json.loads(ax.run(Context(result), {})))
    def test_json_terminal(self):
        self.assertEqual(json.loads(ax.run(Context('{"exit_code":0,"output":"ok"}'), {})), {'markdown':'ok'})

if __name__ == '__main__': unittest.main()
