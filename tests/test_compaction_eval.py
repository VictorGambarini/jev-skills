"""The compaction eval's offline invariants. No network: every model call is out of scope here.

What these pin is the fairness of the comparison. An eval that quietly hands one arm a
bigger budget, or that lets session content land inside a public repo, would be worse than
having no eval, because its numbers would be believed.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("compaction_eval", ROOT / "evals" / "compaction" / "run_eval.py")
ev = importlib.util.module_from_spec(_spec)
sys.modules["compaction_eval"] = ev
_spec.loader.exec_module(ev)


def turns(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "word " * 30} for i in range(n)]


class LoadTests(unittest.TestCase):
    def write(self, text, suffix):
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_a_hermes_jsonl_export_and_a_bare_list_load_the_same_rows(self):
        messages = [{"role": "user", "content": "hi"}, {"role": "tool", "content": "out"},
                    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                    {"role": "system", "content": "ignored"}]
        bare = ev.load(self.write(json.dumps(messages), ".json"))
        wrapped = ev.load(self.write(json.dumps({"messages": messages}), ".json"))
        export = ev.load(self.write(json.dumps({"id": "s1", "messages": messages}) + "\n", ".jsonl"))
        self.assertEqual(bare, wrapped)
        self.assertEqual(bare, export)
        self.assertEqual(bare[2]["said"], "done")
        self.assertEqual([r["role"] for r in bare], ["user", "tool", "assistant"])

    def test_tool_call_arguments_are_visible_to_the_exam(self):
        rows = ev.load(self.write(json.dumps([{"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "terminal", "arguments": '{"command": "systemctl restart render"}'}}]}]), ".json"))
        self.assertIn("systemctl restart render", rows[0]["content"])
        self.assertEqual(ev.dialogue(rows), [])        # the shipped export would not have seen it

    def test_every_arm_sees_the_dialogue_the_shipped_plugin_exports(self):
        rows = [{"role": "user", "content": "a"}, {"role": "tool", "content": "b"}, {"role": "assistant", "content": "c"}]
        self.assertEqual([t["role"] for t in ev.dialogue(rows)], ["user", "assistant"])


class ArmTests(unittest.TestCase):
    def selection(self, n, keep, drop):
        fates = {str(i): "keep" for i in range(n)}
        for i in range(0, keep * 2, 2):                # Jev keeps scattered EARLY turns
            fates[str(i)] = "keep"
        for i in range(n - 1, n - 1 - drop, -1):
            fates[str(i)] = "drop"
        counts = {f: sum(1 for v in fates.values() if v == f) for f in ("keep", "drop")}
        return {"fates": fates, "counts": counts, "jev_calls": 1, "latency_ms": 500, "status": "ok"}

    def test_recency_matched_spends_exactly_jevs_budget(self):
        """Same number kept, same number dropped. Only WHICH turns differs, so a gap between
        the two arms is Jev's judgement and nothing else."""
        t = turns(40)
        jev = self.selection(40, keep=9, drop=11)
        _, meta = ev.build_input("recency_matched", t, 24_000, 400, jev)
        self.assertEqual(meta["counts"]["keep"], 29)
        self.assertEqual(meta["counts"]["drop"], 11)
        self.assertEqual(meta["jev_calls"], 0)

    def test_recency_matched_keeps_the_newest_and_drops_the_oldest(self):
        t = turns(20)
        prompt, _ = ev.build_input("recency_matched", t, 24_000, 400, self.selection(20, keep=3, drop=4))
        self.assertIn("[KEEP VERBATIM] assistant: turn 19", prompt)
        self.assertNotIn("turn 0 ", prompt)
        self.assertIn("turn 4 ", prompt)

    def test_the_matched_arm_refuses_to_run_without_jevs_counts(self):
        with self.assertRaises(RuntimeError):
            ev.build_input("recency_matched", turns(10), 24_000, 400, None)

    def test_the_free_baseline_is_not_told_its_lines_were_judged(self):
        """The shipped no-Jev path tags every line [background] and the prompt says those
        "only need their gist". Comparing Jev to THAT flatters Jev, so the fair baseline
        gets a prompt that asks for exact values and says nothing about markers."""
        prompt, _ = ev.build_input("tail_plain", turns(10), 24_000, 400, None)
        self.assertNotIn("only need their gist", prompt)
        self.assertNotIn("[background]", prompt)
        self.assertIn("UNCHANGED", prompt)
        fallback, _ = ev.build_input("plugin_fallback", turns(10), 24_000, 400, None)
        self.assertNotIn("[KEEP VERBATIM]", fallback)

    def test_every_capped_arm_gets_the_same_transcript_budget(self):
        t = turns(900)
        for arm in ("plugin_fallback", "tail_plain"):
            prompt, _ = ev.build_input(arm, t, 5_000, 400, None)
            self.assertLess(len(prompt), 5_000 + 2_500, arm)

    def test_the_word_budget_reaches_the_prompt(self):
        prompt, _ = ev.build_input("tail_plain", turns(6), 24_000, 1200, None)
        self.assertIn("Under 1200 words total.", prompt)
        self.assertNotIn("Under 400 words", prompt)

    def test_regex_keep_flags_identifier_shaped_turns_only(self):
        t = [{"role": "user", "content": "thanks, sounds good to me"},
             {"role": "assistant", "content": "the config is at /srv/app/conf/render.toml on port 8431"}] + turns(10)
        _, meta = ev.build_input("regex_keep", t, 24_000, 400, None)
        self.assertGreaterEqual(meta["counts"]["keep"], 9)      # the identifier turn + the last eight
        self.assertGreaterEqual(meta["counts"]["drop"], 1)


class SearchTests(unittest.TestCase):
    def test_search_finds_the_turn_and_shows_the_text_around_the_hit(self):
        rows = [{"role": "assistant", "content": "nothing here about it"},
                {"role": "tool", "content": "x " * 400 + "staging listens on port 8431 behind nginx" + " y" * 400},
                {"role": "user", "content": "ok"}]
        hits = ev.Index(rows).search("staging port")
        self.assertTrue(hits and hits[0].startswith("T1 [tool]"))
        self.assertIn("8431", hits[0])

    def test_an_empty_or_unmatched_query_returns_nothing(self):
        index = ev.Index([{"role": "user", "content": "hello there"}])
        self.assertEqual(index.search(""), [])
        self.assertEqual(index.search("zzzz qqqq"), [])


class ScoreAndSafetyTests(unittest.TestCase):
    def test_recall_is_scored_only_on_questions_the_oracle_could_answer(self):
        questions = [{"id": "q1", "kind": "identifier", "evidence_role": "tool", "third": 0},
                     {"id": "q2", "kind": "decision", "evidence_role": "assistant", "third": 2}]
        out = ev.score(questions, {"q1": "correct", "q2": "wrong"}, sound=[questions[0]])
        self.assertEqual(out["all"]["correct"], 1)
        self.assertEqual(out["sound"], {"n": 1, "correct": 1, "partial": 0, "wrong": 0, "unknown": 0})
        self.assertEqual(out["evidence_role"]["tool"]["correct"], 1)

    def test_output_inside_the_repo_is_refused(self):
        """Exams and capsules are real session content. This repo is public."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                ev.main(["--transcripts", tmp, "--out", str(ROOT / "evals" / "compaction" / "out")])
            self.assertIn("inside the repo", str(caught.exception))

    def test_json_wrapped_in_a_code_fence_still_parses(self):
        self.assertEqual(ev.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ev.parse_json('sure: {"a": 2} done'), {"a": 2})


class MalformedReplyTests(unittest.TestCase):
    def test_a_cut_off_reply_is_asked_again_instead_of_ending_the_run(self):
        """One truncated JSON string ended a transcript's whole run, twenty minutes in."""
        replies = ['{"answers": {"q1": "cut off', '{"answers": {"q1": "8431"}}']
        with unittest.mock.patch.object(ev, "chat", lambda *a, **k: replies.pop(0)):
            out = ev.answer_closed("a note", [{"id": "q1", "question": "which port?"}])
        self.assertEqual(out, {"q1": "8431"})
        self.assertEqual(replies, [])

    def test_a_reply_that_never_parses_is_an_error_not_a_silent_zero(self):
        with unittest.mock.patch.object(ev, "chat", lambda *a, **k: "not json at all"):
            with self.assertRaises(RuntimeError):
                ev.grade([{"id": "q1", "question": "q", "answer": "a"}], {"q1": "a"})

    def test_an_empty_capsule_answers_unknown_without_a_model_call(self):
        with unittest.mock.patch.object(ev, "chat", side_effect=AssertionError("no call expected")):
            self.assertEqual(ev.answer_closed("  ", [{"id": "q1", "question": "q"}]), {"q1": "unknown"})


if __name__ == "__main__":
    unittest.main()
