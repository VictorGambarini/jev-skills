"""Thresholds that belong to the decision model, so each backend can carry its own.

Every number here was measured on Jev (jev-1.13.0) and is the default. A named backend
(``backends.json``) may set any of them under ``"tuning"``, because another model's
confidences are not the same numbers: a 0.65 floor that is right for Jev can be too loose or
too tight for clef-flash. A backend sets a knob only once its own eval says what it should be,
and ``jev backend tune`` refuses a key that is not listed here or a value outside its range.

Policy files are tuned the same way, per backend, by a copy in
``<config>/backends/<name>/policies/`` (see ``policy.locate``).
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional, Tuple

# key -> (Jev default, lowest allowed, highest allowed, what it gates)
KNOBS: Dict[str, Tuple[float, float, float, str]] = {
    "choose.min_confidence": (0.65, 0.5, 0.99, "act on a GUI/browser pick only at or above this confidence"),
    "choose.dead_repeats": (2, 1, 10, "abstain when the pick was already tried this many times with no visible effect"),
    "skillpick.need_threshold": (0.5, 0.0, 1.0, "suggest a skill only when needs_skill is at least this"),
    "skillpick.match_threshold": (0.5, 0.0, 1.0, "a finalist skill must verify at least this"),
    "skillpick.shortlist_floor": (0.02, 0.0, 0.5, "stage-1 probability a skill needs to reach the shortlist"),
    "search.sufficiency_threshold": (0.5, 0.0, 1.0, "stop searching when the evidence answers the question this surely"),
    "webscreen.injection_threshold": (0.5, 0.0, 1.0, "withhold a web chunk scored at least this likely to hold instructions"),
}


def check(raw: Mapping[str, object]) -> Dict[str, float]:
    """A backend's tuning map, validated, or ``ValueError`` naming the first problem."""
    out: Dict[str, float] = {}
    for key, value in raw.items():
        if key not in KNOBS:
            raise ValueError(f"unknown tuning key {key!r}; known: {', '.join(sorted(KNOBS))}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"tuning {key} must be a number")
        low, high = KNOBS[key][1], KNOBS[key][2]
        if not low <= float(value) <= high:
            raise ValueError(f"tuning {key} must be between {low} and {high}")
        out[key] = float(value)
    return out


def value(key: str, default: Optional[float] = None) -> float:
    """The active backend's value for ``key``, else ``default``, else the Jev default.

    Never raises: a backends.json that cannot be read is the client's to report (every call
    fails open as ``backend_misconfigured``); a threshold lookup only falls back.
    """
    fallback = KNOBS[key][0] if default is None else default
    try:
        from . import backends  # noqa: PLC0415 - backends imports this module's KNOBS
        chosen = backends.active()
    except Exception:  # noqa: BLE001
        return fallback
    if chosen is None:
        return fallback
    return dict(chosen.tuning).get(key, fallback)
