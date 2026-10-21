"""Runnable tests for the `omh_decision_gate` package-backend boundary.

Run from the repository root:

    python3 -m unittest discover -s omh -p 'test_decision_gate_tool.py' -v

The tests inject an in-memory `omh` backend, so neither a real provider nor a
gate store is touched.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


OMH_DIR = Path(__file__).resolve().parent
_PACKAGE_NAME = "_omh_decision_gate_under_test"
_VALID_ARGS = {
    "gate_id": "gate_1",
    "expected_resume_digest": "sha256:resume",
    "choice": "approve",
    "actor": "actor_1",
    "connector": "connector_1",
    "channel_ref": "channel_1",
    "thread_ref": "thread_1",
    "interaction_ref": "interaction_1",
    "authentication_method": "host_authenticated",
    "observed_at": "2026-10-21T00:00:00Z",
    "event_id": "event_1",
    "wrapper_session_ref": "session_1",
    "wrapper_expected_revision": 3,
}


def _package(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _backend_modules(validate, apply) -> dict[str, types.ModuleType]:
    omh = _package("omh")
    workflows = _package("omh.workflows")
    receipts = types.ModuleType("omh.workflows.decision_gate_receipts")
    receipts.validate_connector_decision_payload = validate
    paths = types.ModuleType("omh.paths")
    paths.resolve_paths = lambda **_kwargs: object()
    commands = _package("omh.commands")
    decision_gate = types.ModuleType("omh.commands.decision_gate")
    decision_gate.apply_connector_decision_answer = apply
    return {
        "omh": omh,
        "omh.workflows": workflows,
        "omh.workflows.decision_gate_receipts": receipts,
        "omh.paths": paths,
        "omh.commands": commands,
        "omh.commands.decision_gate": decision_gate,
    }


class DecisionGateToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            _PACKAGE_NAME,
            OMH_DIR / "__init__.py",
            submodule_search_locations=[str(OMH_DIR)],
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE_NAME] = package
        spec.loader.exec_module(package)
        cls.tool = importlib.import_module(f"{_PACKAGE_NAME}.tools.decision_gate_tool")

    def handler(self):
        return self.tool.omh_decision_gate_handler

    def test_valid_request_without_backend_returns_unavailable(self):
        modules = {"omh": _package("omh"), "omh.workflows": None}
        with patch.dict(sys.modules, modules):
            result = json.loads(self.handler()(dict(_VALID_ARGS)))

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "omh_decision_gate_core_unavailable")
        self.assertEqual(result["receipt"], {})
        self.assertIn("omh.workflows", result["detail"])

    def test_input_validation_precedes_the_backend_import(self):
        modules = {"omh": _package("omh"), "omh.workflows": None}
        invalid = {key: value for key, value in _VALID_ARGS.items() if key != "gate_id"}
        with patch.dict(sys.modules, modules):
            result = json.loads(self.handler()(invalid))

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["reason_code"], "invalid_payload")

    def test_available_backend_still_applies_the_validated_payload(self):
        calls: dict[str, object] = {}

        def validate(raw):
            calls.setdefault("validate", []).append(dict(raw))
            return dict(raw), ""

        def apply(_paths, **payload):
            calls["apply"] = payload
            return {
                "schema_version": "connector_decision_gate_result/v1",
                "status": "applied",
                "reason_code": "",
                "receipt": {"receipt_id": "receipt_1"},
            }

        with patch.dict(sys.modules, _backend_modules(validate, apply)), patch.object(
            self.tool, "default_omh_home", return_value=Path("/tmp/omh")
        ), patch.object(
            self.tool.runtime_paths, "default_hermes_home", return_value=Path("/tmp/hermes")
        ):
            result = json.loads(
                self.handler()(dict(_VALID_ARGS), trusted_connector_event=dict(_VALID_ARGS))
            )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(calls["apply"], _VALID_ARGS)
        self.assertEqual(calls["validate"], [_VALID_ARGS, _VALID_ARGS])

    def test_missing_command_backend_after_validation_returns_unavailable(self):
        modules = _backend_modules(lambda raw: (dict(raw), ""), lambda *_a, **_k: {})
        modules.pop("omh.commands.decision_gate")
        modules["omh.commands"] = None
        with patch.dict(sys.modules, modules):
            result = json.loads(
                self.handler()(dict(_VALID_ARGS), trusted_connector_event=dict(_VALID_ARGS))
            )

        self.assertEqual(result["status"], "unavailable")
        self.assertIn("omh.commands", result["detail"])

    def test_unrelated_import_failure_is_not_reported_as_missing_backend(self):
        def validate(_raw):
            raise ModuleNotFoundError("No module named 'cryptography'", name="cryptography")

        with patch.dict(sys.modules, _backend_modules(validate, lambda *_a, **_k: {})):
            with self.assertRaises(ModuleNotFoundError) as raised:
                self.handler()(dict(_VALID_ARGS))

        self.assertEqual(raised.exception.name, "cryptography")


if __name__ == "__main__":
    unittest.main()
