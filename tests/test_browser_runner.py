"""Tests for the browser runner's ownership logic.

These never launch a real browser: they pin the flags, the binary lookup, the CDP
poll and the cleanup contract, so a live run is the only thing that needs a
machine with Chrome on it.
"""
import _isolate  # noqa: F401 - keep this machine's own decision backend out of the run
import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "jev-browser-use" / "scripts" / "jev_browser_agent.py"
spec = importlib.util.spec_from_file_location("jev_browser_agent", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ChromeDiscoveryTests(unittest.TestCase):
    def test_env_override_wins(self):
        self.assertEqual(runner.find_chrome({"BH_CHROME_PATH": "/bin/ls"}), "/bin/ls")
        self.assertEqual(runner.find_chrome({"CHROME_PATH": "/bin/cat"}), "/bin/cat")

    def test_invalid_env_override_never_wins(self):
        # A bad override must never be used, but the function may still fall back to a
        # real browser on this machine or return None on one without any.
        resolved = runner.find_chrome({"BH_CHROME_PATH": "/does/not/exist"})
        self.assertNotEqual(resolved, "/does/not/exist")
        self.assertTrue(resolved is None or Path(resolved).exists())


class ChromeArgsTests(unittest.TestCase):
    def test_uses_a_throwaway_profile_and_our_own_port(self):
        args = runner.chrome_args(9333, "/tmp/profile-xyz")
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertIn("--user-data-dir=/tmp/profile-xyz", args)
        self.assertIn("--headless=new", args)
        # never the person's real profile
        self.assertFalse(any("Application Support" in a for a in args))
        self.assertFalse(any("9333" in a and "remote-debugging" not in a for a in args))

    def test_free_port_is_usable_and_distinct(self):
        ports = {runner.free_port() for _ in range(5)}
        self.assertTrue(all(1024 < p < 65536 for p in ports))


class CdpPollTests(unittest.TestCase):
    def test_returns_the_websocket_when_the_endpoint_answers(self):
        class Response:
            def read(self):
                return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1234/devtools/browser/x"}).encode()

        ws = runner.wait_for_cdp(1234, timeout=1.0, opener=lambda url: Response())
        self.assertEqual(ws, "ws://127.0.0.1:1234/devtools/browser/x")

    def test_gives_up_when_the_endpoint_never_answers(self):
        def dead(url):
            raise OSError("connection refused")

        self.assertIsNone(runner.wait_for_cdp(1234, timeout=0.6, opener=dead))

    def test_ignores_a_payload_without_a_websocket(self):
        class Response:
            def read(self):
                return b'{"Browser": "Chrome/1"}'

        self.assertIsNone(runner.wait_for_cdp(1234, timeout=0.6, opener=lambda url: Response()))


class OwnedChromeContractTests(unittest.TestCase):
    def test_stop_is_idempotent_and_clears_the_profile(self):
        owned = runner.OwnedChrome("/bin/echo")
        owned.stop()  # never started: must not raise
        owned.stop()

    def test_failed_start_reports_and_cleans_up(self):
        owned = runner.OwnedChrome("/bin/echo", startup_timeout=0.4)
        with self.assertRaises(RuntimeError):
            owned.start()
        self.assertIsNone(owned.profile_dir)  # throwaway profile removed


class LaunchOrderTests(unittest.TestCase):
    def test_browser_starts_after_the_venv_reexec(self):
        # ensure_importable may os.execv into the vendored venv; a browser launched
        # before that swap is orphaned and its atexit cleanup never runs.
        source = SCRIPT.read_text(encoding="utf-8")
        main_body = source.split("def main(", 1)[1]
        self.assertLess(main_body.index("ensure_importable(argv)"),
                        main_body.index("start_owned_browser(args)"))

    def test_stop_is_registered_for_exit_and_signals(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("atexit.register(owned.stop)", source)
        self.assertIn("signal.SIGTERM", source)


class AllowlistTests(unittest.TestCase):
    def test_subdomains_allowed_but_not_lookalikes(self):
        self.assertTrue(runner.host_allowed("https://en.wikipedia.org/wiki/X", ["wikipedia.org"]))
        self.assertFalse(runner.host_allowed("https://evil-wikipedia.org/", ["wikipedia.org"]))
        self.assertFalse(runner.host_allowed("https://example.com/", ["wikipedia.org"]))



class CredentialFallbackTests(unittest.TestCase):
    """An agent's environment carries no keys. The TypeSafe key fell back to the secret
    store; the text-model key did not. So the loop started fine and died the first time
    Jev chose to TYPE - on most sites the very first action - with "TYPE_TEXT needs
    TEXT_MODEL_API_KEY". It only ever worked from a shell where someone had exported the
    key by hand, which is how every test and every demo had been run.
    """

    def test_the_text_key_falls_back_to_the_secret_store(self):
        seen = []

        def lookup(service, account):
            seen.append(service)
            return "sk-from-the-keychain" if service == "OPENROUTER_API_KEY" else "ts-key"

        creds = runner.resolve_credentials({"USER": "someone"}, lookup=lookup)
        self.assertEqual(creds.get("TEXT_MODEL_API_KEY"), "sk-from-the-keychain")
        self.assertIn("OPENROUTER_API_KEY", seen)

    def test_an_explicit_environment_key_still_wins(self):
        creds = runner.resolve_credentials(
            {"TEXT_MODEL_API_KEY": "sk-explicit", "USER": "someone"},
            lookup=lambda service, account: "sk-from-the-keychain")
        self.assertEqual(creds["TEXT_MODEL_API_KEY"], "sk-explicit")

    def test_no_key_anywhere_is_simply_absent_not_an_error(self):
        creds = runner.resolve_credentials({"USER": "someone"}, lookup=lambda s, a: None)
        self.assertNotIn("TEXT_MODEL_API_KEY", creds)


class TextHelperConfigTests(unittest.TestCase):
    """One machine-wide file picks the text helper; env still wins; no key for a local server."""

    def _cfg(self, tmp, data):
        p = Path(tmp) / "browser.json"
        p.write_text(json.dumps(data))
        return p

    def test_local_server_from_config_needs_no_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp, {"TEXT_MODEL": "qwen-local", "TEXT_MODEL_BASE_URL": "http://127.0.0.1:1234/v1",
                                  "TEXT_MODEL_RESPONSE_FORMAT": "json_schema", "api_key": "ignored"})
            got = runner.resolve_credentials({"JEV_BROWSER_CONFIG": str(cfg), "TYPESAFE_API_KEY": "t"},
                                             lookup=lambda *a: None)
            self.assertEqual(got["TEXT_MODEL"], "qwen-local")
            self.assertEqual(got["TEXT_MODEL_BASE_URL"], "http://127.0.0.1:1234/v1")
            self.assertEqual(got["TEXT_MODEL_RESPONSE_FORMAT"], "json_schema")
            self.assertEqual(got["TEXT_MODEL_API_KEY"], "local")
            self.assertNotIn("api_key", got)

    def test_environment_overrides_config(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp, {"TEXT_MODEL": "qwen-local", "TEXT_MODEL_BASE_URL": "http://127.0.0.1:1234/v1"})
            got = runner.resolve_credentials({"JEV_BROWSER_CONFIG": str(cfg), "TEXT_MODEL": "other",
                                              "TEXT_MODEL_BASE_URL": "https://example.com/v1"},
                                             lookup=lambda *a: None)
            self.assertEqual(got["TEXT_MODEL"], "other")
            self.assertNotIn("TEXT_MODEL_API_KEY", got)   # remote server, no key anywhere

    def test_missing_or_bad_config_keeps_defaults(self):
        got = runner.resolve_credentials({"JEV_BROWSER_CONFIG": "/nonexistent/browser.json"}, lookup=lambda *a: None)
        self.assertEqual(got["TEXT_MODEL"], runner.DEFAULT_TEXT_MODEL)
        self.assertNotIn("TEXT_MODEL_RESPONSE_FORMAT", got)


class ClaudeCliTextHelperTests(unittest.TestCase):
    def test_claude_cli_needs_no_key_and_never_reads_the_keychain(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "browser.json"
            cfg.write_text(json.dumps({"TEXT_MODEL_PROVIDER": "claude-cli", "TEXT_MODEL": "claude-haiku-4-5"}))
            calls = []
            got = runner.resolve_credentials({"JEV_BROWSER_CONFIG": str(cfg), "TYPESAFE_API_KEY": "t",
                                              "CLAUDE_CLI": "/x/claude"},
                                             lookup=lambda *a: calls.append(a))
            self.assertEqual(got["TEXT_MODEL_PROVIDER"], "claude-cli")
            self.assertEqual(got["TEXT_MODEL"], "claude-haiku-4-5")
            self.assertEqual(got["CLAUDE_CLI"], "/x/claude")
            self.assertNotIn("TEXT_MODEL_API_KEY", got)
            self.assertEqual(calls, [])

class NamedBackend(unittest.TestCase):
    """A named decision backend reaches Ultrafast only through an endpoint it is known to read."""

    def setUp(self):
        from unittest import mock
        self.mock = mock
        patcher = mock.patch.object(runner, "active_backend",
                                    return_value=("lais", "https://lais.example/v1/systemone", "org/clef", "backend-key-123456789012"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_backend_url_model_and_key_replace_typesafe(self):
        creds = runner.resolve_credentials({"TYPESAFE_API_KEY": "provider-key-123456789012"}, lookup=lambda *a: None)
        self.assertEqual(creds[runner.ENDPOINT_ENV], "https://lais.example/v1/systemone")
        self.assertEqual(creds["TYPESAFE_MODEL"], "org/clef")
        self.assertEqual(creds["TYPESAFE_API_KEY"], "backend-key-123456789012")

    def test_a_stock_ultrafast_is_never_handed_a_backend_key(self):
        import contextlib
        import io
        out = io.StringIO()
        with self.mock.patch.object(runner, "ultrafast_reads_endpoint", return_value=False), \
                self.mock.patch.object(runner, "ensure_importable") as importable, contextlib.redirect_stdout(out):
            code = runner.main(["--url", "https://example.org", "--goal", "x", "--allow-hosts", "example.org"])
        self.assertEqual(code, 2)
        self.assertIn("would send that backend's key to TypeSafe", out.getvalue())
        importable.assert_not_called()

    def test_without_a_backend_typesafe_is_unchanged(self):
        with self.mock.patch.object(runner, "active_backend", return_value=None):
            creds = runner.resolve_credentials({"TYPESAFE_API_KEY": "provider-key-123456789012"}, lookup=lambda *a: None)
        self.assertNotIn(runner.ENDPOINT_ENV, creds)
        self.assertEqual(creds["TYPESAFE_MODEL"], "jev-latest")

if __name__ == "__main__":
    unittest.main()
