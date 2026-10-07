"""Per-backend thresholds: knobs, policy copies, the untuned flag and the dead-action guard."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jevkit import backends, choose, client, policy, skillpick, tuning

CHOICE = lambda pick, conf, ids: {"model": "Cloudflare/clef-flash", "usage": {"input_tokens": 9}, "answers": {
    "next_action": {"type": "choice", "choice": pick, "confidence": conf,
                    "probabilities": {i: (conf if i == pick else (1 - conf) / (len(ids) - 1)) for i in ids}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        env = {"JEV_BACKENDS": str(root / "backends.json"), "JEV_HOME": str(root / "home"),
               "XDG_CONFIG_HOME": str(root / "cfg"), "JEV_LEDGER": "off"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("JEV_BACKEND", "JEV_MIN_CONFIDENCE", "TYPESAFE_BASE_URL", "HERMES_HOME"):
            os.environ.pop(name, None)
        backends.add("lais", "https://lais.example/v1/systemone", "Cloudflare/clef-flash")


class Knobs(Base):
    def test_defaults_are_jevs_until_a_backend_sets_one(self):
        self.assertEqual(tuning.value("skillpick.need_threshold"), 0.5)
        backends.tune("lais", {"skillpick.need_threshold": 0.8})
        self.assertEqual(tuning.value("skillpick.need_threshold"), 0.8)
        os.environ["JEV_BACKEND"] = "default"
        self.assertEqual(tuning.value("skillpick.need_threshold"), 0.5)

    def test_unknown_keys_and_out_of_range_values_are_refused(self):
        for changes in ({"nope": 0.5}, {"choose.min_confidence": 0.2}, {"choose.min_confidence": True}):
            with self.subTest(changes=changes), self.assertRaises(backends.BackendError):
                backends.tune("lais", changes)
        self.assertEqual(backends.get("lais").tuning, ())

    def test_clearing_and_re_adding_keep_the_file_consistent(self):
        backends.tune("lais", {"choose.min_confidence": 0.8})
        backends.add("lais", "https://lais.example/v1/systemone", "Cloudflare/clef")  # model change keeps tuning
        self.assertEqual(dict(backends.get("lais").tuning), {"choose.min_confidence": 0.8})
        backends.tune("lais", {"choose.min_confidence": None})
        self.assertNotIn("tuning", json.loads(Path(os.environ["JEV_BACKENDS"]).read_text())["backends"]["lais"])

    def test_a_broken_tuning_entry_falls_back_never_raises(self):
        path = Path(os.environ["JEV_BACKENDS"])
        data = json.loads(path.read_text())
        data["backends"]["lais"]["tuning"] = {"choose.min_confidence": "high"}
        path.write_text(json.dumps(data))
        self.assertEqual(tuning.value("choose.min_confidence"), 0.65)


class Choose(Base):
    IDS = ["lnk-library", "reobserve", "abstain"]

    def request(self, history=()):
        return {"schema": choose.REQUEST_SCHEMA, "goal": "Open the Library page.",
                "candidates": [{"id": "lnk-library", "description": "Click the Library link."},
                               {"id": "reobserve", "description": "Look again."},
                               {"id": "abstain", "description": "Ask the person."}],
                "history": list(history)}

    def ask(self, request, reply):
        with mock.patch.object(client, "_http_transport", return_value=json.dumps(reply).encode()):
            return choose.choose(request)

    def test_a_dead_action_is_not_repeated_however_sure(self):
        dead = [{"selected_id": "lnk-library", "outcome": "no visible change"}] * 2
        out = self.ask(self.request(dead), CHOICE("lnk-library", 0.94, self.IDS))
        self.assertEqual((out["selected_id"], out["reason"]), ("abstain", "already tried with no visible effect"))
        once = self.ask(self.request(dead[:1]), CHOICE("lnk-library", 0.94, self.IDS))
        self.assertEqual(once["selected_id"], "lnk-library")
        worked = [{"selected_id": "lnk-library", "outcome": "opened a menu"}] * 3
        self.assertEqual(self.ask(self.request(worked), CHOICE("lnk-library", 0.94, self.IDS))["selected_id"], "lnk-library")

    def test_the_backend_floor_applies_unless_the_caller_set_one(self):
        backends.tune("lais", {"choose.min_confidence": 0.9})
        self.assertEqual(self.ask(self.request(), CHOICE("lnk-library", 0.85, self.IDS))["selected_id"], "reobserve")
        with mock.patch.object(choose, "MIN_CONFIDENCE", 0.0):
            self.assertEqual(self.ask(self.request(), CHOICE("lnk-library", 0.85, self.IDS))["selected_id"], "lnk-library")
        os.environ["JEV_MIN_CONFIDENCE"] = "0.8"
        try:
            self.assertEqual(choose.floor(), choose.MIN_CONFIDENCE)
        finally:
            os.environ.pop("JEV_MIN_CONFIDENCE")


class Policies(Base):
    def test_a_backend_copy_wins_only_while_that_backend_is_active(self):
        from jevkit import cli
        with mock.patch.object(cli, "_out", side_effect=lambda value: 0) as out:
            self.assertEqual(cli.main(["backend", "policy", "lais", "gate-strict"]), 0)
        written = Path(out.call_args[0][0]["written"])
        self.assertEqual(json.loads(written.read_text())["tuned_on"], "Cloudflare/clef-flash")
        loaded = policy.load("gate-strict")
        self.assertEqual(loaded["_origin"], "backend")
        self.assertFalse(policy.untuned(loaded, "lais"))
        self.assertTrue(policy.untuned(policy.load("triage-urgency"), "lais"))
        self.assertFalse(policy.untuned(policy.load("triage-urgency"), "typesafe"))
        os.environ["JEV_BACKEND"] = "default"
        self.assertEqual(policy.load("gate-strict")["_origin"], "shipped")
        with mock.patch.object(cli, "_out", side_effect=lambda value: 0):
            self.assertEqual(cli.main(["backend", "policy", "lais", "gate-strict"]), 1)  # exists, no --force

    def test_tuned_on_still_requires_a_version_for_jev(self):
        self.assertTrue(any("tuned_on" in p for p in policy.lint({**policy.load("gate-strict"), "tuned_on": "jev-latest"})))
        self.assertFalse(any("tuned_on" in p for p in policy.lint({**policy.load("gate-strict"), "tuned_on": "Cloudflare/clef-flash"})))


if __name__ == "__main__":
    unittest.main()
