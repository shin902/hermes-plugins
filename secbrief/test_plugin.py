"""Smallest runnable contract check for the skill-only secbrief plugin.

No Hermes imports: the plugin is loaded directly and registered against a tiny
recording context. The check covers manifest/reference paths, the single-skill
registration contract, and the operational claims
that must remain true in SKILL.md.
"""

import ast
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL_DIR = ROOT / "skills" / "secbrief-operations"
SKILL_MD = SKILL_DIR / "SKILL.md"
MANIFEST = ROOT / "plugin.yaml"


def _scalar(text, key):
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def _frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = re.search(r"\n---\s*\n", text[3:])
    if end is None:
        return {}
    block = text[3:end.start() + 3]
    return {
        key: value.strip().strip("\"'")
        for key, value in re.findall(r"^([A-Za-z0-9_-]+):\s*(.*)$", block, re.MULTILINE)
    }


class RecordingContext:
    def __init__(self):
        self.skills = []

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, Path(path), description, frontmatter))

    def __getattr__(self, name):
        if name.startswith("register_"):
            raise AssertionError(f"skill-only plugin called ctx.{name}")
        raise AttributeError(name)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("secbrief_plugin_contract", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecbriefPluginContract(unittest.TestCase):
    def test_manifest(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertEqual(_scalar(text, "name"), "secbrief")
        self.assertEqual(_scalar(text, "kind"), "standalone")
        self.assertTrue(_scalar(text, "description"))
        version = _scalar(text, "version")
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        for unsupported in ("provides_tools", "provides_hooks", "provides_commands", "provides_skills"):
            self.assertIsNone(
                re.search(rf"^{unsupported}:", text, re.MULTILINE),
                f"skill-only plugin must not declare {unsupported}",
            )

    def test_register_only_the_operations_skill(self):
        module = _load_plugin()
        ctx = RecordingContext()
        module.register(ctx)
        self.assertEqual(len(ctx.skills), 1)
        name, path, description, frontmatter = ctx.skills[0]
        self.assertEqual(name, "secbrief-operations")
        self.assertEqual(path.resolve(), SKILL_MD.resolve())
        self.assertTrue(path.is_file())
        self.assertTrue(description)
        self.assertIsNone(frontmatter)

        tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertIn("pathlib", imported)
        for module_name in imported:
            self.assertNotIn(
                module_name.split(".", 1)[0],
                {"hermes", "hermes_cli", "tools", "agent", "plugins"},
                f"plugin must not import Hermes internals: {module_name}",
            )

    def test_skill_frontmatter_and_operational_contract(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        frontmatter = _frontmatter(text)
        self.assertEqual(frontmatter.get("name"), "secbrief-operations")
        self.assertTrue(frontmatter.get("description"))

        for required in (
            "SECBRIEF_HOME",
            "skills.config.secbrief.home",
            "extras/secbrief_extra.py inventory",
            "extras/secbrief_extra.py run",
            "extras/automation.py cycle",
            "extras/secbrief_extra.py render",
            "secbrief.py doctor",
            "extras/secbrief_extra.py doctor",
            "--online",
            "--probe-runtime",
            "inventory_sources_online",
            "inventory_sources_probe_runtimes",
            "changed",
            "nochange",
            "failed",
            "inventory_only",
            "Hermes",
            "cron",
            "unknown",
            "完全なカバレッジ",
            "本番運用",
        ):
            self.assertIn(required, text, f"SKILL.md must document {required!r}")

        for prohibited in ("/home/shi", "secbrief-reference"):
            self.assertNotIn(prohibited, text)

        self.assertIn("自動実行しない", text)
        self.assertIn("更新", text)
        self.assertIn("再起動", text)
        self.assertIn("修復", text)


if __name__ == "__main__":
    unittest.main()
