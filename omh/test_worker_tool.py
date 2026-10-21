"""Integration tests for the coding profile's omh_worker dispatch entry.

Run from the repository root:

    python3 -m unittest discover -s omh -p 'test_worker_tool.py' -v

The plugin is registered through a fake public context. Worker execution is a
local fake; no OpenCode process, provider, or network is used.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch


OMH_DIR = Path(__file__).resolve().parent
REPO = OMH_DIR.parent
PINNED_MODEL = "opencode/deepseek-v4.1-flash-free"


class FakeContext:
    """Only the documented registration methods OMH actually calls."""

    def __init__(self, profile_name: str) -> None:
        self.profile_name = profile_name
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: list[str] = []
        self.prompts: dict[str, dict[str, Any]] = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            **kwargs,
        }

    def register_hook(self, name, handler):
        self.hooks.append(name)

    def register_memory_provider(self, provider):
        return provider

    def register_system_prompt_section(self, section_id, content, **kwargs):
        if section_id in self.prompts:
            raise ValueError(f"section {section_id} already registered")
        self.prompts[section_id] = {"content": content, **kwargs}

    def get_config(self, key, default=None):
        return default


class RegisterForwardingContext:
    """Mimic the host's memory-provider collector: only ``register_*`` forwards.

    The real collector's ``__getattr__`` raises for non-registration attributes,
    including the documented ``profile_name`` property.
    """

    def __init__(self, delegate: FakeContext) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str):
        if name.startswith("register_"):
            return getattr(self.delegate, name)
        raise AttributeError(name)


def load_plugin(name: str, profile_name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        OMH_DIR / "__init__.py",
        submodule_search_locations=[str(OMH_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    ctx = FakeContext(profile_name)
    module.register(ctx)
    return module, ctx


class WorkerToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.cwd = Path(cls.temp.name) / "work"
        cls.cwd.mkdir()
        cls.module, cls.ctx = load_plugin("_omh_worker_tool_test", "coding")

    def test_profile_is_checked_at_dispatch(self):
        _, ctx = load_plugin("_omh_worker_dynamic_profile_test", "default")
        handler = ctx.tools["omh_worker"]["handler"]
        args = {"action": "select", "role": "implementation", "task": "probe"}
        self.assertEqual(json.loads(handler(args))["reason"], "profile_not_coding")
        ctx.profile_name = "coding"
        self.assertTrue(json.loads(handler(args))["ok"])
        ctx.profile_name = "default"
        self.assertEqual(
            json.loads(handler(args, profile_name="coding"))["reason"],
            "profile_not_coding",
        )

    def test_public_profile_property_across_thread_contexts(self):
        import contextvars
        from concurrent.futures import ThreadPoolExecutor
        try:
            from hermes_cli.plugins import PluginContext
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        except ImportError:
            self.skipTest("Hermes public context API is not installed")

        class LiveContext(FakeContext):
            profile_name = PluginContext.profile_name

            def __init__(self):
                self.tools, self.hooks, self.prompts = {}, [], {}

        ctx = LiveContext()
        root = Path.home() / ".hermes"
        token = set_hermes_home_override(root)
        try:
            # Provider persistence is unrelated to dispatch/profile propagation.
            with patch.object(sys.modules[self.module.__name__ + ".memory_provider"],
                              "OmhMemoryProvider", return_value=object()):
                self.module.register(ctx)
        finally:
            reset_hermes_home_override(token)
        handler = ctx.tools["omh_worker"]["handler"]
        args = {"action": "select", "role": "implementation", "task": "probe"}
        for home, allowed in ((root / "profiles" / "coding", True), (root, False)):
            token = set_hermes_home_override(home)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = json.loads(pool.submit(
                        contextvars.copy_context().run, handler, args, profile_name="coding"
                    ).result())
                self.assertEqual(result["ok"], allowed, result)
            finally:
                reset_hermes_home_override(token)

    def test_register_only_forwarding_context_does_not_replace_worker(self):
        # The memory-provider loader registers through a collector/remote context
        # that forwards register_* calls but cannot answer ctx.profile_name. It
        # must not replace the plugin loader's working omh_worker handler with
        # one that can never name the profile.
        module, ctx = load_plugin("_omh_worker_forwarding_profile_test", "coding")
        handler = ctx.tools["omh_worker"]["handler"]
        module.register(RegisterForwardingContext(ctx))
        self.assertIs(ctx.tools["omh_worker"]["handler"], handler)
        args = {"action": "select", "role": "implementation", "task": "probe"}
        self.assertTrue(json.loads(handler(args))["ok"])
        ctx.profile_name = "default"
        self.assertEqual(json.loads(handler(args))["reason"], "profile_not_coding")

    def call(self, **args):
        # Read the handler from the registry dict: a partial stored as a class
        # attribute would descriptor-bind to the test instance on Python 3.14.
        handler = self.ctx.tools["omh_worker"]["handler"]
        return json.loads(handler(args))

    def test_registration_keeps_existing_surface_and_adds_worker(self):
        self.assertEqual(len(self.ctx.tools), 23)
        self.assertIn("omh_worker", self.ctx.tools)
        self.assertEqual(len(self.ctx.hooks), 10)
        self.assertEqual(
            set(self.ctx.prompts), {"omh.awareness", "omh.worker-dispatch"}
        )
        schema = self.ctx.tools["omh_worker"]["schema"]
        self.assertEqual(set(schema["parameters"]["required"]), {"role", "task"})
        self.assertNotIn("model", schema["parameters"]["properties"])
        self.assertNotIn("owner", schema["parameters"]["properties"])
        self.assertNotIn("executor", schema["parameters"]["properties"])

    def test_implementation_selects_and_executes_pinned_opencode_worker(self):
        observed = {}

        def fake_worker(**kwargs):
            observed.update(kwargs)
            return {
                "ok": True,
                "model": PINNED_MODEL,
                "cwd": str(kwargs["cwd"]),
                "session_id": "ses_generated123",
                "requested_session_id": kwargs["session_id"],
                "resumed": True,
                "exit_code": 0,
                "opencode_exit_code": 0,
                "timed_out": False,
                "duration_seconds": 1.0,
                "text": "AUTO_OK",
            }

        with patch.object(self.module.opencode_worker, "run_worker", fake_worker):
            result = self.call(
                role="implementation",
                task="Implement the change",
                context="Focused tests only",
                cwd=str(self.cwd),
                timeout_seconds=30,
                session_id="ses_resume123",
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selection"]["executor"], "opencode")
        self.assertEqual(result["selection"]["model"], PINNED_MODEL)
        self.assertEqual(result["model"], PINNED_MODEL)
        self.assertEqual(result["session_id"], "ses_generated123")
        self.assertEqual(result["text"], "AUTO_OK")
        self.assertEqual(observed["cwd"], self.cwd.resolve())
        self.assertEqual(observed["session_id"], "ses_resume123")
        self.assertEqual(observed["timeout"], 30)
        self.assertFalse(observed["auto"])
        self.assertIn("Implement the change", observed["prompt"])
        self.assertIn("Focused tests only", observed["prompt"])

    def test_select_previews_without_spawning(self):
        def fail(**kwargs):
            raise AssertionError("select must not spawn a worker")

        with patch.object(self.module.opencode_worker, "run_worker", fail):
            result = self.call(
                action="select",
                role="builder",
                task="Implement the change",
                cwd=str(self.cwd),
            )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["executed"])
        self.assertEqual(result["role"], "implementation")
        self.assertEqual(result["selection"]["executor"], "opencode")
        self.assertEqual(result["selection"]["model"], PINNED_MODEL)

    def test_nonimplementation_roles_never_execute_opencode(self):
        def fail(**kwargs):
            raise AssertionError("non-implementation roles must not run OpenCode")

        for role in ("research", "researcher", "review", "reviewer", "parent"):
            with self.subTest(role=role):
                with patch.object(self.module.opencode_worker, "run_worker", fail):
                    result = self.call(
                        role=role,
                        task="Inspect the change",
                        cwd=str(self.cwd),
                    )
                self.assertTrue(result["ok"], result)
                self.assertFalse(result["executed"])
                self.assertEqual(result["status"], "payload_ready")
                self.assertEqual(
                    result["selection"]["executor"], "hermes_delegate_task"
                )
                self.assertEqual(
                    result["selection"]["delegation_config"], "unchanged"
                )
                self.assertEqual(
                    result["delegate_task"],
                    {
                        "tasks": [
                            {
                                "goal": "Inspect the change",
                                "context": (
                                    f"Working directory: {self.cwd.resolve()}"
                                ),
                            }
                        ]
                    },
                )

    def test_delegate_payload_matches_exposed_schema(self):
        with patch.object(
            self.module.opencode_worker,
            "run_worker",
            side_effect=AssertionError("research must not run OpenCode"),
        ):
            with_context = self.call(
                role="research",
                task="Inspect the change",
                context="Background only",
            )
            without_context = self.call(
                role="review",
                task="Review the change",
            )

        self.assertEqual(
            with_context["delegate_task"],
            {"tasks": [{"goal": "Inspect the change", "context": "Background only"}]},
        )
        self.assertEqual(
            without_context["delegate_task"],
            {"tasks": [{"goal": "Review the change"}]},
        )
        # The exposed schema has tasks/action at the top level and
        # goal/context/output_schema/images per task; no singular goal or role.
        for payload in (with_context["delegate_task"], without_context["delegate_task"]):
            self.assertLessEqual(set(payload), {"tasks", "action"})
            for item in payload["tasks"]:
                self.assertLessEqual(
                    set(item), {"goal", "context", "output_schema", "images"}
                )
                self.assertIn("goal", item)

    def test_invalid_requests_fail_before_spawn(self):
        def fail(**kwargs):
            raise AssertionError("invalid request must not reach the worker")

        # Each part fits its own limit; task+context together exceed the
        # prompt limit and must still be refused before spawn.
        oversized_task = "x" * 60_000
        oversized_context = "y" * 60_000
        cases = [
            ({"role": "implement", "task": "x", "cwd": str(self.cwd)}, "unknown_role"),
            ({"role": "research", "task": "x", "session_id": "ses_x"}, "invalid_session"),
            ({"role": "implementation", "task": "x"}, "invalid_cwd"),
            ({"role": "implementation", "task": "x", "cwd": "relative"}, "invalid_cwd"),
            (
                {
                    "role": "implementation",
                    "task": "x",
                    "cwd": str(self.cwd),
                    "timeout_seconds": 0,
                },
                "invalid_timeout",
            ),
            (
                {
                    "role": "implementation",
                    "task": "x",
                    "cwd": str(self.cwd),
                    "timeout_seconds": 3601,
                },
                "invalid_timeout",
            ),
            (
                {
                    "role": "implementation",
                    "task": "x",
                    "cwd": str(self.cwd),
                    "timeout_seconds": True,
                },
                "invalid_timeout",
            ),
            (
                {
                    "role": "implementation",
                    "task": oversized_task,
                    "context": oversized_context,
                    "cwd": str(self.cwd),
                },
                "invalid_task",
            ),
            (
                {
                    "role": "implementation",
                    "task": "x",
                    "cwd": str(self.cwd),
                    "model": "other/model",
                },
                "unknown_arguments",
            ),
            (
                {"role": "implementation", "task": "x", "cwd": str(self.cwd), "action": "run"},
                "unknown_action",
            ),
        ]
        with patch.object(self.module.opencode_worker, "run_worker", fail):
            for args, reason in cases:
                with self.subTest(args=args):
                    result = self.call(**args)
                    self.assertFalse(result["ok"], result)
                    self.assertFalse(result["executed"], result)
                    self.assertEqual(result["reason"], reason)

    def test_worker_failure_is_reported_without_stderr(self):
        with patch.object(
            self.module.opencode_worker,
            "run_worker",
            return_value={
                "ok": False,
                "exit_code": 1,
                "model": PINNED_MODEL,
                "cwd": str(self.cwd),
                "session_id": "ses_failed123",
                "timed_out": False,
                "text": "",
                "error": "output contains an error event",
                "stderr": "SECRET_TOKEN",
            },
        ):
            result = self.call(
                role="implementation",
                task="Implement the change",
                cwd=str(self.cwd),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("error event", result["error"])
        self.assertNotIn("SECRET_TOKEN", json.dumps(result))

    def test_duplicate_prompt_sections_are_absorbed(self):
        # Both loaders call register(); the host raises ValueError for an
        # already-registered section and the public helper must absorb it.
        module, ctx = load_plugin("_omh_worker_tool_duplicate_test", "coding")
        module.register(ctx)
        self.assertEqual(
            set(ctx.prompts), {"omh.awareness", "omh.worker-dispatch"}
        )
        guidance = ctx.prompts["omh.worker-dispatch"]["content"]
        self.assertIn("omh_worker", guidance({"profile_name": "coding"}))
        self.assertEqual(guidance({"profile_name": "default"}), "")

    def test_profile_scoping_refuses_non_coding(self):
        _module, default_ctx = load_plugin("_omh_worker_tool_default_test", "default")
        handler = default_ctx.tools["omh_worker"]["handler"]
        result = json.loads(
            handler(
                {
                    "role": "implementation",
                    "task": "Implement the change",
                    "cwd": str(self.cwd),
                }
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "profile_not_coding")
        self.assertFalse(result["executed"])

        guidance = self.ctx.prompts["omh.worker-dispatch"]["content"]
        self.assertIn("omh_worker", guidance({"profile_name": "coding"}))
        self.assertIn("does not intercept `delegate_task`", guidance({"profile_name": "coding"}))
        self.assertEqual(guidance({"profile_name": "default"}), "")

    def test_init_has_no_private_host_imports_or_registries(self):
        source = (OMH_DIR / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        targets: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                targets.append(node.module)
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str) and "import_module" in ast.unparse(node.func):
                    targets.append(node.args[0].value)
        self.assertEqual([name for name in targets if name.startswith("hermes_cli")], [])

        # Prompt-section idempotence must use the public registration attempt,
        # never the host's private plugin registry.
        forbidden = {"_plugin_context", "_manager", "_system_prompt_sections"}
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertEqual(attributes & forbidden, set())
        self.assertEqual(names & forbidden, set())
        for name in forbidden:
            self.assertNotIn(name, source, f"private host registry reference: {name}")


if __name__ == "__main__":
    unittest.main()
