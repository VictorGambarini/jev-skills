"""Where Jev keeps its state, whatever agent harness it runs under.

Under Hermes the answer is Hermes's own layout, unchanged: a profile's home is
``HERMES_HOME`` (``<root>/profiles/<name>`` or the root itself), shared settings live in
``<root>/jev``, a profile's own in ``<home>/jev``, and logs in ``<home>/logs``.

Without Hermes (Claude Code, Codex, a plain shell) there is no profile and no shared root,
so every one of those collapses to one XDG location: settings in ``$XDG_CONFIG_HOME/jev``
and logs in ``$XDG_STATE_HOME/jev/logs``. Before this module each feature resolved
``~/.hermes`` for itself and created it on machines that have never run Hermes.

``JEV_HOME`` overrides both: settings in ``$JEV_HOME`` and logs in ``$JEV_HOME/logs``.
"""
from __future__ import annotations

import os
from pathlib import Path

JEV_HOME_ENV = "JEV_HOME"


def hermes_home() -> Path:
    """The Hermes home this process belongs to, whether or not it exists."""
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def hermes_root() -> Path:
    """The shared Hermes folder. A profile's home is <root>/profiles/<name>."""
    home = hermes_home()
    return home.parent.parent if home.parent.name == "profiles" else home


def uses_hermes() -> bool:
    """True when this process runs under Hermes, or Hermes is installed for this user.

    ``JEV_HOME`` wins over both: someone who set it asked for one place, not two.
    """
    if (os.environ.get(JEV_HOME_ENV) or "").strip():
        return False
    return bool((os.environ.get("HERMES_HOME") or "").strip()) or hermes_home().is_dir()


def _jev_home() -> Path | None:
    value = (os.environ.get(JEV_HOME_ENV) or "").strip()
    return Path(value).expanduser() if value else None


def config_dir(shared: bool = True) -> Path:
    """Settings: ``<root>/jev`` (shared) or ``<home>/jev`` (this profile) under Hermes."""
    explicit = _jev_home()
    if explicit is not None:
        return explicit
    if uses_hermes():
        return (hermes_root() if shared else hermes_home()) / "jev"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jev"


def logs_dir(shared: bool = False) -> Path:
    """Logs: ``<home>/logs`` (this profile) or ``<root>/logs`` (shared) under Hermes."""
    explicit = _jev_home()
    if explicit is not None:
        return explicit / "logs"
    if uses_hermes():
        return (hermes_root() if shared else hermes_home()) / "logs"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "jev" / "logs"


def profile() -> str:
    """The Hermes profile name, or ``default`` (also for every non-Hermes harness)."""
    if not uses_hermes():
        return "default"
    home = hermes_home()
    return home.name if home.parent.name == "profiles" else "default"
