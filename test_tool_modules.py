"""Public tool registration parity and tool-named module ownership.

The baseline is read from git HEAD, extracted into an isolated temporary tree,
loaded under distinct module names and exercised only with an in-memory
registration context. No tool handler is called and no live service is touched.
"""

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


REPO = Path(__file__).resolve().parent
PLUGINS = {
    'bot-acl': 'bot_acl',
    'hermes-cli': 'hermes_cli_plugin',
    'host-android': 'host_android_plugin',
    'host-github': 'host_github_plugin',
    'host-ops': 'host_ops_plugin',
    'host-transfer': 'host_transfer_plugin',
    'mail-monitor': 'mail_monitor_plugin',
    'research-fetch': 'research_fetch_plugin',
    'rss-monitor': 'rss_monitor_plugin',
}
OWNERS = {
    'ask_bot': ('bot-acl', 'ask_bot.py'),
    'list_bot_peers': ('bot-acl', 'list_bot_peers.py'),
    'bot_task': ('bot-acl', 'bot_task.py'),
    'hermes_cli': ('hermes-cli', 'hermes_cli.py'),
    'host_android': ('host-android', 'host_android.py'),
    'host_github': ('host-github', 'host_github.py'),
    'host_ops': ('host-ops', 'host_ops.py'),
    'host_transfer': ('host-transfer', 'host_transfer.py'),
    'mail_publish': ('mail-monitor', 'mail_publish.py'),
    'youtube_transcript': ('research-fetch', 'youtube_transcript.py'),
    'arxiv_search': ('research-fetch', 'arxiv_search.py'),
    'rss_publish': ('rss-monitor', 'rss_publish.py'),
}


class Registration:
    """Small public registration surface; records metadata, never handlers."""

    def __init__(self):
        self.tools = []
        self.hooks = []
        self.middleware = []
        self.prompts = []
        self.cli = []
        self.skills = []

    def register_tool(self, *args, **kwargs):
        if args:
            name, toolset, schema = args[0], args[1], args[2]
        else:
            name, toolset, schema = kwargs['name'], kwargs.get('toolset'), kwargs['schema']
        self.tools.append({
            'name': name,
            'toolset': toolset,
            'schema': schema,
            'description': kwargs.get('description'),
            'emoji': kwargs.get('emoji'),
            'check': kwargs.get('check_fn') is not None,
        })

    def register_hook(self, name, function, **kwargs):
        self.hooks.append((name, getattr(function, '__name__', None)))

    def register_middleware(self, name, function, **kwargs):
        self.middleware.append((name, getattr(function, '__name__', None)))

    def register_system_prompt_section(self, name, text, **kwargs):
        self.prompts.append((name, text, kwargs.get('position')))

    def register_cli_command(self, name, description, setup, handler, **kwargs):
        self.cli.append((name, description))

    def register_skill(self, name, path):
        self.skills.append((name, str(path)))

    def surface(self):
        return {
            'tools': self.tools,
            'hooks': self.hooks,
            'middleware': self.middleware,
            'prompts': self.prompts,
            'cli': self.cli,
            'skills': self.skills,
        }


def load_package(directory, module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, directory / '__init__.py',
        submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def register_package(directory, module_name, home):
    module = load_package(directory, module_name)
    context = Registration()
    if module_name.endswith('research_fetch_plugin'):
        patch_target = patch.object(module, 'get_hermes_home', return_value=home)
    else:
        patch_target = patch.dict(os.environ, {'HERMES_HOME': str(home)})
    with patch_target:
        module.register(context)
    return module, context.surface()


def git(*args):
    return subprocess.run(
        ['git', '-C', str(REPO)] + list(args),
        check=False, capture_output=True)


def extract_head(plugin, destination):
    archive = git('archive', '--format=tar', 'HEAD', '--', plugin)
    if archive.returncode != 0:
        raise RuntimeError(archive.stderr.decode('utf-8', 'replace'))
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:') as bundle:
        root = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError('unsafe archive member: ' + member.name)
        bundle.extractall(destination)


class ToolModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.temp.name) / 'home'
        cls.home.mkdir()
        cls.current = {}
        cls.modules = {}
        cls.current_names = {}
        for plugin, module_name in PLUGINS.items():
            current_name = '_tool_modules_current_' + module_name
            module, surface = register_package(REPO / plugin, current_name, cls.home)
            cls.current[plugin] = surface
            cls.modules[plugin] = module
            cls.current_names[plugin] = current_name

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_public_tools_are_owned_by_tool_named_modules(self):
        registered = {}
        for plugin, surface in self.current.items():
            for tool in surface['tools']:
                self.assertNotIn(tool['name'], registered,
                                 f"duplicate public tool {tool['name']}")
                registered[tool['name']] = plugin
        self.assertEqual(set(registered), set(OWNERS))

        for tool_name, (plugin, filename) in OWNERS.items():
            owner = REPO / plugin / filename
            self.assertTrue(owner.is_file(), str(owner))
            init = (REPO / plugin / '__init__.py').read_text(encoding='utf-8')
            self.assertNotIn('register_tool', init,
                             f'{plugin}/__init__.py must not register tools')

            module_name = self.current_names[plugin] + '.' + owner.stem
            module = sys.modules[module_name]
            self.assertEqual(Path(module.__file__).resolve(), owner.resolve())
            context = Registration()
            domains = getattr(self.modules[plugin], 'DOMAINS', {})
            with patch.dict(os.environ, {'HERMES_HOME': str(self.home)}):
                if plugin == 'bot-acl':
                    module.register(context, domains)
                else:
                    module.register(context)
            self.assertEqual([item['name'] for item in context.tools], [tool_name])

    def test_registration_and_schemas_match_git_head(self):
        if git('cat-file', '-e', 'HEAD:bot-acl/consult.py').returncode != 0:
            self.skipTest('git HEAD does not contain the pre-refactor layout')

        baseline_root = Path(self.temp.name) / 'baseline'
        baseline_root.mkdir()
        baseline = {}
        token = set_hermes_home_override(self.home)
        try:
            for plugin, module_name in PLUGINS.items():
                extract_head(plugin, baseline_root)
                baseline_name = '_tool_modules_head_' + module_name
                _, surface = register_package(
                    baseline_root / plugin, baseline_name, self.home)
                baseline[plugin] = surface
        finally:
            reset_hermes_home_override(token)

        self.assertEqual(baseline, self.current)


if __name__ == '__main__':
    unittest.main()
