"""The escalation ladder: which frontier seat gets hard work, and what to do when it is full.

Subscription seats are not like a metered API. They run out, they come back hours
later, and the refusal is expensive to discover — you learn a seat is full by being
turned away, after paying the latency. So two rules shape this module:

1. **A refusal is shared knowledge.** When one lane finds the top seat exhausted,
   every other lane must know immediately. The cooldown lives in a file under the
   shared home, not in a process. Forty-one lanes rediscovering the same 429 is the
   failure this prevents.
2. **A rung is skipped, never silently downgraded.** If the frontier seat is full the
   work goes to the next seat down and the decision says so out loud. Quietly serving
   hard work from a cheap model, with no one told, is the worst outcome available.

The ladder is configuration, not code. It knows nothing about Codex, Claude or any
particular vendor — the fleet names the rungs and their probes.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import catalog as catalog_mod, paths

DEFAULT_COOLDOWN = 1800.0          # a full subscription seat is usually full for a while
PROBE_CACHE_SECONDS = 900.0        # probing costs a request; 15 minutes is plenty
PROBE_TIMEOUT = 20.0


def state_path() -> Path:
    """Shared across every lane on the machine, so one refusal teaches all of them."""
    override = os.environ.get("JEV_LADDER_STATE")
    if override:
        return Path(override)
    root = catalog_mod.hermes_root()
    if paths.uses_hermes() and root.is_dir():
        return root / "jev" / "ladder.json"
    return paths.logs_dir(shared=True).parent / "ladder.json"


def _read() -> Dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: Mapping[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle, indent=2)
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp, path)
    except OSError:
        pass                        # a ladder that cannot persist still routes correctly


# ── rungs ────────────────────────────────────────────────────────────────────

def normalize(rungs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in rungs or []:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "kind": str(raw.get("kind") or "delegate"),      # "delegate" | "model"
            "model": raw.get("model"),                        # for kind == "model"
            "probe": raw.get("probe"),                        # shell command; exit 0 == available
            "cooldown": float(raw.get("cooldown") or DEFAULT_COOLDOWN),
            "why": str(raw.get("why") or ""),
            "last_resort": bool(raw.get("last_resort")),
        })
    return out


def cooling(name: str, *, now: Optional[float] = None, state: Optional[Mapping[str, Any]] = None) -> float:
    """Seconds left on this rung's cooldown; 0 when it is available to try."""
    now = now if now is not None else time.time()
    entry = ((state if state is not None else _read()).get("cooldowns") or {}).get(name) or {}
    try:
        return max(0.0, float(entry.get("until", 0)) - now)
    except (TypeError, ValueError):
        return 0.0


def refuse(name: str, reason: str, *, cooldown: float = DEFAULT_COOLDOWN, now: Optional[float] = None) -> Dict[str, Any]:
    """Record that a rung turned work away, so no other lane wastes a turn discovering it."""
    now = now if now is not None else time.time()
    data = _read()
    cooldowns = dict(data.get("cooldowns") or {})
    cooldowns[name] = {"until": now + float(cooldown), "reason": str(reason)[:300], "at": now}
    data["cooldowns"] = cooldowns
    _write(data)
    return {"rung": name, "cooling_for": round(float(cooldown)), "reason": reason}


def clear(name: Optional[str] = None) -> None:
    """Forget a cooldown — the seat came back, or a person says try again now."""
    data = _read()
    cooldowns = dict(data.get("cooldowns") or {})
    if name is None:
        cooldowns = {}
    else:
        cooldowns.pop(name, None)
    data["cooldowns"] = cooldowns
    _write(data)


def _run_probe(command: str, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        done = subprocess.run(command, shell=True, capture_output=True, timeout=timeout, check=False)
        return done.returncode == 0
    except Exception:  # noqa: BLE001 - a probe that cannot run is a rung that cannot be trusted
        return False


def probe(rung: Mapping[str, Any], *, now: Optional[float] = None, cache_seconds: float = PROBE_CACHE_SECONDS,
          runner: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
    """Is this rung usable? Cached, because asking is itself expensive."""
    now = now if now is not None else time.time()
    command = rung.get("probe")
    if not command:
        return {"available": True, "source": "no-probe"}
    data = _read()
    cached = ((data.get("probes") or {}).get(rung["name"]) or {})
    try:
        age = now - float(cached.get("at", 0))
    except (TypeError, ValueError):
        age = 1e9
    if cached and age < cache_seconds:
        return {"available": bool(cached.get("available")), "source": "cached", "age_s": round(age)}
    available = (runner or _run_probe)(str(command))
    probes = dict(data.get("probes") or {})
    probes[rung["name"]] = {"available": bool(available), "at": now}
    data["probes"] = probes
    _write(data)
    return {"available": bool(available), "source": "probed"}


def choose(
    rungs: Sequence[Mapping[str, Any]], *, now: Optional[float] = None,
    runner: Optional[Callable[[str], bool]] = None, skip_probe: bool = False,
) -> Dict[str, Any]:
    """The first rung that is neither cooling down nor probing unavailable.

    Always returns a decision. If every rung is unavailable the last-resort rung is
    used anyway and `forced` says so — refusing to do hard work at all is not a
    service, but pretending nothing was wrong is not either.
    """
    now = now if now is not None else time.time()
    order = normalize(rungs)
    if not order:
        return {"rung": None, "reason": "no ladder configured", "considered": []}
    state = _read()
    considered: List[Dict[str, Any]] = []
    for rung in order:
        left = cooling(rung["name"], now=now, state=state)
        if left > 0:
            considered.append({"rung": rung["name"], "skipped": "cooling down",
                               "available_in_s": round(left)})
            continue
        status = {"available": True, "source": "skipped"} if skip_probe else probe(rung, now=now, runner=runner)
        if not status["available"]:
            considered.append({"rung": rung["name"], "skipped": "probe says unavailable"})
            continue
        return {"rung": rung["name"], "kind": rung["kind"], "model": rung.get("model"),
                "why": rung["why"], "considered": considered, "forced": False,
                "reason": f"{rung['name']} is available" if considered else f"{rung['name']} is the first choice"}
    fallback = next((r for r in reversed(order) if r["last_resort"]), order[-1])
    return {"rung": fallback["name"], "kind": fallback["kind"], "model": fallback.get("model"),
            "why": fallback["why"], "considered": considered, "forced": True,
            "reason": "every rung was unavailable; using the last resort and saying so"}


def status(rungs: Sequence[Mapping[str, Any]], *, now: Optional[float] = None) -> Dict[str, Any]:
    """What the ladder looks like right now, for a person or a status line."""
    now = now if now is not None else time.time()
    state = _read()
    rows = []
    for rung in normalize(rungs):
        left = cooling(rung["name"], now=now, state=state)
        cached = ((state.get("probes") or {}).get(rung["name"]) or {})
        rows.append({
            "rung": rung["name"], "kind": rung["kind"], "model": rung.get("model"),
            "cooling_for_s": round(left) if left else 0,
            "last_probe": ("available" if cached.get("available") else "unavailable") if cached else "never probed",
            "reason": ((state.get("cooldowns") or {}).get(rung["name"]) or {}).get("reason", ""),
        })
    return {"rungs": rows, "state_file": str(state_path())}
