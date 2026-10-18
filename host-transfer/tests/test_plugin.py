import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location("host_transfer", Path(__file__).parents[1] / "__init__.py")
ht = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ht)


def args(action, params=None):
    return {"action": action, "params": params or {}}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._root_patch = patch.object(ht, "LOCAL_ROOT", self.root)
        self._ssh_patch = patch.object(ht, "SSH_DIR", self.root / ".ssh")
        self._root_patch.start()
        self._ssh_patch.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._root_patch.stop)
        self.addCleanup(self._ssh_patch.stop)

    def _file(self, name="payload.bin", data=b"hello"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_unknown_action_and_parameters_rejected(self):
        with self.assertRaises(ValueError):
            ht.validate("upload", {})
        with self.assertRaises(ValueError):
            ht.validate("probe", {"host": "evil"})
        with self.assertRaises(ValueError):
            ht.validate("send", {"local_path": str(self._file()), "confirm": True, "port": 22})

    def test_send_requires_confirm_and_path(self):
        with self.assertRaises(ValueError):
            ht.validate("send", {"local_path": str(self._file())})
        with self.assertRaises(ValueError):
            ht.validate("send", {"confirm": True})
        with self.assertRaises(ValueError):
            ht.validate("send", {"local_path": str(self._file()), "confirm": "yes"})

    def test_target_allowlist(self):
        self.assertEqual(ht.validate("probe", {})["target"], "mac")
        with self.assertRaises(ValueError):
            ht.validate("probe", {"target": "laptop"})
        with self.assertRaises(ValueError):
            ht.validate("probe", {"target": []})

    def test_local_path_must_be_absolute_under_root_regular_file(self):
        path = self._file()
        self.assertEqual(ht.validate("send", {"local_path": str(path), "confirm": True})["local_path"], str(path))
        for bad in ("relative.bin", str(self.root / "missing.bin"), str(self.root)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ht.validate("send", {"local_path": bad, "confirm": True})

    def test_local_path_outside_root_and_ssh_material_rejected(self):
        with self.assertRaises(ValueError):
            ht.validate("send", {"local_path": "/etc/hostname", "confirm": True})
        ssh_file = self.root / ".ssh" / "id_ed25519"
        ssh_file.parent.mkdir()
        ssh_file.write_bytes(b"k")
        with self.assertRaises(ValueError):
            ht.validate("send", {"local_path": str(ssh_file), "confirm": True})

    def test_symlink_escape_rejected(self):
        link = self.root / "escape.bin"
        link.symlink_to("/etc/hostname")
        with self.assertRaises(ValueError):
            ht.validate("send", {"local_path": str(link), "confirm": True})


class CommandTests(unittest.TestCase):
    def test_argv_uses_fixed_binaries_and_explicit_options(self):
        argv = ht._send_argv("mac", "/home/shi/report.pdf")
        self.assertEqual(argv[0], ht.SCP)
        self.assertIn("-F", argv)
        self.assertIn(ht.NULL_CONFIG, argv)
        self.assertIn(ht.IDENTITY, argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertEqual(argv[-2], "/home/shi/report.pdf")
        self.assertEqual(argv[-1], "shin@macbook-pro:/Users/shin/Downloads/")

    def test_remote_sha_quotes_the_remote_path(self):
        argv = ht._remote_sha_argv("mac", "/Users/shin/Downloads/a b;rm -rf.txt")
        self.assertEqual(argv[0], ht.SSH)
        self.assertIn("shasum -a 256 '/Users/shin/Downloads/a b;rm -rf.txt'", argv[-1])


class HandleTests(unittest.TestCase):
    def test_probe_reports_reachability(self):
        with patch.object(ht, "_run", return_value=(0, "ok\n", "")) as run:
            payload = json.loads(ht.handle(args("probe")))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["reachable"])
        self.assertEqual(run.call_args.args[0][0], ht.SSH)

    def test_probe_failure_is_not_ok(self):
        with patch.object(ht, "_run", return_value=(255, "", "Permission denied")):
            payload = json.loads(ht.handle(args("probe")))
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["result"]["reachable"])

    def test_send_verifies_matching_checksum(self):
        digest = "a" * 64

        def fake(argv, timeout=ht.TIMEOUT):
            if argv[0] == ht.SCP:
                return 0, "", ""
            if argv[0] == ht.SHA256SUM:
                return 0, f"{digest}  {argv[1]}\n", ""
            return 0, f"{digest}  /Users/shin/Downloads/x.bin\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ht, "LOCAL_ROOT", Path(tmp)), patch.object(ht, "SSH_DIR", Path(tmp) / ".ssh"):
                target = Path(tmp) / "x.bin"
                target.write_bytes(b"data")
                with patch.object(ht, "_run", side_effect=fake):
                    payload = json.loads(ht.handle(args("send", {"local_path": str(target), "confirm": True})))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["verified"])
        self.assertEqual(payload["result"]["remote_path"], "/Users/shin/Downloads/x.bin")

    def test_send_checksum_mismatch_is_not_ok(self):
        def fake(argv, timeout=ht.TIMEOUT):
            if argv[0] == ht.SCP:
                return 0, "", ""
            if argv[0] == ht.SHA256SUM:
                return 0, f"{'a' * 64}  {argv[1]}\n", ""
            return 0, f"{'b' * 64}  /Users/shin/Downloads/x.bin\n", ""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ht, "LOCAL_ROOT", Path(tmp)), patch.object(ht, "SSH_DIR", Path(tmp) / ".ssh"):
                target = Path(tmp) / "x.bin"
                target.write_bytes(b"data")
                with patch.object(ht, "_run", side_effect=fake):
                    payload = json.loads(ht.handle(args("send", {"local_path": str(target), "confirm": True})))
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["result"]["transfer_succeeded"])
        self.assertFalse(payload["result"]["verified"])

    def test_handle_rejects_bad_payload_shape(self):
        payload = json.loads(ht.handle({"action": "probe"}))
        self.assertFalse(payload["ok"])
        self.assertIn("expected exactly", payload["error"])

    def test_redaction(self):
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz012345", ht._redact("token=ghp_abcdefghijklmnopqrstuvwxyz012345"))
        self.assertNotIn("hunter2", ht._redact("password=hunter2"))


if __name__ == "__main__":
    unittest.main()
