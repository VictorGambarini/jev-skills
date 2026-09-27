#!/usr/bin/env python3
"""coagent_cu: drive the desktop through Co-Agent's computer-use service.

Co-Agent (a Mac app) already holds Accessibility and Screen Recording, so it is
the fastest safe hands on a Mac: its `computer_run` tool lets Jev pick every
step from a closed menu built from a fresh observation, acts, and verifies each
step on a new observation. This CLI is a thin, standard-library client for
agents that shell out (Hermes' terminal tool, cron jobs, scripts). Agents that
speak MCP can call the same tools directly: computer_status, computer_observe,
computer_act, computer_run.

    coagent_cu.py status
    coagent_cu.py observe --app "System Settings" [--query Appearance]
    coagent_cu.py click  --app "Epic Games Launcher" --target Library [--near "top bar"] [--expect-text "ENGINE VERSIONS"]
    coagent_cu.py type   --app Safari --target "Full name" --text "Ada Lovelace" [--replace]
    coagent_cu.py key    --app Safari --keys cmd+r
    coagent_cu.py run    --goal "Open the Library tab" --app "Epic Games Launcher" --expect-text "ENGINE VERSIONS"
    coagent_cu.py run    --goal "Fill and save the form" --app Safari --input "Full name=Ada" --input "Email address=ada@example.com" --expect-text "Thanks Ada"
    coagent_cu.py call computer_act '{"kind": "scroll", "app": "Safari", "direction": "down"}'
    coagent_cu.py setup --name "Hermes"     # one-time: provision this machine's token

Exit codes: 0 done, 3 needs_approval (nothing was done: tell the person, do not
work around it), 4 not verified / stalled / loop / needs_input, 5 blocked (lock
screen, macOS prompt, missing permission, covered window), 2 usage or connection
error.

The token is read from $COAGENT_CU_TOKEN, else the file named by
$COAGENT_CU_TOKEN_FILE, else ~/.config/coagent/cu-token. It is never printed.
"""
import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8792"
DEFAULT_TOKEN_FILE = os.path.join("~", ".config", "coagent", "cu-token")
DEFAULT_ADMIN_TOKEN_FILE = os.path.join("~", "Library", "Application Support", "Co-Agent", "runtime", "secret", "admin.token")

EXIT_OK, EXIT_USAGE, EXIT_APPROVAL, EXIT_UNVERIFIED, EXIT_BLOCKED = 0, 2, 3, 4, 5
STATUS_EXIT = {
    "done": EXIT_OK,
    "needs_approval": EXIT_APPROVAL,
    "denied": EXIT_APPROVAL,
    "blocked": EXIT_BLOCKED,
}


class CuError(Exception):
    pass


def base_url(env=None):
    env = os.environ if env is None else env
    url = (env.get("COAGENT_CU_URL") or DEFAULT_URL).rstrip("/")
    # Loopback only: the token must never leave this machine.
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "[::1]"):
        raise CuError("COAGENT_CU_URL must be a loopback address")
    return url


def load_token(env=None):
    env = os.environ if env is None else env
    token = (env.get("COAGENT_CU_TOKEN") or "").strip()
    if token:
        return token
    path = os.path.expanduser(env.get("COAGENT_CU_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        token = ""
    if not token:
        raise CuError("no Co-Agent token: run `coagent_cu.py setup --name <agent>` on this Mac, or set COAGENT_CU_TOKEN")
    return token


def post_json(url, payload, headers, timeout, opener=None):
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    try:
        with opener(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        if err.code == 401:
            raise CuError("Co-Agent rejected the token (401): re-run setup")
        raise CuError("Co-Agent answered HTTP %s" % err.code)
    except (urllib.error.URLError, OSError) as err:
        raise CuError("Co-Agent is not reachable at %s (%s): is the app running?" % (url, getattr(err, "reason", err)))
    # Streamable HTTP servers may answer as one SSE event.
    if body.lstrip().startswith("event:") or body.lstrip().startswith("data:"):
        data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        body = data[-1] if data else "{}"
    try:
        return json.loads(body)
    except ValueError:
        raise CuError("Co-Agent sent a reply that is not JSON")


def call_tool(name, arguments, env=None, opener=None, timeout=200):
    url = base_url(env) + "/mcp"
    token = load_token(env)
    reply = post_json(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
                      {"Authorization": "Bearer " + token}, timeout, opener)
    if reply.get("error"):
        raise CuError("tool error: %s" % (reply["error"].get("message") or reply["error"]))
    result = reply.get("result") or {}
    if result.get("isError"):
        text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        raise CuError("tool error: %s" % (text[:300] or "unknown"))
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for c in result.get("content", []):
        if isinstance(c, dict) and c.get("type") == "text":
            try:
                return json.loads(c.get("text", ""))
            except ValueError:
                return {"text": c.get("text", "")}
    return result


def exit_code_for(result):
    if not isinstance(result, dict):
        return EXIT_UNVERIFIED
    status = result.get("status")
    if status in STATUS_EXIT:
        return STATUS_EXIT[status]
    if status is None and result.get("ok") is True:
        return EXIT_OK
    if result.get("ok") is True:
        return EXIT_OK
    return EXIT_UNVERIFIED


def parse_inputs(pairs):
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise CuError("--input must look like 'Field name=text'")
        key, value = pair.split("=", 1)
        if not key.strip():
            raise CuError("--input needs a field name before '='")
        out[key.strip()] = value
    return out


def expect_from(args):
    exp = {}
    if getattr(args, "expect_text", None):
        exp["text_present"] = args.expect_text if len(args.expect_text) > 1 else args.expect_text[0]
    if getattr(args, "expect_gone", None):
        exp["text_absent"] = args.expect_gone
    if getattr(args, "expect_title", None):
        exp["window_title"] = args.expect_title
    return exp or None


def build_request(args):
    """Turn parsed arguments into (tool name, arguments). Pure: no I/O."""
    where = {k: v for k, v in (("app", getattr(args, "app", None)), ("bundle", getattr(args, "bundle", None))) if v}
    if args.command == "status":
        return "computer_status", {}
    if args.command == "observe":
        a = dict(where)
        if args.query:
            a["query"] = args.query
        if args.ocr:
            a["ocr"] = args.ocr
        if args.actionable:
            a["actionable_only"] = True
        return "computer_observe", a
    if args.command == "run":
        a = dict(where)
        if args.goal:
            a["goal"] = args.goal
        if args.open:
            a["open"] = True
        inputs = parse_inputs(args.input)
        if inputs:
            a["inputs"] = inputs
        exp = expect_from(args)
        if exp:
            a["expect"] = exp
        if args.max_steps:
            a["max_steps"] = args.max_steps
        if args.steps:
            a["steps"] = json.loads(args.steps)
        if not a.get("goal") and not a.get("steps"):
            raise CuError("run needs --goal or --steps")
        return "computer_run", a
    if args.command == "call":
        return args.tool, json.loads(args.json or "{}")
    # Single actions.
    a = {"kind": {"click": "click", "double-click": "double_click", "type": "type", "key": "key", "scroll": "scroll", "menu": "menu", "open": "open_app", "wait": "wait"}[args.command]}
    a.update(where)
    for field in ("target", "near", "role", "obs", "el", "text", "keys", "direction", "approval"):
        value = getattr(args, field, None)
        if value not in (None, ""):
            a[field] = value
    if getattr(args, "x", None) is not None and getattr(args, "y", None) is not None:
        a["x"], a["y"] = args.x, args.y
    if getattr(args, "index", None) is not None:
        a["index"] = args.index
    if getattr(args, "replace", False):
        a["replace"] = True
    if getattr(args, "path", None):
        a["path"] = [p.strip() for p in args.path.split(">") if p.strip()]
    if getattr(args, "ms", None):
        a["ms"] = args.ms
    exp = expect_from(args)
    if exp:
        a["expect"] = exp
    return "computer_act", a


def setup(name, env=None, opener=None):
    """Provision a local Co-Agent client for this agent and store its token 0600. Prints nothing secret."""
    env = os.environ if env is None else env
    admin_path = os.path.expanduser(env.get("COAGENT_ADMIN_TOKEN_FILE") or DEFAULT_ADMIN_TOKEN_FILE)
    try:
        with open(admin_path, "r", encoding="utf-8") as fh:
            admin = fh.read().strip()
    except OSError:
        raise CuError("Co-Agent's admin token is not readable here; run setup as the Mac's owner user")
    reply = post_json(base_url(env) + "/admin/computer-use/client", {"name": name}, {"x-admin-token": admin}, 20, opener)
    token = (reply or {}).get("token")
    if not token:
        raise CuError("Co-Agent did not issue a token: %s" % ((reply or {}).get("error") or "unknown"))
    path = os.path.expanduser(env.get("COAGENT_CU_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.chmod(path, 0o600)
    return {"ok": True, "client": reply.get("name", name), "token_file": path}


def parser():
    p = argparse.ArgumentParser(prog="coagent_cu.py", description="Drive this Mac through Co-Agent computer use (Jev picks, Co-Agent acts and verifies).")
    sub = p.add_subparsers(dest="command", required=True)

    def where(sp):
        sp.add_argument("--app")
        sp.add_argument("--bundle")

    def expects(sp):
        sp.add_argument("--expect-text", action="append", help="text that must be visible afterwards (repeatable)")
        sp.add_argument("--expect-gone", help="text that must be gone afterwards")
        sp.add_argument("--expect-title", help="window title afterwards")

    def target(sp):
        sp.add_argument("--target", help="visible label")
        sp.add_argument("--near", help="top bar | left sidebar | main area | right side | bottom")
        sp.add_argument("--role")
        sp.add_argument("--index", type=int)
        sp.add_argument("--obs")
        sp.add_argument("--el")
        sp.add_argument("--x", type=float)
        sp.add_argument("--y", type=float)
        sp.add_argument("--approval", help="an approved ticket id")

    sub.add_parser("status")
    s = sub.add_parser("observe"); where(s); s.add_argument("--query"); s.add_argument("--ocr", choices=["auto", "always", "never"]); s.add_argument("--actionable", action="store_true")
    for name in ("click", "double-click"):
        s = sub.add_parser(name); where(s); target(s); expects(s)
    s = sub.add_parser("type"); where(s); target(s); expects(s); s.add_argument("--text", required=True); s.add_argument("--replace", action="store_true")
    s = sub.add_parser("key"); where(s); expects(s); s.add_argument("--keys", required=True); s.add_argument("--approval")
    s = sub.add_parser("scroll"); where(s); target(s); s.add_argument("--direction", choices=["up", "down"], default="down")
    s = sub.add_parser("menu"); where(s); s.add_argument("--path", required=True, help='e.g. "File > New Window"'); s.add_argument("--approval")
    s = sub.add_parser("open"); where(s)
    s = sub.add_parser("wait"); s.add_argument("--ms", type=int, default=500)
    s = sub.add_parser("run"); where(s); expects(s)
    s.add_argument("--goal"); s.add_argument("--open", action="store_true"); s.add_argument("--input", action="append", help="'Field name=text' (repeatable)")
    s.add_argument("--max-steps", type=int); s.add_argument("--steps", help="JSON list of computer_act argument objects (no Jev)")
    s = sub.add_parser("call"); s.add_argument("tool"); s.add_argument("json", nargs="?")
    s = sub.add_parser("setup"); s.add_argument("--name", required=True)
    return p


def main(argv=None, env=None, opener=None, out=None):
    out = out or sys.stdout
    args = parser().parse_args(argv)
    try:
        if args.command == "setup":
            result = setup(args.name, env=env, opener=opener)
            out.write(json.dumps(result, indent=1) + "\n")
            return EXIT_OK
        tool, arguments = build_request(args)
        result = call_tool(tool, arguments, env=env, opener=opener)
    except CuError as err:
        out.write(json.dumps({"ok": False, "error": str(err)}) + "\n")
        return EXIT_USAGE
    except ValueError as err:
        out.write(json.dumps({"ok": False, "error": "bad JSON argument: %s" % err}) + "\n")
        return EXIT_USAGE
    out.write(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    return exit_code_for(result)


if __name__ == "__main__":
    sys.exit(main())
