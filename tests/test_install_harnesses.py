"""Claude Code, Codex, Gemini CLI and OpenCode: the fewest skill folders, and lanes where defined."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_install
from test_install import install

KEEP_OUT = ("HERMES_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CODEX_HOME", "JEV_HOME", "JEV_LANE_HOST",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
            "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
GEMINI_LANES = {lane: {"model": f"gemini-{lane}", "effort": "medium"} for lane in ("small", "medium", "high", "escalate")}


def run(argv, home, **env_extra):
    env = {k: v for k, v in os.environ.items() if k not in KEEP_OUT}
    env.update(HOME=str(home), PATH="/usr/bin:/bin", **env_extra)
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["install.py", *argv]), \
            mock.patch.object(install.shutil, "which", return_value=None), contextlib.redirect_stdout(out):
        code = install.main()
    return code, json.loads(out.getvalue())


class Harnesses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def make(self, *names):
        folders = {"claude": ".claude", "codex": ".codex", "gemini": ".gemini", "opencode": ".config/opencode",
                   "agents": ".agents"}
        for name in names:
            (self.home / folders[name]).mkdir(parents=True, exist_ok=True)

    def skills_in(self, folder):
        return sorted(p.name for p in (self.home / folder).iterdir()) if (self.home / folder).is_dir() else []

    def test_every_harness_is_reached_through_two_folders(self):
        self.make("claude", "codex", "gemini", "opencode")
        code, report = run([], self.home)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(f["folder"] for f in report["skill_folders"]),
                         sorted([str(self.home / ".claude" / "skills"), str(self.home / ".agents" / "skills")]))
        via = {name: entry["skills_via"] for name, entry in report["harnesses"].items()}
        self.assertEqual(via["opencode"], str(self.home / ".claude" / "skills"))
        self.assertEqual(via["codex"], str(self.home / ".agents" / "skills"))
        self.assertEqual(via["gemini-cli"], str(self.home / ".agents" / "skills"))
        self.assertEqual(self.skills_in(".claude/skills"), install.SKILLS)
        self.assertEqual(self.skills_in(".config/opencode/skills"), [])
        self.assertEqual(self.skills_in(".gemini/skills"), [])
        self.assertEqual(self.skills_in(".codex/skills"), [])

    def test_opencode_alone_gets_its_own_folder(self):
        self.make("opencode")
        _, report = run([], self.home)
        self.assertEqual([f["folder"] for f in report["skill_folders"]], [str(self.home / ".config/opencode/skills")])

    def test_opencode_without_claude_skills_is_not_counted_as_reached_through_them(self):
        self.make("claude", "opencode")
        _, report = run([], self.home, OPENCODE_DISABLE_CLAUDE_CODE_SKILLS="1")
        self.assertEqual(report["harnesses"]["opencode"]["skills_via"], str(self.home / ".config/opencode/skills"))

    def test_an_old_codex_folder_is_refreshed_only_if_it_holds_ours(self):
        self.make("codex")
        _, report = run([], self.home)
        self.assertNotIn(str(self.home / ".codex/skills"), [f["folder"] for f in report["skill_folders"]])
        (self.home / ".codex/skills/jev-setup").mkdir(parents=True)
        (self.home / ".codex/skills/jev-setup/SKILL.md").write_text("old")
        _, report = run([], self.home)
        self.assertIn(str(self.home / ".codex/skills"), [f["folder"] for f in report["skill_folders"]])

    def test_no_lanes_until_lanes_json_defines_the_harness(self):
        self.make("gemini", "codex")
        _, report = run([], self.home)
        self.assertIn("lanes.json", report["harnesses"]["gemini-cli"]["lanes"])
        self.assertIn("not supported", report["harnesses"]["codex"]["lanes"])
        self.assertFalse((self.home / ".gemini/agents").exists())
        self.assertFalse((self.home / ".gemini/GEMINI.md").exists())

    def write_lanes(self, data):
        folder = self.home / ".config" / "jev"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "lanes.json").write_text(json.dumps(data))

    def test_gemini_lanes_from_lanes_json_and_uninstall(self):
        self.make("gemini")
        (self.home / ".gemini/GEMINI.md").write_text("# mine\n")
        self.write_lanes({"gemini-cli": GEMINI_LANES})
        _, report = run([], self.home)
        entry = report["harnesses"]["gemini-cli"]
        self.assertEqual(entry["lane_models"]["small"], "gemini-small")
        head = (self.home / ".gemini/agents/jev-lane-small.md").read_text().split("---")[1]
        self.assertIn("name: jev-lane-small", head)
        self.assertIn("model: gemini-small", head)
        self.assertNotIn("effort:", head)
        block = (self.home / ".gemini/GEMINI.md").read_text()
        self.assertTrue(block.startswith("# mine\n"))
        self.assertIn("jev lane classify --host gemini-cli --task", block)
        self.assertIn("`jev-lane-small` (gemini-small, medium)", block)
        _, report = run(["--uninstall"], self.home)
        self.assertEqual(list((self.home / ".gemini/agents").iterdir()), [])
        self.assertEqual((self.home / ".gemini/GEMINI.md").read_text(), "# mine\n")
        self.assertEqual(self.skills_in(".agents/skills"), [])

    def test_opencode_agents_are_subagents_and_its_claude_md_fallback_is_kept(self):
        self.make("opencode", "claude")
        (self.home / ".claude/CLAUDE.md").write_text("# shared rules\n")
        self.write_lanes({"opencode": {lane: {"model": f"lais/{lane}", "variant": "high"}
                                       for lane in ("small", "medium", "high", "escalate")}})
        _, report = run([], self.home)
        entry = report["harnesses"]["opencode"]
        head = (self.home / ".config/opencode/agents/jev-lane-high.md").read_text().split("---")[1]
        self.assertIn("mode: subagent", head)
        self.assertIn("model: lais/high", head)
        self.assertIn("variant: high", head)
        self.assertNotIn("name:", head)
        self.assertEqual(entry["instructions"]["change"], "skipped")
        self.assertFalse((self.home / ".config/opencode/AGENTS.md").exists())

    def test_an_agent_file_that_is_not_ours_is_left_alone(self):
        self.make("gemini")
        self.write_lanes({"gemini-cli": GEMINI_LANES})
        (self.home / ".gemini/agents").mkdir()
        (self.home / ".gemini/agents/jev-lane-small.md").write_text("---\nname: jev-lane-small\n---\nmine")
        _, report = run([], self.home)
        self.assertIn(str(self.home / ".gemini/agents/jev-lane-small.md"),
                      report["harnesses"]["gemini-cli"]["agents_left_alone"])
        self.assertEqual((self.home / ".gemini/agents/jev-lane-small.md").read_text(), "---\nname: jev-lane-small\n---\nmine")

    def test_check_writes_nothing(self):
        self.make("claude", "gemini", "opencode")
        self.write_lanes({"gemini-cli": GEMINI_LANES})
        run(["--check"], self.home)
        for folder in (".claude/skills", ".agents/skills", ".gemini/agents", ".claude/agents"):
            self.assertFalse((self.home / folder).exists(), folder)


if __name__ == "__main__":
    unittest.main()
