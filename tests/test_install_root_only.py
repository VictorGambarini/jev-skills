"""A refresh must be possible without writing unrelated agents or lane links."""
import tempfile
import unittest
from pathlib import Path

from test_install import CONFIG, run_installer


class RootOnlyTests(unittest.TestCase):
    def test_refresh_leaves_profiles_and_other_agents_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / '.hermes'
            lane = root / 'profiles' / 'other'
            lane.mkdir(parents=True)
            (root / 'config.yaml').write_text(CONFIG, encoding='utf-8')
            (lane / 'config.yaml').write_text(CONFIG, encoding='utf-8')
            for name in ('.claude', '.codex', '.agents'):
                (home / name).mkdir()
                (home / name / 'sentinel').write_text('keep', encoding='utf-8')
            code, report = run_installer(['--hermes-root-only'], home)
            self.assertEqual(code, 0)
            self.assertEqual(report['hermes']['profiles'], 0)
            self.assertEqual(report['hermes']['enabled_in'], {})
            self.assertTrue((root / 'plugins' / 'hermes-jev' / 'jevkit').is_dir())
            self.assertTrue((root / 'bin' / 'jev').is_symlink())
            self.assertEqual(list(lane.iterdir()), [lane / 'config.yaml'])
            self.assertEqual((root / 'config.yaml').read_text(encoding='utf-8'), CONFIG)
            self.assertEqual((lane / 'config.yaml').read_text(encoding='utf-8'), CONFIG)
            for name in ('.claude', '.codex', '.agents'):
                self.assertEqual(list((home / name).iterdir()), [home / name / 'sentinel'])
            self.assertFalse((home / '.local').exists())

    def test_preview_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / '.hermes'
            root.mkdir()
            (root / 'config.yaml').write_text(CONFIG, encoding='utf-8')
            code, report = run_installer(['--hermes-root-only', '--check'], home)
            self.assertEqual(code, 0)
            self.assertEqual(list(root.iterdir()), [root / 'config.yaml'])
            self.assertEqual(report['skill_folders'], [])
            self.assertEqual(report['hermes']['would_enable_in'], [])

    def test_uninstall_cannot_expand_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                run_installer(['--hermes-root-only', '--uninstall'], Path(tmp))
