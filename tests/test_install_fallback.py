"""Offline reproduction of Windows symlink privilege denial."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("fallback_install", Path(__file__).resolve().parents[1] / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class CommandFallbackTests(unittest.TestCase):
    def test_denied_symlink_creates_a_working_owned_launcher(self):
        with tempfile.TemporaryDirectory(prefix="jev fallback ") as tmp:
            link = Path(tmp) / "bin" / "jev"
            with mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "symlink privilege denied")):
                reason = install._link_command(link, check=False)
            self.assertIsNone(reason)
            self.assertTrue(link.is_file())
            self.assertFalse(link.is_symlink())
            self.assertTrue(install._is_ours(link))
            # Exercise the actual launcher from outside the checkout with a relative file.
            source = Path(tmp) / "question.json"
            source.write_text('{"question":"hello"}', encoding="utf-8")
            env = dict(os.environ, HOME=tmp, HERMES_HOME=str(Path(tmp) / "hermes"))
            result = subprocess.run(["sh", str(link), "--help"], cwd=tmp, env=env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("jev", result.stdout.lower())


    def test_owned_launcher_is_idempotent_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                link = Path(tmp) / ".local" / "bin" / "jev"
                with mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "denied")):
                    self.assertIsNone(install._link_command(link, False))
                before = link.stat().st_mtime_ns
                self.assertIsNone(install._link_command(link, False))
                self.assertIsNone(install._link_command(link, True))
                self.assertEqual(link.stat().st_mtime_ns, before)
                self.assertEqual(install.uninstall_cli()["removed"], [str(link)])
                self.assertFalse(link.exists())

    def test_foreign_and_modified_launchers_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "jev"
            for text in ("#!/bin/sh\necho user-owned\n", install._command_wrapper() + "# modified\n"):
                link.write_text(text, encoding="utf-8")
                self.assertFalse(install._is_ours(link))
                self.assertIn("did not put there", install._link_command(link, False))
                self.assertEqual(link.read_text(encoding="utf-8"), text)

    def test_check_does_not_create_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "new" / "bin" / "jev"
            self.assertIsNone(install._link_command(link, True))
            self.assertFalse(link.parent.exists())

    def test_unwritable_fallback_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "jev"
            with mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "denied")), \
                    mock.patch.object(Path, "open", side_effect=OSError(13, "not writable")):
                self.assertIn("not writable", install._link_command(link, False))
            self.assertFalse(link.exists())

    def test_quoted_checkout_launcher_preserves_arguments_and_cwd(self):
        with tempfile.TemporaryDirectory(prefix="jev's checkout ") as tmp:
            source = Path(tmp) / "source command"
            source.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n', encoding="utf-8")
            source.chmod(0o755)
            link = Path(tmp) / "lane" / "jev"
            with mock.patch.object(install, "JEV", source), \
                    mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "denied")):
                self.assertIsNone(install._link_command(link, False))
                result = subprocess.run(["sh", str(link), "relative/path", "two words", "$(false)"],
                                        cwd=tmp, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[1:], ["relative/path", "two words", "$(false)"])
            if os.name != "nt":
                self.assertEqual(Path(result.stdout.splitlines()[0]).resolve(), Path(tmp).resolve())

    def test_profile_install_also_works_without_symlink_privileges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            profile = root / "profiles" / "alpha"
            profile.mkdir(parents=True)
            config = "plugins:\n  enabled: []\n"
            (root / "config.yaml").write_text(config, encoding="utf-8")
            (profile / "config.yaml").write_text(config, encoding="utf-8")
            with mock.patch.object(Path, "symlink_to", side_effect=OSError(1314, "denied")):
                report = install.install_hermes(root, check=False, enable="all")
            self.assertNotIn("not_installed_in", report)
            self.assertTrue((profile / "plugins" / "hermes-jev" / "plugin.yaml").is_file())
            self.assertTrue((profile / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
