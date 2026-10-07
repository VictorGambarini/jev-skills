"""Named decision backends: any server that answers the systemone protocol, not only Jev.

The built-in providers (TypeSafe, OpenRouter, Venice, Zen) all serve the same Jev model and
keep their own key handling in ``keystore``. A backend here is anything else that speaks the
same wire protocol — a self-hosted decision model, a gateway, a local mock — named once in
``~/.config/jev/backends.json`` and selected by ``JEV_BACKEND`` or the file's ``default``::

    {"default": "lais05",
     "backends": {"lais05": {"protocol": "systemone",
                             "url": "https://lais05.example/v1/systemone",
                             "model": "Cloudflare/clef"}}}

A backend's key is its own: it is read from ``key_env`` (``JEV_BACKEND_<NAME>_API_KEY`` by
default), the OS secret store or a 0600 file under that backend's name, and it is only ever
sent to that backend's own URL. A provider key (``TYPESAFE_API_KEY`` and friends) is never
sent to a backend, for the same reason ``client.PROXY_KEY_ENV`` exists.

``protocol`` names how the request and reply are shaped. Only ``systemone`` exists today;
a backend that answers some other way (an OpenAI-compatible server with logprobs, say) is a
new protocol adapter in ``client``, with the same validated answers coming out of it.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from . import tuning as tuning_mod

BACKEND_ENV = "JEV_BACKEND"
CONFIG_ENV = "JEV_BACKENDS"
PROTOCOLS = ("systemone",)
# Names the built-in providers already use: a backend may not shadow one, or `JEV_BACKEND=zen`
# would mean two different things depending on whether a file exists.
RESERVED = ("typesafe", "openrouter", "venice", "zen", "custom", "absent", "default", "none")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_KEY_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Plain path segments only, as in client._PATH_PREFIX: the URL that is validated is the URL
# that is requested.
_PATH = re.compile(r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._~!$&'()*+,;=:@-]+)+/?")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


class BackendError(ValueError):
    """A backends.json entry, or a request for one, that cannot be used as written."""


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    model: str
    protocol: str = "systemone"
    key_env: str = ""
    tuning: Tuple[Tuple[str, float], ...] = ()   # see tuning.KNOBS; kept hashable

    @property
    def key_variable(self) -> str:
        return self.key_env or default_key_env(self.name)

    def public(self) -> Dict[str, Any]:
        """What `jev backend list` and `jev doctor` show. No key, ever."""
        out = asdict(self)
        out["key_env"] = self.key_variable
        out["tuning"] = dict(self.tuning)
        return out


def default_key_env(name: str) -> str:
    return "JEV_BACKEND_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY"


def config_path() -> Path:
    explicit = (os.environ.get(CONFIG_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jev" / "backends.json"


def check_url(url: str) -> str:
    """https anywhere, http only on loopback; no credentials, query or fragment in the URL."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        raise BackendError(f"not a usable URL: {url!r}") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or port == 0:
        raise BackendError("the URL must be http(s)://host/path")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
        raise BackendError("plain http is allowed only on 127.0.0.1, ::1 or localhost; use https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BackendError("the URL may not carry a user, password, query or fragment")
    if not _PATH.fullmatch(parsed.path or ""):
        raise BackendError("the URL needs the full endpoint path, like https://host/v1/systemone")
    return url.strip().rstrip("/")


def _parse(name: str, raw: Any) -> Backend:
    if not _NAME.fullmatch(name) or name in RESERVED:
        raise BackendError(f"backend name {name!r}: use lowercase letters, digits and dashes, "
                           f"and not one of {', '.join(RESERVED)}")
    if not isinstance(raw, Mapping):
        raise BackendError(f"backend {name!r} must be an object")
    protocol = raw.get("protocol", "systemone")
    if protocol not in PROTOCOLS:
        raise BackendError(f"backend {name!r}: protocol must be one of {', '.join(PROTOCOLS)}")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise BackendError(f"backend {name!r} needs a model id")
    key_env = raw.get("key_env") or ""
    if key_env and (not isinstance(key_env, str) or not _KEY_ENV.fullmatch(key_env)):
        raise BackendError(f"backend {name!r}: key_env must be an UPPER_CASE variable name")
    raw_tuning = raw.get("tuning") or {}
    if not isinstance(raw_tuning, Mapping):
        raise BackendError(f"backend {name!r}: tuning must be an object")
    try:
        tuned = tuning_mod.check(raw_tuning)
    except ValueError as error:
        raise BackendError(f"backend {name!r}: {error}") from None
    return Backend(name=name, url=check_url(str(raw.get("url") or "")), model=model.strip(),
                   protocol=protocol, key_env=key_env, tuning=tuple(sorted(tuned.items())))


def load_file(path: Optional[Path] = None) -> Dict[str, Any]:
    """The raw file, or an empty one. A file that is not JSON is an error, not an empty file."""
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"default": None, "backends": {}}
    except (OSError, json.JSONDecodeError) as error:
        raise BackendError(f"{path} could not be read: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("backends", {}), dict):
        raise BackendError(f"{path} must be an object with a \"backends\" object")
    data.setdefault("backends", {})
    data.setdefault("default", None)
    return data


def load(path: Optional[Path] = None) -> Dict[str, Backend]:
    return {name: _parse(name, raw) for name, raw in load_file(path)["backends"].items()}


def get(name: str) -> Backend:
    backends = load()
    if name not in backends:
        raise BackendError(f"no backend named {name!r} in {config_path()}")
    return backends[name]


def active() -> Optional[Backend]:
    """The backend decisions go to, or None for the built-in provider order.

    ``JEV_BACKEND`` wins over the file's default. ``JEV_BACKEND=default`` (or ``none``) means
    the built-in providers even when the file names a default. A selection that names a
    backend that does not exist is an error rather than a silent fall back to a provider:
    the person asked for their own model, and quietly asking Jev instead would hide that.
    """
    pinned = (os.environ.get(BACKEND_ENV) or "").strip()
    if pinned in ("default", "none"):
        return None
    if pinned in ("typesafe", "openrouter", "venice", "zen"):
        return None  # a built-in provider by name: client and keystore handle it
    data = load_file()
    name = pinned or data.get("default")
    if not name:
        return None
    if name not in data["backends"]:
        raise BackendError(f"backend {name!r} is selected but not defined in {config_path()}")
    return _parse(name, data["backends"][name])


def save(data: Mapping[str, Any], path: Optional[Path] = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def add(name: str, url: str, model: str, protocol: str = "systemone", key_env: str = "",
        make_default: bool = False) -> Backend:
    data = load_file()
    entry: Dict[str, Any] = {"protocol": protocol, "url": url, "model": model}
    if key_env:
        entry["key_env"] = key_env
    previous = data["backends"].get(name) or {}
    if isinstance(previous, Mapping) and previous.get("tuning"):
        entry["tuning"] = previous["tuning"]  # re-adding to change the URL or model keeps the tuning
    backend = _parse(name, entry)  # validate before anything is written
    data["backends"][name] = {k: v for k, v in asdict(backend).items() if k not in ("name", "tuning") and v}
    if backend.tuning:
        data["backends"][name]["tuning"] = dict(backend.tuning)
    if make_default or not data.get("default"):
        data["default"] = name
    save(data)
    return backend


def remove(name: str) -> bool:
    data = load_file()
    if name not in data["backends"]:
        return False
    del data["backends"][name]
    if data.get("default") == name:
        data["default"] = None
    save(data)
    return True


def tune(name: str, changes: Mapping[str, Optional[float]]) -> Backend:
    """Set (or, with None, clear) tuning keys on one backend; validated before it is written."""
    data = load_file()
    if name not in data["backends"]:
        raise BackendError(f"no backend named {name!r} in {config_path()}")
    entry = dict(data["backends"][name])
    current = dict(entry.get("tuning") or {})
    for key, value in changes.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    if current:
        entry["tuning"] = current
    else:
        entry.pop("tuning", None)
    backend = _parse(name, entry)
    data["backends"][name] = entry
    save(data)
    return backend


def policy_dir(name: str) -> Path:
    """Where a backend's own copies of policies live; they win while it is the active backend."""
    from . import paths  # noqa: PLC0415
    return paths.config_dir(shared=True) / "backends" / name / "policies"


def use(name: Optional[str]) -> None:
    """Make ``name`` the default, or clear the default with None."""
    data = load_file()
    if name is not None and name not in data["backends"]:
        raise BackendError(f"no backend named {name!r} in {config_path()}")
    data["default"] = name
    save(data)
