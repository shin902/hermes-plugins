"""Security regressions for the canonical-ID policy."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_acl import acl
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class CanonicalAclTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.yaml"
        self.config.write_text("{}\n")
        (self.root / "profile.yaml").write_text("{}\n")
        self.target = self.root / "profiles/coding"
        self.target.mkdir(parents=True)
        (self.target / "profile.yaml").write_text("{}\n")
        self.token = set_hermes_home_override(self.root)
        self.addCleanup(reset_hermes_home_override, self.token)

    def call(self, target: object = "coding"):
        return acl.check_bot_acl("message_agent", {"target": target})

    def blocked(self, target: object = "coding"):
        self.assertEqual(self.call(target)["action"], "block")

    def test_valid_id_and_invalid_inputs(self):
        self.assertIsNone(self.call())
        self.assertIsNone(self.call("@coding"))
        for value in ("Friendly Name", "CODING", "@@coding", "coding@remote", "peer/coding", "../coding", None, 123):
            with self.subTest(value=value):
                self.blocked(value)

    def test_config_failures_and_peer_collision(self):
        for value in ("[\n", "[]\n", "bot_peers: []\n", "bot_peers: {1: {}}\n", "bot_peers: {coding: {}}\n"):
            with self.subTest(value=value):
                self.config.write_text(value)
                self.blocked()
        self.config.unlink()
        self.blocked()

    def test_default_handle_peer_collision(self):
        token = set_hermes_home_override(self.target)
        try:
            self.assertIsNone(self.call("@hermes"))
            self.config.write_text("bot_peers: {hermes: {}}\n")
            self.blocked("@hermes")
            self.assertIsNone(self.call("default"))
            self.config.write_text("bot_peers: {default: {}}\n")
            self.blocked("default")
        finally:
            reset_hermes_home_override(token)

    def test_missing_and_tombstoned_target(self):
        metadata = self.target / "profile.yaml"
        metadata.unlink()
        self.blocked()
        metadata.write_text("{}\n")
        marker = self.root / "profiles/.deleted/coding"
        marker.parent.mkdir()
        marker.touch()
        self.blocked()
        marker.unlink()
        self.assertIsNone(self.call())

    def test_ambiguous_group(self):
        with patch.object(acl, "DOMAINS", {"one": {"default", "coding"}, "two": {"default", "coding"}}):
            self.blocked()

    def test_no_private_core_imports(self):
        tree = ast.parse(Path(__file__).with_name("policy.py").read_text())
        imports = [(node.module, item.name) for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) for item in node.names
                   if node.module and node.module.startswith(("tools", "agent", "hermes"))]
        self.assertEqual(imports, [("hermes_constants", "get_hermes_home")])

    def test_registered_hook_allows_and_blocks(self):
        manager = PluginManager(str(self.root))
        acl.register(PluginContext(PluginManifest(name="bot-acl", provides_hooks=["pre_tool_call"]), manager))
        allowed = manager.invoke_hook("pre_tool_call", tool_name="message_agent", args={"target": "coding"})
        denied = manager.invoke_hook("pre_tool_call", tool_name="message_agent", args={"target": "local"})
        self.assertEqual(allowed, [])
        self.assertEqual(denied[0]["action"], "block")
