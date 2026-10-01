"""A suggested skill must be one THIS session can actually open.

Three times in two sessions the plugin named a skill that ``skill_view`` then refused:
``product-runtime-feature-audits``, ``delegated-work-followthrough`` and
``macos-third-party-software-installation``. All three exist on disk under the DEFAULT
profile's skills folder; none of them is in the running profile's catalog. An agent told to
load a procedure it cannot open spends a call to find that out, and a fleet member with a
smaller catalog can only be sent down that path. So the ranking is now checked against
Hermes's own loader before anything is said.

Offline: no network, no real home, no real Hermes. Every Jev reply is injected and every
path lives in a temporary directory.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Loaded under its own name so patches here cannot collide with the other test modules.
PLUGIN = REPO / "hermes" / "plugin" / "hermes-jev"
_spec = importlib.util.spec_from_file_location(
    "hermes_jev_skill_reachable", PLUGIN / "__init__.py",
    submodule_search_locations=[str(PLUGIN), str(REPO)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_jev_skill_reachable"] = plugin
_spec.loader.exec_module(plugin)

TURN = "the desktop update channel is serving a version that is 49 releases old, fix it"


def fake_skills_tool(payload):
    """A stand-in for tools.skills_tool whose skill_view returns ``payload``."""
    tool = types.ModuleType("tools.skills_tool")
    tool.skill_view = lambda name, **_: json.dumps(payload)
    package = types.ModuleType("tools")
    package.skills_tool = tool
    return {"tools": package, "tools.skills_tool": tool}


class SkillSuggestionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(plugin._TURNS.clear)
        state = self.home / "jev" / "state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"skills": "on"}), encoding="utf-8")

    def suggest_with(self, picked_name, modules):
        """Run the hook with one canned pick and the given tools modules in place."""
        pick = {"status": "ok", "skills": [{"name": picked_name, "match": 0.71}]}
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(plugin, "_hermes_skill_roots", lambda: []), \
                mock.patch.object(plugin.skillpick, "discover", lambda roots, disabled=(): []), \
                mock.patch.object(plugin.skillpick, "pick", return_value=pick):
            return plugin._on_pre_llm_call(session_id="s", turn_id="t1", user_message=TURN)

    def log(self):
        path = self.home / "logs" / "jev-decisions.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class SkillFailureDiagnosticsTests(SkillSuggestionCase):
    def test_fail_open_logs_only_a_bounded_reason_code(self):
        for reason, expected in [("Jev unavailable (timeout)", "timeout"),
                                 ("turn looks sensitive; not sent", "sensitive_turn"),
                                 ("no skills", "no_skills"),
                                 ("stage 1 incomplete (batches [1])", "stage_one_incomplete"),
                                 ("private text must never appear", "other")]:
            with mock.patch.object(plugin, "_hermes_skill_roots", lambda: []), \
                    mock.patch.object(plugin.skillpick, "discover", return_value=[]), \
                    mock.patch.object(plugin.skillpick, "pick", return_value={
                        "status": "fail_open", "reason": reason, "skills": []}):
                self.assertIsNone(plugin._on_pre_llm_call(session_id="failure", user_message="hello"))
            entry = [item for item in self.log() if item.get("kind") == "skill"][-1]
            self.assertEqual(entry.get("reason_code"), expected)
            self.assertNotIn("reason", entry)
            self.assertNotIn("private text", json.dumps(entry))


class UnreachableSkillTests(SkillSuggestionCase):
    def test_a_pick_hermes_refuses_is_never_suggested(self):
        """The whole point: the agent is not sent to a skill_view that fails."""
        modules = fake_skills_tool({"success": False,
                                    "error": "Skill 'macos-third-party-software-installation' not found"})
        self.assertIsNone(self.suggest_with("macos-third-party-software-installation", modules))

    def test_the_dropped_pick_is_diagnosable_in_the_decision_log(self):
        modules = fake_skills_tool({"success": False, "error": "not found"})
        self.suggest_with("product-runtime-feature-audits", modules)
        entry = [e for e in self.log() if e.get("kind") == "skill_unreachable"]
        self.assertEqual([e["candidate"] for e in entry], ["product-runtime-feature-audits"])

    def test_a_loader_that_raises_says_nothing_rather_than_guessing(self):
        tool = types.ModuleType("tools.skills_tool")

        def boom(name, **_):
            raise RuntimeError("loader exploded")
        tool.skill_view = boom
        package = types.ModuleType("tools")
        package.skills_tool = tool
        self.assertIsNone(self.suggest_with("hermes-kanban", {"tools": package, "tools.skills_tool": tool}))

    def test_a_hermes_without_the_loader_says_nothing(self):
        """An older Hermes cannot be asked, so there is no verified name to offer."""
        with mock.patch.dict(sys.modules, {"tools": None, "tools.skills_tool": None}):
            self.assertIsNone(self.suggest_with("hermes-kanban", {}))


class ReachableSkillTests(SkillSuggestionCase):
    def test_a_pick_hermes_can_open_is_suggested(self):
        modules = fake_skills_tool({"success": True, "name": "hermes-kanban",
                                    "path": "devops/hermes-kanban/SKILL.md"})
        answer = self.suggest_with("hermes-kanban", modules)
        self.assertIn("`hermes-kanban`", answer["context"])
        self.assertIn("Load it with skill_view", answer["context"])

    def test_the_name_hermes_resolves_is_the_one_offered(self):
        """A ranked path is not necessarily the name the loader answers to."""
        modules = fake_skills_tool({"success": True, "name": "coagent-feature-intake"})
        answer = self.suggest_with("product-development/coagent-feature-intake", modules)
        self.assertIn("`coagent-feature-intake`", answer["context"])


class RootScopingTests(SkillSuggestionCase):
    def test_hermes_own_roots_are_ranked_when_it_can_name_them(self):
        """HERMES_HOME can be the default home while another profile is the running one,
        which is how the default profile's catalog got ranked at all."""
        profile_skills = self.home / "profiles" / "coagent" / "skills"
        shared = Path("/fleet/shared-skills")
        seen = []

        def discover(roots, disabled=()):
            seen.extend(Path(root) for root in roots)
            return []
        with mock.patch.object(plugin, "_hermes_skill_roots", lambda: [profile_skills, shared]), \
                mock.patch.object(plugin.skillpick, "discover", discover), \
                mock.patch.object(plugin.skillpick, "pick", return_value={"status": "ok", "skills": []}):
            plugin._on_pre_llm_call(session_id="s", turn_id="t1", user_message=TURN)
        self.assertEqual(seen, [profile_skills, shared])

    def test_without_hermes_the_home_root_is_still_scanned(self):
        """The old behaviour is the fallback, not the first answer."""
        seen = []
        with mock.patch.object(plugin, "_hermes_skill_roots", lambda: []), \
                mock.patch.object(plugin.skillpick, "discover",
                                  lambda roots, disabled=(): seen.extend(roots) or []), \
                mock.patch.object(plugin.skillpick, "pick", return_value={"status": "ok", "skills": []}):
            plugin._on_pre_llm_call(session_id="s", turn_id="t1", user_message=TURN)
        self.assertIn(self.home / "skills", [Path(p) for p in seen])


if __name__ == "__main__":
    unittest.main()
