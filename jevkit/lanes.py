"""Lanes: finish with the smallest model and the lowest effort that still gets it right.

After @0x_rody, "Claude -> Opus 5.5 -> Jev: multi-model coding orchestration":

* **Four lanes.** ``small`` < ``medium`` < ``high`` < ``escalate``. Each maps to a real model and
  reasoning effort per host (``TARGETS``; a local ``lanes.json`` overrides). Always start in the
  lowest lane that is enough; a stronger model thinks more at the same effort, so start one
  level lower than habit says.
* **Jev decides, it does not do.** Jev picks the first lane (policy ``lane``) and, after each
  implementation cycle, whether to continue, retry, verify, escalate or complete (policy
  ``loop-step``). It never writes code or designs anything.
* **Deterministic first.** Tests, compilers, type checkers, linters and ``git diff`` answer
  what they can before Jev is asked (``evidence`` runs them; the policy's pre-rules read the
  facts). Only the short tail of a long shell output goes into Jev's state: compaction is for
  tool output, never for anyone's reasoning.
* **Escalation needs evidence.** Only repeated failures, failing checks, security-sensitive
  changes, unresolved design doubt or low Jev confidence move work up a lane, one step at a
  time. Never because a stronger model exists.
* **Completion is earned.** ``step`` refuses a ``complete`` that the deterministic facts do not
  support: checks must have passed and the diff must stay in scope. A failed verification is
  never hidden.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import decide as engine, paths

LANES = ("small", "medium", "high", "escalate")
STEP_ACTIONS = ("continue", "retry", "verify", "escalate", "complete")
HOSTS = ("claude-code", "hermes")  # built in; lanes.json may define any other harness
HOST_ENV = "JEV_LANE_HOST"
_HOST_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# The Claude Code subagents the installer writes (claude/agents/jev-lane-*.md) carry the same
# model and effort in their frontmatter, so `Agent(subagent_type="jev-lane-small")` is the lane.
TARGETS: Dict[str, Dict[str, Dict[str, str]]] = {
    "claude-code": {
        "small": {"agent": "jev-lane-small", "model": "haiku", "effort": "low"},
        "medium": {"agent": "jev-lane-medium", "model": "sonnet", "effort": "medium"},
        "high": {"agent": "jev-lane-high", "model": "opus", "effort": "medium"},
        "escalate": {"agent": "jev-lane-escalate", "model": "opus", "effort": "high"},
    },
    # Hermes Kanban tasks carry a per-task model_override and reasoning_effort, so a lane is two
    # fields on the card. Calibrated on one fleet's 2,357 real tasks (docs/lanes.md): the smaller
    # model matched the default on the cards it was given with ~40% fewer tokens; raising effort up
    # front cost ~1.8x tokens with no first-try gain, even on the cards Jev called hard; and low
    # effort cost more, not less (more turns). So `small` changes the model, `high` keeps the
    # default until a fleet's own shadow rows show effort paying for itself, and only `escalate`
    # (reached on evidence, never up front by habit) buys the strongest model. Override in lanes.json.
    "hermes": {
        "small": {"provider": "openai-codex", "model": "gpt-5.6-luna", "effort": "medium"},
        "medium": {"provider": "openai-codex", "model": "gpt-6-sol", "effort": "medium"},
        "high": {"provider": "openai-codex", "model": "gpt-6-sol", "effort": "medium"},
        "escalate": {"provider": "openai-codex", "model": "gpt-6-astra", "effort": "high"},
    },
}

# Paths whose change is security-sensitive whatever the diff says. Matched on the path only.
SENSITIVE = re.compile(
    r"(^|/)(\.env[^/]*|.*secret[^/]*|.*credential[^/]*|.*passw[^/]*|.*token[^/]*|.*keychain[^/]*|"
    r"auth[^/]*|.*oauth[^/]*|.*permission[^/]*|.*acl[^/]*|.*crypt[^/]*|.*payment[^/]*|.*billing[^/]*|"
    r"migrations?|.*\.pem|.*\.key|id_rsa[^/]*|keystore[^/]*|key_setup[^/]*|install\.py|sudoers)(/|$)",
    re.IGNORECASE,
)
TAIL_LINES = 15
TAIL_CHARS = 1_200
CHECK_TIMEOUT = 900


# ── lanes and targets ─────────────────────────────────────────────────────────

def _config_files() -> List[Path]:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    files = [Path(base) / "jev" / "lanes.json"]
    shared = paths.config_dir(shared=True) / "lanes.json"
    return files + ([shared] if shared not in files else [])


def _local_tables() -> List[Dict[str, Any]]:
    """Each readable lanes.json, least specific first. An unreadable file is ignored, never fatal."""
    out = []
    for path in _config_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def hosts() -> List[str]:
    """The built-in harnesses, then every other one a lanes.json defines completely.

    A harness lanes.json adds (Codex, OpenCode, Gemini CLI, a home-grown loop) must name a
    model for all four lanes: a lane with no model would be an instruction nobody can follow.
    """
    found = list(HOSTS)
    merged: Dict[str, Dict[str, Any]] = {}
    for data in _local_tables():
        for host, table in data.items():
            if host in HOSTS or not isinstance(host, str) or not _HOST_NAME.fullmatch(host) \
                    or not isinstance(table, dict):
                continue
            for lane, spec in table.items():
                if lane in LANES and isinstance(spec, dict):
                    merged.setdefault(host, {}).setdefault(lane, {}).update(spec)
    for host, table in sorted(merged.items()):
        if all(isinstance((table.get(lane) or {}).get("model"), str) and table[lane]["model"].strip()
               for lane in LANES):
            found.append(host)
    return found


def default_host() -> str:
    """``JEV_LANE_HOST`` when it names a known harness, else Claude Code (the historical default)."""
    pinned = (os.environ.get(HOST_ENV) or "").strip()
    return pinned if pinned and pinned in hosts() else "claude-code"


def targets(host: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """The lane -> model/effort map for a host, with any local ``lanes.json`` laid on top.

    ``lanes.json`` is ``{"hermes": {"small": {"model": "...", "effort": "low"}}}``; a lane or
    field it does not name keeps the default. A host that is not built in exists only through
    lanes.json, and only once all four of its lanes name a model (see ``hosts``).
    """
    host = host or default_host()
    if host not in hosts():
        raise ValueError(f"host must be one of {', '.join(hosts())}")
    out = {lane: dict(spec) for lane, spec in TARGETS.get(host, {}).items()}
    for lane in LANES:
        out.setdefault(lane, {})
        if host != "hermes":
            # Every subagent harness gets the same file names (install.py writes them), so a
            # lane names the agent to delegate to whatever the harness is.
            out[lane].setdefault("agent", f"jev-lane-{lane}")
    for data in _local_tables():
        for lane, spec in (data.get(host) or {}).items() if isinstance(data.get(host), dict) else ():
            if lane in out and isinstance(spec, dict):
                out[lane].update({str(k): str(v) for k, v in spec.items() if isinstance(v, (str, int, float))})
    return out


def next_lane(lane: str) -> Optional[str]:
    """One step up the ladder, or None at the top (the next step is a person)."""
    if lane not in LANES:
        raise ValueError(f"lane must be one of {', '.join(LANES)}")
    index = LANES.index(lane)
    return LANES[index + 1] if index + 1 < len(LANES) else None


# ── deterministic evidence ───────────────────────────────────────────────────

def tail(text: str, lines: int = TAIL_LINES, chars: int = TAIL_CHARS) -> str:
    """The end of a long output, where the verdict of a test run or build usually is."""
    kept = "\n".join((text or "").rstrip().splitlines()[-lines:])
    return kept[-chars:]


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def changed_files(repo: Path, base: str = "HEAD") -> List[str]:
    """Tracked changes against ``base`` plus untracked files, as repo-relative paths."""
    names = set(filter(None, _git(repo, "diff", "--name-only", base).splitlines()))
    names |= set(filter(None, _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()))
    return sorted(names)


def in_scope(path: str, scope: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*").rstrip("/") + "/")
               or path == pattern for pattern in scope)


def run_check(command: str, repo: Path, timeout: float = CHECK_TIMEOUT) -> Dict[str, Any]:
    """Run one check the caller named (a test runner, compiler, type checker or linter)."""
    started = time.monotonic()
    try:
        done = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True, timeout=timeout)
        code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as error:
        code = 124
        output = f"{error.stdout or ''}{error.stderr or ''}\n[timed out after {timeout:.0f}s]"
    except OSError as error:
        code, output = 127, str(error)
    return {"command": command[:200], "exit": code, "passed": code == 0,
            "seconds": round(time.monotonic() - started, 1), "tail": tail(output if isinstance(output, str) else "")}


def evidence(repo: Any = ".", runs: Sequence[str] = (), scope: Sequence[str] = (), base: str = "HEAD",
             expects_changes: bool = True, checks_available: Optional[bool] = None,
             timeout: float = CHECK_TIMEOUT) -> Dict[str, Any]:
    """Everything the toolchain can say about one cycle, as ``facts`` (never sent) and ``state``.

    ``facts`` feed the loop-step pre-rules; ``state`` is the short, redacted view Jev reads when
    judgment is still needed: a diff stat and the tail of each check, not whole logs.
    """
    root = Path(repo).expanduser().resolve()
    files = changed_files(root, base)
    stat = _git(root, "diff", "--stat", base).strip()
    untracked = sorted(filter(None, _git(root, "ls-files", "--others", "--exclude-standard").splitlines()))
    if untracked:
        stat += ("\n" if stat else "") + "\n".join(f" {name} (new file)" for name in untracked[:30])
    checks = [run_check(command, root, timeout) for command in runs]
    outside = [name for name in files if scope and not in_scope(name, scope)]
    sensitive = [name for name in files if SENSITIVE.search(name)]
    failed = [check for check in checks if not check["passed"]]
    facts = {
        "checks_run": bool(checks), "checks_available": bool(checks) if checks_available is None else checks_available,
        "checks_failed": len(failed), "out_of_scope_files": len(outside), "diff_empty": not files,
        "expects_changes": bool(expects_changes), "security_changed": bool(sensitive),
        "files_changed": len(files),
    }
    state = {
        "diff_stat": tail(stat, 40, 1_500) if stat else "(no changes)",
        "checks": "\n\n".join(f"$ {c['command']}\nexit {c['exit']}\n{c['tail']}" for c in checks) or "(no checks run)",
    }
    return {"facts": facts, "state": state, "checks": checks, "files": files,
            "out_of_scope": outside, "sensitive": sensitive}


# ── the two decisions ────────────────────────────────────────────────────────

def classify(task: str, *, context: str = "", facts: Optional[Mapping[str, Any]] = None,
             host: Optional[str] = None, mode: str = "live", timeout: float = 4.0,
             transport: Any = None, record: bool = True) -> Dict[str, Any]:
    """The first lane for a piece of work: one Jev request, three questions, code applies the rules."""
    host = host or default_host()
    state = {"task": task, **({"context": context} if context else {})}
    decision = engine.decide(state, "lane", mode=mode, facts=dict(facts or {}), timeout=timeout,
                             transport=transport, record=record)
    lane = decision["action"]
    out = {"lane": lane, "target": targets(host).get(lane), "host": host, "decision": decision}
    if lane == "keep_current":
        out["target"] = None
        out["why"] = ("Jev could not judge; keep the model you were going to use" if decision.get("fallback_used")
                      else "keep the model already chosen" if decision.get("source") == "code"
                      else "Jev read this as work a person should look at first; keep the current model")
    return out


def _complete_supported(facts: Mapping[str, Any]) -> Optional[str]:
    """Why the deterministic facts forbid calling this complete, or None when they allow it."""
    if int(facts.get("checks_failed") or 0) > 0:
        return "a check is failing"
    if int(facts.get("out_of_scope_files") or 0) > 0:
        return "files outside the requested scope changed"
    if facts.get("checks_available") and not facts.get("checks_run"):
        return "relevant checks exist and were not run"
    if facts.get("expects_changes") and facts.get("diff_empty"):
        return "nothing changed"
    return None


def step(task: str, *, lane: str, attempts: int = 1, facts: Optional[Mapping[str, Any]] = None,
         state: Optional[Mapping[str, Any]] = None, notes: str = "", host: Optional[str] = None,
         same_failure_repeated: bool = False, mode: str = "live", timeout: float = 4.0,
         transport: Any = None, record: bool = True) -> Dict[str, Any]:
    """After one implementation cycle: continue / retry / verify / escalate / complete.

    ``facts`` and ``state`` normally come from ``evidence``. The answer names the lane to run
    next: the same lane, one lane up on escalate, or ``person`` when escalate is already the top.
    """
    host = host or default_host()
    if lane not in LANES:
        raise ValueError(f"lane must be one of {', '.join(LANES)}")
    known = {**dict(facts or {}), "lane": lane, "attempts": int(attempts),
             "same_failure_repeated": bool(same_failure_repeated)}
    view = {"task": task, **dict(state or {}), **({"notes": notes} if notes else {})}
    decision = engine.decide(view, "loop-step", mode=mode, facts=known, timeout=timeout,
                             transport=transport, record=record)
    action = decision["action"]
    refused = None
    if action == "complete":
        refused = _complete_supported(known)
        if refused:
            action = "verify"
    run_lane: Optional[str] = lane
    if action == "escalate":
        run_lane = next_lane(lane)
    out: Dict[str, Any] = {"action": action, "lane": run_lane or "person",
                           "target": targets(host).get(run_lane) if run_lane else None,
                           "source": decision.get("source"), "decision": decision}
    if refused:
        out["complete_refused"] = refused
    if action == "escalate" and run_lane is None:
        out["why"] = "already in the top lane: take it to a person with the evidence"
    return out
