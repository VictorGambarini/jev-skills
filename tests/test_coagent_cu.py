"""coagent_cu: the Co-Agent computer-use client. No network: a fake opener stands in for Co-Agent."""
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("coagent_cu", ROOT / "skills" / "jev-computer-use" / "scripts" / "coagent_cu.py")
cu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cu)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        body = self.reply(req) if callable(self.reply) else self.reply
        return FakeResponse(json.dumps(body).encode("utf-8"))


def tool_reply(payload):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}}


class BuildRequestTests(unittest.TestCase):
    def build(self, *argv):
        return cu.build_request(cu.parser().parse_args(list(argv)))

    def test_run_with_inputs_and_expectation(self):
        name, args = self.build("run", "--goal", "Save the form", "--app", "Safari", "--input", "Full name=Ada = Lovelace",
                                "--input", "Email address=ada@example.com", "--expect-text", "Thanks Ada")
        self.assertEqual(name, "computer_run")
        self.assertEqual(args["inputs"], {"Full name": "Ada = Lovelace", "Email address": "ada@example.com"})
        self.assertEqual(args["expect"], {"text_present": "Thanks Ada"})
        self.assertEqual(args["app"], "Safari")

    def test_single_actions_map_to_computer_act(self):
        name, args = self.build("click", "--app", "Epic Games Launcher", "--target", "Library", "--near", "top bar")
        self.assertEqual((name, args["kind"], args["target"], args["near"]), ("computer_act", "click", "Library", "top bar"))
        _, args = self.build("menu", "--app", "Finder", "--path", "File > New Finder Window")
        self.assertEqual(args["path"], ["File", "New Finder Window"])
        _, args = self.build("type", "--target", "Full name", "--text", "Ada", "--replace")
        self.assertTrue(args["replace"])
        _, args = self.build("open", "--app", "Calculator")
        self.assertEqual(args["kind"], "open_app")

    def test_run_needs_a_goal_or_steps(self):
        with self.assertRaises(cu.CuError):
            self.build("run", "--app", "Safari")
        _, args = self.build("run", "--steps", '[{"kind": "click", "target": "7"}]')
        self.assertEqual(args["steps"][0]["target"], "7")

    def test_bad_input_pair(self):
        with self.assertRaises(cu.CuError):
            self.build("run", "--goal", "x", "--input", "no separator")


class TransportTests(unittest.TestCase):
    def test_token_goes_in_the_header_and_never_in_output(self):
        opener = FakeOpener(tool_reply({"ok": True, "status": "done", "verified": True}))
        out = io.StringIO()
        code = cu.main(["click", "--target", "Library"], env={"COAGENT_CU_TOKEN": "sekret-token"}, opener=opener, out=out)
        self.assertEqual(code, 0)
        req = opener.requests[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer sekret-token")
        self.assertEqual(req.full_url, "http://127.0.0.1:8792/mcp")
        sent = json.loads(req.data)
        self.assertEqual(sent["params"]["name"], "computer_act")
        self.assertNotIn("sekret-token", out.getvalue())

    def test_exit_codes_follow_status(self):
        for status, code in (("done", 0), ("needs_approval", 3), ("blocked", 5), ("stalled", 4), ("unverified", 4), ("denied", 3)):
            opener = FakeOpener(tool_reply({"ok": status == "done", "status": status}))
            self.assertEqual(cu.main(["run", "--goal", "g"], env={"COAGENT_CU_TOKEN": "t"}, opener=opener, out=io.StringIO()), code, status)

    def test_missing_token_is_a_usage_error_with_a_fix(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            code = cu.main(["status"], env={"COAGENT_CU_TOKEN_FILE": os.path.join(tmp, "none")}, opener=FakeOpener({}), out=out)
        self.assertEqual(code, 2)
        self.assertIn("setup", out.getvalue())

    def test_only_loopback(self):
        out = io.StringIO()
        code = cu.main(["status"], env={"COAGENT_CU_TOKEN": "t", "COAGENT_CU_URL": "http://example.com:8792"}, opener=FakeOpener({}), out=out)
        self.assertEqual(code, 2)
        self.assertIn("loopback", out.getvalue())

    def test_tool_errors_surface(self):
        opener = FakeOpener({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "error: kind must be one of"}], "isError": True}})
        out = io.StringIO()
        self.assertEqual(cu.main(["status"], env={"COAGENT_CU_TOKEN": "t"}, opener=opener, out=out), 2)
        self.assertIn("kind must be", out.getvalue())

    def test_sse_framed_reply(self):
        class SseOpener(FakeOpener):
            def __call__(self, req, timeout=None):
                self.requests.append(req)
                return FakeResponse(("event: message\ndata: " + json.dumps(tool_reply({"ok": True, "status": "done"})) + "\n\n").encode())
        self.assertEqual(cu.main(["status"], env={"COAGENT_CU_TOKEN": "t"}, opener=SseOpener(None), out=io.StringIO()), 0)


class SetupTests(unittest.TestCase):
    def test_setup_stores_token_0600_and_does_not_print_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            admin = os.path.join(tmp, "admin.token")
            with open(admin, "w") as fh:
                fh.write("admin-secret\n")
            token_file = os.path.join(tmp, "cfg", "cu-token")
            opener = FakeOpener({"name": "Hermes", "token": "client-secret", "mcp_url": "/mcp"})
            out = io.StringIO()
            env = {"COAGENT_ADMIN_TOKEN_FILE": admin, "COAGENT_CU_TOKEN_FILE": token_file}
            self.assertEqual(cu.main(["setup", "--name", "Hermes"], env=env, opener=opener, out=out), 0)
            self.assertEqual(opener.requests[0].get_header("X-admin-token"), "admin-secret")
            self.assertEqual(open(token_file).read().strip(), "client-secret")
            self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
            self.assertNotIn("secret", out.getvalue())
            self.assertEqual(cu.load_token(env), "client-secret")


if __name__ == "__main__":
    unittest.main()
