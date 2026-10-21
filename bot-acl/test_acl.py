"""Run: hermes --run-module unittest discover -s ~/.hermes/plugins/bot-acl -p test_acl.py"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

spec = importlib.util.spec_from_file_location("bot_acl", Path(__file__).with_name("__init__.py"))
assert spec is not None and spec.loader is not None
acl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acl
spec.loader.exec_module(acl)


class BotAclTests(unittest.TestCase):
    def test_local_domain_boundary_uses_canonical_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("bot_peers: {main-coding: {}}\n")
            profiles = {
                "default": (root, "Main Friendly Name", "Main Bot"),
                "main-research": (root / "profiles/main-research", "Main Research", "Research Bot"),
                "main-coding": (root / "profiles/main-coding", "Main Coding", "Coding Bot"),
                "local": (root / "profiles/local", "Local Friendly Name", "Main-looking Title"),
                "local-research": (root / "profiles/local-research", "Local Research", "Local Bot"),
                "unknown-profile": (root / "profiles/unknown-profile", "Unknown", "Unknown Bot"),
            }
            for home, display, title in profiles.values():
                home.mkdir(parents=True, exist_ok=True)
                (home / "profile.yaml").write_text(
                    f"display_name: {display}\nui_meta:\n  hermes-bots:\n    title: {title}\n"
                )
            domains = {
                "main": {"default", "main-research", "main-coding"},
                "local": {"local", "local-research"},
            }
            cases = [
                ("default", "main-research", True),
                ("main-research", "default", True),
                ("local", "local-research", True),
                ("local-research", "local", True),
                ("default", "local", False),
                ("local", "default", False),
                ("main-research", "local-research", False),
                ("unknown-profile", "default", False),
                ("default", "unknown", False),
                ("default", "@Local Friendly Name", False),
                ("default", "@local-friendly-name", False),
                ("default", "@Main-looking Title", False),
                ("local-research", "@Local Friendly Name", False),
                ("main-research", "@Main Bot", False),
                ("main-research", "@hermes", True),
                ("default", "foo@other-machine", False),
                ("default", "peer/foo", False),
                ("default", "main-coding", False),
                ("default", "@hermes", False),
                ("default", "", False),
            ]
            with patch.dict(os.environ, HERMES_HOME=str(root)), patch.object(acl, "DOMAINS", domains):
                for source, target, allowed in cases:
                    with self.subTest(source=source, target=target):
                        token = set_hermes_home_override(profiles[source][0])
                        try:
                            response = acl.check_bot_acl("message_agent", {"target": target, "source": "default"})
                        finally:
                            reset_hermes_home_override(token)
                        if allowed:
                            self.assertIsNone(response)
                        else:
                            self.assertEqual(response["action"], "block")
                with patch("hermes_constants.get_hermes_home", side_effect=RuntimeError("scope failed")):
                    self.assertEqual(acl.check_bot_acl("message_agent", {"target": "default"})["action"], "block")
                    self.assertIsNone(acl.check_bot_acl("terminal", object()))

    def test_bot_task_spawn_is_covered_by_pre_tool_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("{}\n")
            for name in ("coding", "mail", "local"):
                home = root / "profiles" / name
                home.mkdir(parents=True)
                (home / "profile.yaml").write_text("{}\n")
            domains = {"main": {"coding", "mail"}, "local": {"local"}}
            token = set_hermes_home_override(root / "profiles/coding")
            try:
                with patch.object(acl, "DOMAINS", domains):
                    blocked = acl.check_bot_acl("bot_task", {"action": "spawn", "target": "local"})
                    self.assertEqual(blocked["action"], "block")
                    self.assertIsNone(acl.check_bot_acl("bot_task", {"action": "spawn", "target": "mail"}))
                    self.assertIsNone(acl.check_bot_acl("bot_task", {"action": "status", "task_id": "t"}))
            finally:
                reset_hermes_home_override(token)

    def test_multiplex_hook_workers_preserve_each_source_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("{}\n")
            local = root / "profiles/local"
            local.mkdir(parents=True)
            (local / "profile.yaml").write_text("{}\n")
            manager = PluginManager(str(root))
            acl.register(PluginContext(PluginManifest(name="bot-acl", provides_hooks=["pre_tool_call"]), manager))

            async def invoke(home, target):
                token = set_hermes_home_override(home)
                try:
                    await asyncio.sleep(0)
                    return manager.invoke_hook("pre_tool_call", tool_name="message_agent", args={"target": target})
                finally:
                    reset_hermes_home_override(token)

            async def both():
                return await asyncio.gather(invoke(root, "local"), invoke(local, "default"))

            with patch.dict(os.environ, HERMES_HOME=str(root)):
                responses = asyncio.run(both())
            for response, source in zip(responses, ("default", "local")):
                self.assertEqual(response[0]["action"], "block")
                self.assertIn(f"source='{source}'", response[0]["message"])


if __name__ == "__main__":
    unittest.main()
