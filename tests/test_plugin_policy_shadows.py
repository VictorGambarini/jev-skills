"""Kanban and shell-command policy shadows: off by default, log-only, never the text.

* No kanban hook registers and no shell command is sampled until a feature is set to shadow.
* post_tool_call samples shell commands at `gate_all_sample` and returns at once.
* Kanban hooks queue the work; a hook never raises, whatever the board does.
* The log holds ids, hashes, actions and probabilities, never the command or card text.
"""
from __future__ import annotations

import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import TempHome  # noqa: E402
from test_plugin_policy_features import FakeContext, load_plugin  # noqa: E402

SECRETISH = "rm -rf /srv/private-project-name"


def fake_kanban(task=None, runs=()):
    """A stand-in for hermes_cli.kanban_db: only the calls the plugin makes."""
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    kb = types.SimpleNamespace(connect_closing=lambda board=None: Conn(),
                               get_task=lambda conn, task_id: task, list_runs=lambda conn, task_id: list(runs))
    package = types.ModuleType("hermes_cli")
    package.kanban_db = kb
    return {"hermes_cli": package, "hermes_cli.kanban_db": kb}


def run(i, outcome="crashed", error="pid 1 not alive", summary="", ended=1):
    return types.SimpleNamespace(id=i, outcome=outcome, error=error, summary=summary, ended_at=ended)


class Defaults(TempHome):
    def test_no_new_hook_registers_by_default(self):
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        for hook in ("on_kanban_worker_exited", "kanban_task_blocked", "kanban_task_completed"):
            self.assertNotIn(hook, ctx.hooks)

    def test_shadow_registers_and_the_kill_switch_wins(self):
        plugin = load_plugin()
        plugin.switches.set_mode("retry", "shadow", shared=True)
        plugin.switches.set_mode("kanban_done", "shadow", shared=True)
        plugin.switches.kill_switch("kanban_done").touch()
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertIn("on_kanban_worker_exited", ctx.hooks)
        self.assertNotIn("kanban_task_completed", ctx.hooks)

    def test_shell_commands_are_not_sampled_when_off(self):
        plugin = load_plugin()
        with mock.patch.object(plugin.shadowq, "submit") as submit:
            plugin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s")
        submit.assert_not_called()


class Shadows(TempHome):
    def setUp(self):
        super().setUp()
        self.plugin = load_plugin()

    def rows(self):
        path = self.home / "logs" / "jev-shadow.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_gate_all_samples_and_logs_no_command(self):
        self.plugin.switches.set_mode("gate_all", "shadow", shared=True)
        seen = []

        def check(tool, **kwargs):
            seen.append(kwargs)
            return {"action": "ask_human", "policy": "gate-ask@1#t", "status": "ok", "command_sha256": "h",
                    "answers": {"destroys_data": {"kind": "noul", "p": 0.9}}}

        with mock.patch.object(self.plugin.gate, "check", side_effect=check), \
                mock.patch.object(self.plugin.random, "random", return_value=0.0):
            started = time.perf_counter()
            self.plugin._on_post_tool_call(tool_name="terminal", args={"command": SECRETISH}, session_id="s")
            self.assertLess((time.perf_counter() - started) * 1000, 20)
            self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        self.assertEqual(seen[0]["policy"], "gate-ask")
        self.assertEqual(seen[0]["mode"], "shadow")
        rows = self.rows()
        self.assertEqual(rows[0]["feature"], "gate_all")
        self.assertEqual(rows[0]["action"], "ask_human")
        self.assertNotIn("private-project-name", json.dumps(rows))

    def test_gate_all_sample_rate_zero_sends_nothing(self):
        self.plugin.switches.set_mode("gate_all", "shadow", shared=True)
        path = self.plugin.switches.jev_dir(True) / "state.json"
        state = json.loads(path.read_text())
        state["gate_all_sample"] = 0
        path.write_text(json.dumps(state))
        with mock.patch.object(self.plugin.shadowq, "submit") as submit:
            for _ in range(50):
                self.plugin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s")
        submit.assert_not_called()

    def test_retry_shadow_builds_facts_in_code_and_logs_ids(self):
        self.plugin.switches.set_mode("retry", "shadow", shared=True)
        task = types.SimpleNamespace(title="Private card title", body="private body")
        runs = [run(1, error="Iteration budget exhausted (40/40)"), run(2, error="Iteration budget exhausted (40/40)")]
        seen = []

        def decide(state, policy, **kwargs):
            seen.append((state, policy, kwargs))
            return {"action": "retry", "policy": "retry@1#t", "status": "ok", "source": "jev", "answers": {}}

        with mock.patch.dict(sys.modules, fake_kanban(task, runs)), \
                mock.patch.object(self.plugin.policy_engine, "decide", side_effect=decide):
            hook = self.plugin._kanban_hook("retry", self.plugin._retry_job)
            self.assertIsNone(hook(task_id="t_0000abcd", run_id=2, outcome="timed_out", exit_kind="nonzero_exit"))
            self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        state, policy, kwargs = seen[0]
        self.assertEqual(policy, "retry")
        self.assertTrue(kwargs["facts"]["budget_exhausted"])
        self.assertTrue(kwargs["facts"]["same_error_as_last_attempt"])
        self.assertEqual(state["attempt_number"], 2)
        row = self.rows()[0]
        self.assertEqual((row["feature"], row["task_id"], row["run_id"]), ("retry", "t_0000abcd", 2))
        self.assertNotIn("Private card", json.dumps(row))

    def test_a_broken_board_is_logged_as_an_error_not_raised(self):
        self.plugin.switches.set_mode("blockcheck", "shadow", shared=True)
        with mock.patch.dict(sys.modules, {"hermes_cli": None}):
            hook = self.plugin._kanban_hook("blockcheck", self.plugin._blockcheck_job)
            self.assertIsNone(hook(task_id="t_0000abcd", reason="needs Steve"))
            self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        self.assertEqual(self.rows()[0]["status"], "error")

    def test_owner_shadow_needs_an_option_file(self):
        self.plugin.switches.set_mode("owner", "shadow", shared=True)
        with mock.patch.object(self.plugin.policy_engine, "decide") as decide:
            self.plugin._on_post_tool_call(tool_name="kanban_create", args={"title": "x"}, session_id="s")
            self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        decide.assert_not_called()



class Exclusions(TempHome):
    def test_an_excluded_profile_sends_nothing(self):
        plugin = load_plugin()
        plugin.switches.set_mode("gate_all", "shadow", shared=True)
        path = plugin.switches.jev_dir(True) / "state.json"
        state = json.loads(path.read_text())
        state.update(gate_all_sample=1, shadow_exclude_profiles=["customer"])
        path.write_text(json.dumps(state))
        with mock.patch.object(plugin, "_profile", return_value="customer"), \
                mock.patch.object(plugin, "_private_profile", return_value=False), \
                mock.patch.object(plugin.shadowq, "submit") as submit:
            plugin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s")
            plugin._kanban_hook("retry", plugin._retry_job)(task_id="t_0000abcd")
        submit.assert_not_called()
        with mock.patch.object(plugin, "_profile", return_value="devbot"), \
                mock.patch.object(plugin, "_private_profile", return_value=False), \
                mock.patch.object(plugin.shadowq, "submit") as submit:
            plugin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s")
        submit.assert_called_once()

    def test_a_private_profile_sends_nothing(self):
        plugin = load_plugin()
        plugin.switches.set_mode("gate_all", "shadow", shared=True)
        with mock.patch.object(plugin, "_private_profile", return_value=True), \
                mock.patch.object(plugin.random, "random", return_value=0.0), \
                mock.patch.object(plugin.shadowq, "submit") as submit:
            plugin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s")
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
