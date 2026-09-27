"""`jev models suggest --provider X` builds pools from that provider only. Offline."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevkit import cli  # noqa: E402

MODEL = {"cost": {"input": 1, "output": 2}, "tool_call": True, "limit": {"context": 200000}}
MODELS_DEV = {
    "openrouter": {"env": ["OPENROUTER_API_KEY"], "models": {"z/one": MODEL}},
    "deepseek": {"env": ["DEEPSEEK_API_KEY"], "models": {"deepseek-chat": MODEL}},
}


class SuggestProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        hermes = root / "hermes"
        hermes.mkdir()
        (hermes / "models_dev_cache.json").write_text(json.dumps(MODELS_DEV))
        patcher = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(hermes), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"), "OPENROUTER_API_KEY": "set", "DEEPSEEK_API_KEY": "set"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _suggest(self, *extra):
        seen = []
        with mock.patch.object(cli, "_out", side_effect=lambda value: seen.append(value) or 0):
            cli.main(["models", "suggest", *extra])
        return {ref for pools in seen[0]["suggested_tiers"].values() for pool in pools.values() for ref in pool}

    def test_suggest_with_provider_builds_pools_from_that_provider_only(self):
        self.assertEqual(self._suggest("--provider", "deepseek"), {"deepseek:deepseek-chat"})

    def test_suggest_without_provider_uses_every_available_provider(self):
        self.assertEqual({ref.split(":")[0] for ref in self._suggest()}, {"openrouter", "deepseek"})


if __name__ == "__main__":
    unittest.main()
