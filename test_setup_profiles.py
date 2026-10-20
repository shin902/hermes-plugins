import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import setup_profiles as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo, self.home = root / 'repo', root / 'home'
        self.src = self.repo / 'demo'
        self.src.mkdir(parents=True)
        (self.src / 'plugin.yaml').write_text('name: demo\nversion: 1.2.3\n')
        (self.home / 'plugins').mkdir(parents=True)
        self.target = self.home / 'plugins' / 'demo'
        self.entry = dict(profile='default', plugin='demo', source='demo', version='1.2.3')
        self.mapping = dict(schema_version=2, mappings=[self.entry])

    def run_setup(self, apply=False):
        return setup.reconcile(self.repo, self.home, self.mapping, apply)

    def test_dry_run_apply_idempotent(self):
        self.assertEqual(self.run_setup()['would_create'], 1)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.run_setup(True)['created'], 1)
        self.assertFalse(Path(self.target.readlink()).is_absolute())
        self.assertEqual(self.target.resolve(), self.src)
        self.assertEqual(self.run_setup(True)['created'], 0)

    def test_real_directory_refused(self):
        self.target.mkdir()
        with self.assertRaises(ValueError): self.run_setup(True)

    def test_divergent_link_refused(self):
        self.target.symlink_to('/nonexistent')
        with self.assertRaises(ValueError): self.run_setup(True)

    def test_duplicate_refused_before_writes(self):
        self.mapping['mappings'].append(self.entry.copy())
        with self.assertRaises(ValueError): self.run_setup(True)
        self.assertFalse(self.target.exists())

    def test_version_refused(self):
        self.entry['version'] = '2.0.0'
        with self.assertRaises(ValueError): self.run_setup()

    def test_manual_refused(self):
        self.entry['plugin'] = 'hindsight'
        with self.assertRaises(ValueError): self.run_setup(True)

    def test_unsafe_identifiers(self):
        for bad in ['../escape', '/absolute', '.deleted', '.', 'a/.backup', 'a/..']:
            with self.subTest(value=bad), self.assertRaises(ValueError): setup.safe_parts(bad)

    def test_no_enumeration_or_config_reads(self):
        (self.home / 'config.yaml').write_text('sentinel')
        with patch.object(Path, 'iterdir', side_effect=AssertionError('enumeration forbidden')):
            self.run_setup(True)
        self.assertEqual((self.home / 'config.yaml').read_text(), 'sentinel')

    def test_profile_specific_state(self):
        self.entry['profile'] = 'example'
        (self.home / 'profiles/example/plugins').mkdir(parents=True)
        (self.src / 'feeds.json').write_text('{"private":true}')
        self.run_setup(True)
        target = self.home / 'profiles/example/plugins/demo'
        self.assertEqual((target / 'feeds.json').read_bytes(), (self.src / 'feeds.json').read_bytes())

    def test_nested_provider(self):
        self.entry['plugin'] = 'model-providers/demo'
        self.entry['source'] = 'model-providers/demo'
        destination = self.repo / self.entry['source']
        destination.parent.mkdir()
        self.src.rename(destination)
        self.src = destination
        (self.home / 'plugins/model-providers').mkdir()
        self.assertEqual(self.run_setup(True)['created'], 1)

    def test_profile_specific_source_refused(self):
        self.entry['source'] = 'profiles/example/demo'
        with self.assertRaises(ValueError): self.run_setup(True)
        self.assertFalse(self.target.exists())

    def test_profiles_share_one_source(self):
        self.mapping['mappings'].append(dict(self.entry, profile='example'))
        target = self.home / 'profiles/example/plugins/demo'
        target.parent.mkdir(parents=True)
        self.assertEqual(self.run_setup(True)['created'], 2)
        self.assertTrue(self.target.samefile(target))
        self.assertEqual(self.run_setup()['unchanged'], 2)

    def test_rollback(self):
        src2 = self.repo / 'second'; src2.mkdir()
        (src2 / 'plugin.yaml').write_text('version: 1.2.3\n')
        self.mapping['mappings'].append(dict(self.entry, plugin='second', source='second'))
        original = Path.symlink_to
        calls = []
        def fail_second(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2: raise OSError('simulated failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'symlink_to', fail_second), self.assertRaises(OSError):
            self.run_setup(True)
        self.assertFalse(self.target.is_symlink())


if __name__ == '__main__':
    unittest.main()
