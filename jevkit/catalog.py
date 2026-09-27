"""Every model this machine can actually call, with price, context and abilities.

The source is the public models.dev catalog (Hermes keeps a copy; otherwise it is
fetched and cached for a day). A provider counts as *available* when one of the
API-key names it declares is set in the environment or in a Hermes ``.env``, or
when Hermes holds a login for it. Only names are read, never values. Nous Portal, which
models.dev lacks, is added from the Nous API's own /models (see ``_with_nous``).
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_TTL = 24 * 3600

# Hermes login names → models.dev provider ids, where they differ.
HERMES_ALIASES = {
    "xai-oauth": "xai", "gemini": "google", "kimi-coding": "kimi-for-coding", "openai-codex": "openai",
    "zai": "zai", "copilot": "github-copilot", "minimax": "minimax", "moonshot": "moonshotai",
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def hermes_root() -> Path:
    """The shared Hermes folder. A profile's home is <root>/profiles/<name>."""
    home = hermes_home()
    return home.parent.parent if home.parent.name == "profiles" else home


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "jev" / "models_dev.json"


def load_models_dev(refresh: bool = False) -> Dict[str, Any]:
    return _with_nous(_load_models_dev(refresh), refresh)


def _load_models_dev(refresh: bool) -> Dict[str, Any]:
    candidates = [hermes_home() / "models_dev_cache.json", hermes_root() / "models_dev_cache.json", _cache_path()]
    if not refresh:
        fresh = [p for p in candidates if p.is_file() and time.time() - p.stat().st_mtime < CACHE_TTL]
        for path in fresh or [p for p in candidates if p.is_file()]:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    request = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "hermes-jev-skills"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https URL
        raw = response.read(60_000_000)
    data = json.loads(raw)
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


# ── Nous Portal ──────────────────────────────────────────────────────────────
# models.dev has no Nous entry, but the Nous inference API serves an OpenRouter-shaped
# /models with its own prices, read with the Hermes Nous login (sent to NOUS_HOSTS only). It
# is fetched only on refresh (routing reads this catalog on every turn and must not touch the
# network); otherwise the saved copy is used, and failing that Hermes's cached Nous model
# list priced from models.dev's OpenRouter entry.

NOUS_ID = "nous"
# The only hosts the Hermes Nous login is ever sent to. auth.json is not ours; a base URL in it
# pointing anywhere else (or carrying userinfo, a port, a query) is not followed.
NOUS_HOSTS = frozenset({"inference-api.nousresearch.com"})


def _nous_cache_path() -> Path:
    return _cache_path().parent / "nous_models.json"


def _hermes_nous_login() -> Dict[str, Any]:
    try:
        path = hermes_home() / "auth.json"
        data = json.loads((path if path.is_file() else hermes_root() / "auth.json").read_text(encoding="utf-8"))
        login = (data.get("providers") or {}).get(NOUS_ID)
        return login if isinstance(login, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _nous_models_url(base: str) -> Optional[str]:
    """``base``/models if ``base`` is plain https on a known Nous host, else None."""
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or "@" in parts.netloc or parts.hostname not in NOUS_HOSTS
            or port not in (None, 443) or parts.query or parts.fragment):
        return None
    return base.rstrip("/") + "/models"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer to wherever it points; refuse it (it surfaces as an HTTPError)."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _open_nous(request: urllib.request.Request) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=20)  # noqa: S310 - host checked


def _fetch_nous() -> Optional[List[Dict[str, Any]]]:
    login = _hermes_nous_login()
    url = _nous_models_url(str(login.get("inference_base_url") or ""))
    tokens = [login.get(name) for name in ("agent_key", "access_token") if login.get(name)]
    if url is None:
        return None
    for token in tokens:
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "User-Agent": "hermes-jev-skills"})
        try:
            with _open_nous(request) as response:
                rows = json.loads(response.read(20_000_000)).get("data")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(rows, list) and rows:
            path = _nous_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows), encoding="utf-8")
            return rows
    return None


def _nous_spec(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One Nous /models row in models.dev's shape. Prices there are dollars per token."""
    pricing = row.get("pricing") or {}
    try:
        cost = {"input": float(pricing["prompt"]) * 1e6, "output": float(pricing["completion"]) * 1e6}
    except (KeyError, TypeError, ValueError):
        return None
    if cost["input"] < 0 or cost["output"] < 0:       # OpenRouter marks variable-priced routers with -1
        return None
    architecture = row.get("architecture") or {}
    params = row.get("supported_parameters") or []
    created = row.get("created")
    return {
        "name": row.get("name") or row.get("id"), "cost": cost,
        "limit": {"context": int(row.get("context_length") or 0)},
        "modalities": {"input": architecture.get("input_modalities") or ["text"],
                       "output": architecture.get("output_modalities") or ["text"]},
        "tool_call": "tools" in params, "reasoning": "reasoning" in params,
        "release_date": time.strftime("%Y-%m-%d", time.gmtime(created)) if isinstance(created, (int, float)) else "",
    }


def _nous_models(catalog: Dict[str, Any], refresh: bool) -> Dict[str, Any]:
    rows = _fetch_nous() if refresh else None
    if rows is None:
        try:
            rows = json.loads(_nous_cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows = None
    if isinstance(rows, list):
        specs = {row["id"]: _nous_spec(row) for row in rows if isinstance(row, dict) and row.get("id")}
        return {model: spec for model, spec in specs.items() if spec and not model.endswith(":batch")}
    # No Nous copy yet: Hermes's model list, priced as OpenRouter prices the same ids.
    try:
        listed = json.loads((hermes_home() / "provider_models_cache.json").read_text(encoding="utf-8"))
        names = (listed.get(NOUS_ID) or {}).get("models") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    openrouter = (catalog.get("openrouter") or {}).get("models") or {}
    return {name: openrouter[name] for name in names if name in openrouter}


def _with_nous(catalog: Dict[str, Any], refresh: bool) -> Dict[str, Any]:
    if NOUS_ID in catalog:                            # models.dev lists Nous itself one day: trust it
        return catalog
    found = _nous_models(catalog, refresh)
    if found:
        catalog[NOUS_ID] = {"id": NOUS_ID, "name": "Nous Portal", "env": [], "models": found}
    return catalog


def _env_names() -> Set[str]:
    names = {name for name, value in os.environ.items() if value}
    for path in {hermes_home() / ".env", hermes_root() / ".env"}:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name, sep, value = line.partition("=")
                if sep and value.strip() and name.strip().isidentifier():
                    names.add(name.strip())
        except OSError:
            continue
    return names


def _hermes_logins() -> Set[str]:
    try:
        path = hermes_home() / "auth.json"
        data = json.loads((path if path.is_file() else hermes_root() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    found: Set[str] = set()
    for section in ("credential_pool", "providers"):
        block = data.get(section)
        if isinstance(block, dict):
            found.update(str(name) for name in block)
    return {HERMES_ALIASES.get(name, name) for name in found}


def available_providers(catalog: Dict[str, Any], extra: Iterable[str] = ()) -> List[str]:
    env = _env_names()
    logins = _hermes_logins()
    out = []
    for provider_id, provider in catalog.items():
        declared = provider.get("env") or []
        if provider_id in logins or provider_id in extra or any(name in env for name in declared):
            out.append(provider_id)
    return sorted(out)


def _blended(cost: Dict[str, Any]) -> Optional[float]:
    """Dollars per million tokens for a typical agent turn (input-heavy)."""
    try:
        return 0.8 * float(cost["input"]) + 0.2 * float(cost["output"])
    except (KeyError, TypeError, ValueError):
        return None


def models(
    catalog: Optional[Dict[str, Any]] = None, providers: Optional[Iterable[str]] = None,
    require_tools: bool = True,
) -> List[Dict[str, Any]]:
    """Flat list of callable text models, cheapest first."""
    catalog = catalog if catalog is not None else load_models_dev()
    wanted = set(providers) if providers is not None else set(available_providers(catalog))
    rows: List[Dict[str, Any]] = []
    for provider_id in wanted:
        provider = catalog.get(provider_id) or {}
        for model_id, spec in (provider.get("models") or {}).items():
            if not isinstance(spec, dict):
                continue
            modalities = spec.get("modalities") or {}
            if "text" not in (modalities.get("output") or ["text"]):
                continue
            if require_tools and not spec.get("tool_call"):
                continue
            price = _blended(spec.get("cost") or {})
            if price is None:
                continue
            if spec.get("status") in ("deprecated", "retired"):
                continue
            limit = spec.get("limit") or {}
            rows.append({
                "provider": provider_id, "model": model_id, "name": spec.get("name") or model_id,
                "price": round(price, 4), "input": (spec.get("cost") or {}).get("input"),
                "output": (spec.get("cost") or {}).get("output"), "context": int(limit.get("context") or 0),
                "vision": "image" in (modalities.get("input") or []), "reasoning": bool(spec.get("reasoning")),
                "released": spec.get("release_date") or "",
            })
    rows.sort(key=lambda row: (row["price"], row["provider"], row["model"]))
    return rows
