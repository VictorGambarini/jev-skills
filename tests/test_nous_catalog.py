"""Nous Portal is not on models.dev; the catalog adds it from the Nous API. Offline."""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevkit import catalog  # noqa: E402

NOUS_ROW = {
    "id": "anthropic/claude-opus-4.5", "name": "Claude Opus 4.5", "created": 1763942400,
    "context_length": 200000, "pricing": {"prompt": "0.000005", "completion": "0.000025"},
    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
    "supported_parameters": ["tools", "reasoning"],
}
BASE = "https://inference-api.nousresearch.com/v1"
MODELS_DEV = {"openrouter": {"env": ["OPENROUTER_API_KEY"], "models": {
    "z/cheap": {"cost": {"input": 1, "output": 2}, "tool_call": True, "limit": {"context": 1000}}}}}


class Reply:
    def __init__(self, body=None):
        self.body = json.dumps({"data": [NOUS_ROW]}).encode() if body is None else body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n):
        return self.body


class NousCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.hermes = root / "hermes"
        self.hermes.mkdir()
        (self.hermes / "models_dev_cache.json").write_text(json.dumps(MODELS_DEV))
        (self.hermes / "auth.json").write_text(json.dumps({"providers": {"nous": {
            "inference_base_url": BASE, "agent_key": "k"}}}))
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes), "XDG_CACHE_HOME": str(root / "cache")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saved_nous_models_are_priced_per_million_and_available(self):
        path = catalog._nous_cache_path()
        path.parent.mkdir(parents=True)
        batch = dict(NOUS_ROW, id="anthropic/claude-opus-4.5:batch")
        router = dict(NOUS_ROW, id="openrouter/auto", pricing={"prompt": "-1", "completion": "-1"})
        path.write_text(json.dumps([NOUS_ROW, batch, router]))
        data = catalog.load_models_dev()
        self.assertEqual(list(data["nous"]["models"]), ["anthropic/claude-opus-4.5"])
        self.assertIn("nous", catalog.available_providers(data))
        row = catalog.models(data, providers=["nous"])[0]
        self.assertEqual((row["input"], row["output"], row["context"]), (5.0, 25.0, 200000))
        self.assertTrue(row["vision"] and row["reasoning"])

    def test_without_a_saved_copy_the_hermes_list_is_priced_from_openrouter(self):
        (self.hermes / "provider_models_cache.json").write_text(json.dumps({"nous": {"models": ["z/cheap", "z/unknown"]}}))
        with mock.patch.object(catalog, "_open_nous", side_effect=AssertionError("no network without refresh")):
            data = catalog.load_models_dev()
        self.assertEqual(list(data["nous"]["models"]), ["z/cheap"])

    def test_refresh_fetches_and_saves_with_the_hermes_login(self):
        seen = []
        def fake(request):
            seen.append((request.full_url, request.get_header("Authorization")))
            return Reply()
        with mock.patch.object(catalog, "_load_models_dev", return_value=json.loads(json.dumps(MODELS_DEV))), \
                mock.patch.object(catalog, "_open_nous", side_effect=fake):
            data = catalog.load_models_dev(refresh=True)
        self.assertEqual(seen, [(BASE + "/models", "Bearer k")])
        self.assertIn("anthropic/claude-opus-4.5", data["nous"]["models"])
        self.assertTrue(catalog._nous_cache_path().is_file())

    def test_a_models_dev_nous_entry_wins(self):
        data = catalog._with_nous({"nous": {"models": {"x": {}}}}, refresh=False)
        self.assertEqual(data["nous"], {"models": {"x": {}}})

    def _login(self, base):
        (self.hermes / "auth.json").write_text(json.dumps({"providers": {"nous": {
            "inference_base_url": base, "agent_key": "k"}}}))

    def _refresh(self, **opener):
        with mock.patch.object(catalog, "_load_models_dev", return_value=json.loads(json.dumps(MODELS_DEV))), \
                mock.patch.object(catalog, "_open_nous", **opener) as opened:
            return catalog.load_models_dev(refresh=True), opened

    def test_the_login_is_never_sent_to_userinfo_or_an_unexpected_host(self):
        for base in ["https://user:pw@inference-api.nousresearch.com/v1",
                     "https://user@inference-api.nousresearch.com/v1",
                     "https://inference-api.nousresearch.com@evil.example/v1",
                     "https://evil.example/v1",
                     "https://inference-api.nousresearch.com.evil.example/v1",
                     "https://evil.example/inference-api.nousresearch.com/v1",
                     "https://inference-api.nousresearch.com:8443/v1",
                     "https://inference-api.nousresearch.com/v1?next=https://evil.example",
                     "http://inference-api.nousresearch.com/v1",
                     "https://[::1/v1", ""]:
            with self.subTest(base=base):
                self._login(base)
                self.assertIsNone(catalog._nous_models_url(base))
                _, opened = self._refresh(side_effect=AssertionError("login sent to " + base))
                opened.assert_not_called()
        self.assertEqual(catalog._nous_models_url(BASE + "/"), BASE + "/models")

    def test_a_redirect_is_not_followed_with_the_login(self):
        followed = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/models":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                else:
                    followed.append(self.headers.get("Authorization"))
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = catalog.urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/models", headers={"Authorization": "Bearer k"})
        with self.assertRaises(urllib.error.HTTPError):
            catalog._open_nous(request)
        self.assertEqual(followed, [])

    def test_an_unavailable_endpoint_keeps_the_saved_copy(self):
        path = catalog._nous_cache_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([NOUS_ROW]))
        for failure in [urllib.error.URLError("down"), TimeoutError(), OSError("reset"),
                        urllib.error.HTTPError(BASE + "/models", 503, "busy", {}, None)]:
            with self.subTest(failure=failure):
                data, _ = self._refresh(side_effect=failure)
                self.assertEqual(list(data["nous"]["models"]), ["anthropic/claude-opus-4.5"])
                self.assertEqual(json.loads(path.read_text()), [NOUS_ROW])

    def test_an_unusable_reply_keeps_the_saved_copy(self):
        path = catalog._nous_cache_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([NOUS_ROW]))
        for body in [b"<html>oops</html>", b"[]", b'{"data": []}', b'{"data": "x"}']:
            with self.subTest(body=body):
                data, _ = self._refresh(return_value=Reply(body))
                self.assertEqual(list(data["nous"]["models"]), ["anthropic/claude-opus-4.5"])
                self.assertEqual(json.loads(path.read_text()), [NOUS_ROW])

    MALFORMED = [
        dict(NOUS_ROW, id="bad/context", context_length="200k"),
        dict(NOUS_ROW, id="bad/context-type", context_length=[1]),
        dict(NOUS_ROW, id="bad/created", created=1e300),
        dict(NOUS_ROW, id="bad/created-nan", created=float("nan")),
        dict(NOUS_ROW, id="bad/pricing", pricing="free"),
        dict(NOUS_ROW, id="bad/price-inf", pricing={"prompt": "inf", "completion": "1"}),
        dict(NOUS_ROW, id="bad/architecture", architecture="text"),
        dict(NOUS_ROW, id="bad/params", supported_parameters=3),
        dict(NOUS_ROW, id=7), "not a row", None,
    ]

    def test_a_malformed_saved_row_is_dropped_and_catalog_reads_keep_working(self):
        path = catalog._nous_cache_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.MALFORMED + [NOUS_ROW]))
        data = catalog.load_models_dev()
        self.assertEqual(list(data["nous"]["models"]), ["anthropic/claude-opus-4.5"])
        self.assertIn("z/cheap", data["openrouter"]["models"])
        self.assertEqual({row["model"] for row in catalog.models(data, providers=["nous"])},
                         {"anthropic/claude-opus-4.5"})

    def test_a_saved_copy_with_only_malformed_rows_falls_back_to_the_hermes_list(self):
        path = catalog._nous_cache_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.MALFORMED))
        (self.hermes / "provider_models_cache.json").write_text(json.dumps({"nous": {"models": ["z/cheap", ["x"]]}}))
        data = catalog.load_models_dev()
        self.assertEqual(list(data["nous"]["models"]), ["z/cheap"])

    def test_a_reply_of_only_malformed_rows_does_not_replace_the_saved_copy(self):
        path = catalog._nous_cache_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([NOUS_ROW]))
        data, _ = self._refresh(return_value=Reply(json.dumps({"data": self.MALFORMED}).encode()))
        self.assertEqual(list(data["nous"]["models"]), ["anthropic/claude-opus-4.5"])
        self.assertEqual(json.loads(path.read_text()), [NOUS_ROW])

    def test_only_valid_rows_of_a_mixed_reply_are_saved(self):
        other = dict(NOUS_ROW, id="z/other")
        data, _ = self._refresh(return_value=Reply(json.dumps({"data": self.MALFORMED + [other]}).encode()))
        self.assertEqual(list(data["nous"]["models"]), ["z/other"])
        self.assertEqual(json.loads(catalog._nous_cache_path().read_text()), [other])

    def test_an_unavailable_endpoint_with_no_saved_copy_falls_back_to_the_hermes_list(self):
        (self.hermes / "provider_models_cache.json").write_text(json.dumps({"nous": {"models": ["z/cheap"]}}))
        data, opened = self._refresh(side_effect=urllib.error.URLError("down"))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(list(data["nous"]["models"]), ["z/cheap"])
        self.assertFalse(catalog._nous_cache_path().exists())

    def test_an_unavailable_endpoint_with_nothing_to_fall_back_on_adds_no_nous(self):
        data, _ = self._refresh(side_effect=urllib.error.URLError("down"))
        self.assertNotIn("nous", data)
        self.assertIn("openrouter", data)


if __name__ == "__main__":
    unittest.main()
