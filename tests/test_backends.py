"""Named decision backends: a self-hosted systemone server used in place of Jev."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jevkit import backends, cli, client, key_setup, keystore, ledger

REPLY = {"model": "Cloudflare/clef-flash",
         "answers": {"ok": {"type": "noul", "noul": 0.93}},
         "usage": {"input_tokens": 120, "output_tokens": 0}}


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = Path(self.dir.name) / "backends.json"
        env = {"JEV_BACKENDS": str(self.config), "XDG_CONFIG_HOME": self.dir.name}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("JEV_BACKEND", "JEV_MODEL", "TYPESAFE_BASE_URL", "TYPESAFE_MODEL", "TYPESAFE_API_KEY",
                     "JEV_BACKEND_LAIS_API_KEY"):
            os.environ.pop(name, None)
        # No real secret store is read or written by these tests.
        for target in ("_keychain_lookup", "_keychain_store"):
            stub = mock.patch.object(keystore, target, return_value=None if target == "_keychain_lookup" else False)
            stub.start()
            self.addCleanup(stub.stop)

    def add_lais(self, **extra):
        return backends.add("lais", "https://lais.example/v1/systemone", "Cloudflare/clef", **extra)

    def capture(self, **ask_kwargs):
        seen = {}

        def fake(body, headers, timeout, url=client.ENDPOINT, max_bytes=client.MAX_RESPONSE_BYTES):
            seen.update(url=url, body=json.loads(body), headers=headers)
            return json.dumps(REPLY).encode()

        with mock.patch.object(client, "_http_transport", fake):
            reply = client.ask("state", {"ok": client.noul("It worked")}, **ask_kwargs)
        return seen, reply

    def test_no_file_means_builtin_providers(self):
        self.assertIsNone(backends.active())

    def test_default_backend_gets_url_model_and_only_its_own_key(self):
        self.add_lais()
        with mock.patch.dict(os.environ, {"JEV_BACKEND_LAIS_API_KEY": "backend-key-0123456789",
                                          "TYPESAFE_API_KEY": "provider-key-0123456789"}):
            seen, reply = self.capture()
        self.assertEqual(seen["url"], "https://lais.example/v1/systemone")
        self.assertEqual(seen["body"]["model"], "Cloudflare/clef")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer backend-key-0123456789")
        self.assertEqual(reply["provider"], "lais")
        self.assertEqual(reply["jev_model"], "Cloudflare/clef-flash")

    def test_backend_without_key_sends_no_provider_key(self):
        self.add_lais()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "provider-key-0123456789"}):
            seen, _ = self.capture()
        self.assertNotIn("Authorization", seen["headers"])

    def test_jev_backend_default_returns_to_providers(self):
        self.add_lais()
        with mock.patch.dict(os.environ, {"JEV_BACKEND": "default"}):
            self.assertIsNone(backends.active())

    def test_selected_but_undefined_backend_fails_open_not_to_jev(self):
        with mock.patch.dict(os.environ, {"JEV_BACKEND": "missing", "TYPESAFE_API_KEY": "provider-key-0123456789"}), \
                mock.patch.object(client, "_http_transport") as transport:
            with self.assertRaises(client.JevError) as caught:
                client.ask("state", {"ok": client.noul("It worked")})
        self.assertEqual(caught.exception.code, "backend_misconfigured")
        transport.assert_not_called()

    def test_explicit_provider_still_wins(self):
        self.add_lais()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "provider-key-0123456789"}):
            seen, reply = self.capture(provider="typesafe")
        self.assertEqual(seen["url"], client.ENDPOINT)
        self.assertEqual(reply["provider"], "typesafe")

    def test_jev_model_overrides_backend_model(self):
        self.add_lais()
        with mock.patch.dict(os.environ, {"JEV_MODEL": "Cloudflare/clef-flash"}):
            seen, _ = self.capture()
        self.assertEqual(seen["body"]["model"], "Cloudflare/clef-flash")

    def test_url_and_name_rules(self):
        for url in ("http://lais.example/v1/systemone", "https://user:pw@lais.example/v1/systemone",
                    "https://lais.example/v1/systemone?x=1", "https://lais.example", "ftp://lais.example/x"):
            with self.subTest(url=url), self.assertRaises(backends.BackendError):
                backends.add("lais", url, "m")
        self.assertEqual(backends.check_url("http://127.0.0.1:8787/v1/systemone"), "http://127.0.0.1:8787/v1/systemone")
        for name in ("zen", "Upper", "has space", ""):
            with self.subTest(name=name), self.assertRaises(backends.BackendError):
                backends.add(name, "https://lais.example/v1/systemone", "m")
        self.assertFalse(self.config.exists(), "a refused add must not write the file")

    def test_add_use_remove(self):
        self.add_lais()
        backends.add("local", "http://127.0.0.1:9000/v1/systemone", "m", make_default=False)
        self.assertEqual(backends.load_file()["default"], "lais")
        backends.use("local")
        self.assertEqual(backends.active().name, "local")
        backends.remove("local")
        self.assertIsNone(backends.load_file()["default"])

    def test_key_store_and_describe_never_show_the_key(self):
        backend = self.add_lais()
        stored = keystore.store_backend("backend-key-0123456789", backend)
        self.assertNotIn("backend-key", json.dumps(stored))
        path = keystore.backend_credentials_file(backend)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        described = keystore.describe()
        self.assertTrue(described["present"])
        self.assertEqual(described["provider"], "lais")
        self.assertNotIn("backend-key", json.dumps(described))
        # The provider resolution is untouched by a backend's stored key.
        self.assertIsNone(keystore.resolve("typesafe"))

    def test_backend_calls_bill_nothing(self):
        self.assertEqual(ledger.cost(1000, "lais", "Cloudflare/clef")["cost_usd"], 0.0)
        self.assertGreater(ledger.cost(1000, "typesafe", "jev-1.13.0")["cost_usd"], 0.0)

    def test_cli_test_reports_answers(self):
        self.add_lais()
        reply = {"model": "Cloudflare/clef-flash", "usage": {"input_tokens": 10},
                 "answers": {"ok": {"type": "noul", "noul": 0.9},
                             "kind": {"type": "choice", "choice": "success", "confidence": 0.95,
                                      "probabilities": {"success": 0.95, "failure": 0.05}}}}
        with mock.patch.object(client, "_http_transport", return_value=json.dumps(reply).encode()), \
                mock.patch.object(cli, "_out", side_effect=lambda value: 0) as out:
            code = cli.main(["backend", "test", "lais"])
        self.assertEqual(code, 0)
        self.assertTrue(out.call_args[0][0]["ok"])

    def test_key_page_rejects_only_a_refused_key(self):
        backend = self.add_lais()
        with mock.patch.object(client, "_http_transport", side_effect=client.JevError("auth_failed")):
            result = key_setup._finish("backend-key-0123456789", True, False, None, backend=backend)
        self.assertEqual((result["status"], result["error"]), ("rejected", "auth_failed"))
        self.assertIn("refused that key", result["reason"])
        self.assertFalse(keystore.backend_credentials_file(backend).exists(), "a refused key is not stored")

    def test_a_server_problem_stores_the_key_and_says_why(self):
        backend = self.add_lais()
        for code, words in (("timeout", "did not answer in time"), ("malformed", "cannot read"),
                            ("http_503", "server error"), ("http_422", "refused the check request")):
            with self.subTest(code=code), \
                    mock.patch.object(client, "_http_transport", side_effect=client.JevError(code)):
                result = key_setup._finish("backend-key-0123456789", True, False, None, backend=backend)
            self.assertEqual((result["status"], result["verified"], result["check_error"]), ("stored", False, code))
            self.assertIn(words, result["warning"])
            self.assertNotIn("backend-key", json.dumps(result))

    def test_a_refused_request_keeps_the_servers_reason(self):
        response = mock.Mock(status=422, getheader=lambda name: None)
        response.read.return_value = b'{"detail": "questions.ok.criteria: field required"}'
        connection = mock.Mock(getresponse=mock.Mock(return_value=response))
        with mock.patch.object(client._POOL, "borrow", return_value=connection), \
                mock.patch.object(client._POOL, "drop"):
            with self.assertRaises(client.JevError) as caught:
                client._http_transport(b"{}", {}, 5.0, "https://lais.example/v1/systemone")
        self.assertEqual(caught.exception.code, "http_422")
        self.assertIn("field required", str(caught.exception))
        response.status = 401
        response.read.return_value = b'{"error": "bad token tok-123"}'
        with mock.patch.object(client._POOL, "borrow", return_value=connection), \
                mock.patch.object(client._POOL, "drop"):
            with self.assertRaises(client.JevError) as caught:
                client._http_transport(b"{}", {}, 5.0, "https://lais.example/v1/systemone")
        self.assertNotIn("tok-123", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
