"""One answer to "where does Jev keep its state", with and without Hermes."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jevkit import ladder, lane_shadow, ledger, limits, paths, policy, switches


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)

    def env(self, **extra):
        base = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "cfg"),
                "XDG_STATE_HOME": str(self.home / "state")}
        base.update(extra)
        patcher = mock.patch.dict(os.environ, base)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HERMES_HOME", "JEV_HOME", "JEV_LEDGER_PATH", "JEV_LADDER_STATE"):
            if name not in extra:
                os.environ.pop(name, None)

    def test_without_hermes_everything_is_xdg_and_hermes_is_never_named(self):
        self.env()
        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertFalse(paths.uses_hermes())
            cfg, state = self.home / "cfg" / "jev", self.home / "state" / "jev"
            self.assertEqual(switches.jev_dir(True), cfg)
            self.assertEqual(switches.jev_dir(False), cfg)
            self.assertEqual(policy.override_dir(), cfg / "policies")
            self.assertEqual(limits.root(), cfg)
            self.assertEqual(ledger.path(), state / "logs" / "jev-ledger.jsonl")
            self.assertEqual(lane_shadow.log_path(), state / "logs" / "jev-lanes.jsonl")
            self.assertEqual(ladder.state_path(), state / "ladder.json")
            self.assertEqual(ledger.profile(), "default")
            for path in (switches.jev_dir(True), ledger.path(), ladder.state_path()):
                self.assertNotIn(".hermes", str(path))

    def test_hermes_profile_keeps_the_hermes_layout(self):
        root = self.home / "h"
        self.env(HERMES_HOME=str(root / "profiles" / "coder"))
        self.assertTrue(paths.uses_hermes())
        self.assertEqual(paths.profile(), "coder")
        self.assertEqual(switches.jev_dir(True), root / "jev")
        self.assertEqual(switches.jev_dir(False), root / "profiles" / "coder" / "jev")
        self.assertEqual(policy.override_dir(), root / "jev" / "policies")
        self.assertEqual(ledger.path(), root / "profiles" / "coder" / "logs" / "jev-ledger.jsonl")
        self.assertEqual(lane_shadow.log_path(), root / "logs" / "jev-lanes.jsonl")

    def test_an_installed_hermes_is_found_without_hermes_home(self):
        self.env()
        (self.home / ".hermes").mkdir()
        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertTrue(paths.uses_hermes())
            self.assertEqual(switches.jev_dir(True), self.home / ".hermes" / "jev")

    def test_jev_home_wins_over_hermes(self):
        jev = self.home / "jev-home"
        self.env(JEV_HOME=str(jev), HERMES_HOME=str(self.home / "h"))
        self.assertFalse(paths.uses_hermes())
        self.assertEqual(switches.jev_dir(True), jev)
        self.assertEqual(ledger.path(), jev / "logs" / "jev-ledger.jsonl")
        self.assertEqual(paths.profile(), "default")


if __name__ == "__main__":
    unittest.main()
