"""Tests for the effort floor and the large-context guard on catalog-unknown refs.

Three behaviours a live fleet measured as real failures, pinned so they cannot regress:

1. Routing's tier is probability MASS; the effort pick reads the ARGMAX bucket. The two
   can disagree (tier=medium, argmax=trivial). Without a floor the turn bought the
   cheapest budget routing did not ask for — observed as a tool-needing turn answering
   in one line with zero tool calls.
2. A kept decision (tier None) with low confidence must not buy the cheapest bucket
   either: "unsure is not hard" cuts both ways.
3. `omniproxy:*`-style refs are absent from the models.dev catalog, so they carry no
   price, so the large-context guard's price comparison never ran and every big-context
   turn still switched models. A switch re-reads the whole history on a cold cache.

Also pins the cache-key boundary: the guard in decide() is strict (`>`), so the cache
key's notion of "over the guard" must be strict too, or a cached sub-guard SWITCH leaks
to an over-guard turn inside the same context bucket.
"""
import unittest
from typing import Any, cast

from jevkit import effort, route


def difficulty(bucket, confidence=0.95):
    """A spread whose argmax is `bucket`, with a matching averaged score."""
    spread = {str(i): (1.0 if i == bucket else 0.0) for i in range(4)}
    return {"difficulty": {"type": "score", "score": float(bucket),
                           "confidence": confidence, "probabilities": spread}}


LEVELS = ("low", "medium", "high", "max")


class TierFloorTests(unittest.TestCase):
    def test_medium_tier_floors_a_trivial_argmax(self):
        """The regression that broke a live turn: tier=medium, argmax=trivial."""
        decision = {"tier": "medium", "answers": difficulty(0)}
        floor = effort.min_bucket_for(decision)
        self.assertEqual(floor, 1)
        self.assertEqual(effort.pick(decision["answers"], levels=LEVELS, min_bucket=floor),
                         "medium")

    def test_hard_tier_floors_a_routine_argmax(self):
        decision = {"tier": "hard", "answers": difficulty(1)}
        floor = effort.min_bucket_for(decision)
        self.assertEqual(floor, 2)
        self.assertEqual(effort.pick(decision["answers"], levels=LEVELS, min_bucket=floor),
                         "high")

    def test_simple_tier_does_not_raise_a_confident_trivial_turn(self):
        """The cheap tier must stay cheap, or the whole pool saves nothing."""
        decision = {"tier": "simple", "answers": difficulty(0, confidence=0.97)}
        floor = effort.min_bucket_for(decision)
        self.assertEqual(floor, 0)
        self.assertEqual(effort.pick(decision["answers"], levels=LEVELS, min_bucket=floor),
                         "low")

    def test_a_harder_argmax_still_wins_over_the_floor(self):
        """The floor raises; it must never lower a genuinely hard turn."""
        decision = {"tier": "medium", "answers": difficulty(3)}
        floor = effort.min_bucket_for(decision)
        self.assertEqual(effort.pick(decision["answers"], levels=LEVELS, min_bucket=floor),
                         "max")

    def test_tier_floor_is_zero_for_unknown_tiers(self):
        for tier in (None, "", "medium-ish", 3, {"tier": "hard"}):
            self.assertEqual(effort.tier_floor(tier), 0, f"tier={tier!r}")


class DoubtFloorTests(unittest.TestCase):
    def test_a_kept_unsure_decision_does_not_buy_the_cheapest_bucket(self):
        decision = {"tier": None, "answers": difficulty(0, confidence=0.40)}
        self.assertEqual(effort.min_bucket_for(decision, {"min_confidence": 0.6}), 1)

    def test_a_kept_confident_trivial_decision_stays_cheap(self):
        decision = {"tier": None, "answers": difficulty(0, confidence=0.95)}
        self.assertEqual(effort.min_bucket_for(decision, {"min_confidence": 0.6}), 0)

    def test_risk_floor_survives_a_large_context_keep(self):
        config = {**route.DEFAULT_CONFIG, "tiers": {"medium": {"general": ["relay:other"]}}}
        answers = {"difficulty": {"score": 0.0, "confidence": 0.95, "probabilities": {0: 1.0}},
                   "kind": {"choice": "general", "confidence": 0.95},
                   "costly_mistake": {"noul": 0.0}}
        decision = route.decide("review the production migration", current="relay:current",
                                context_tokens=60000, config=config, rows=[], answers=answers)
        self.assertFalse(decision["routed"])
        self.assertEqual(effort.min_bucket_for(decision, config), 1)

    def test_the_threshold_comes_from_config(self):
        decision = {"tier": None, "answers": difficulty(0, confidence=0.70)}
        self.assertEqual(effort.min_bucket_for(decision, {"min_confidence": 0.6}), 0)
        self.assertEqual(effort.min_bucket_for(decision, {"min_confidence": 0.8}), 1)

    def test_a_tiered_decision_ignores_confidence(self):
        """A routed tier already floored the pick; doubt must not double-count."""
        decision = {"tier": "hard", "answers": difficulty(0, confidence=0.10)}
        self.assertEqual(effort.min_bucket_for(decision, {"min_confidence": 0.6}), 2)

    def test_missing_confidence_fails_open_to_no_floor(self):
        decision = {"tier": None, "answers": {"difficulty": {"score": 0.0}}}
        self.assertEqual(effort.min_bucket_for(decision), 0)

    def test_a_malformed_threshold_falls_back_to_the_default(self):
        decision = {"tier": None, "answers": difficulty(0, confidence=0.40)}
        self.assertEqual(effort.min_bucket_for(decision, {"min_confidence": "wide"}), 1)

    def test_a_non_mapping_decision_floors_nothing(self):
        self.assertEqual(effort.min_bucket_for(None), 0)
        self.assertEqual(effort.min_bucket_for(cast(Any, "medium")), 0)

    def test_min_bucket_rejects_garbage_and_still_picks(self):
        """A malformed floor must not lose the turn's effort."""
        self.assertEqual(effort.pick(difficulty(2), levels=LEVELS, min_bucket=cast(Any, "high")),
                         "high")


class PickFloorClampTests(unittest.TestCase):
    def test_a_floor_cannot_push_past_the_rubric(self):
        self.assertEqual(effort.pick(difficulty(0), levels=LEVELS, min_bucket=9), "max")

    def test_a_negative_floor_is_clamped_to_zero(self):
        self.assertEqual(effort.pick(difficulty(0), levels=LEVELS, min_bucket=-3), "low")


class StickyGuardTests(unittest.TestCase):
    """`_pick` + the guard, on a config whose refs no catalog knows."""

    CONFIG = {
        "tiers": {
            "simple": {"general": ["omniproxy:cheap-model"]},
            "medium": {"general": ["omniproxy:mid-model"]},
            "hard": {"general": ["omniproxy:main-model"]},
        },
        "exclude": [],
        "escalation": {"enabled": False, "rungs": []},
        "sticky_context_tokens": 50_000,
        "min_confidence": 0.6,
        "simple_needs_confidence": 0.85,
        "simple_needs_probability": 0.7,
        "hard_needs_probability": 0.6,
        "cache_repeat_asks": False,
        "mode": "redacted-text",
    }

    def test_pick_still_returns_a_catalog_unknown_ref(self):
        got = route._pick(self.CONFIG, {}, "simple", "general", False, 500, "omniproxy")
        self.assertEqual(got, "omniproxy:cheap-model")

    def test_the_guard_keeps_the_model_when_neither_side_has_a_price(self):
        """The live failure: no catalog rows => no prices => the guard used to skip."""
        current = "omniproxy:main-model"
        picked = "omniproxy:cheap-model"
        by_ref = {}                       # exactly what a relay-only fleet looks like
        context = self.CONFIG["sticky_context_tokens"] + 1
        kept = route._large_context_verdict(current, picked, context, by_ref, self.CONFIG,
                                            answers=difficulty(0))
        self.assertIsNotNone(kept)
        self.assertFalse(kept["routed"])
        self.assertEqual(kept["model"], current)
        self.assertIn("catalog-unknown", kept["reason"])

    def test_a_small_context_is_not_guarded(self):
        kept = route._large_context_verdict("omniproxy:main-model", "omniproxy:cheap-model",
                                            500, {}, self.CONFIG, answers=difficulty(0))
        self.assertIsNone(kept)

    def test_exactly_at_the_threshold_is_not_guarded(self):
        """decide() guards on `>`, so the boundary itself still switches."""
        kept = route._large_context_verdict("omniproxy:main-model", "omniproxy:cheap-model",
                                            50_000, {}, self.CONFIG, answers=difficulty(0))
        self.assertIsNone(kept)

    def test_the_original_priced_guard_still_runs_first(self):
        """A cheaper pick with known prices keeps the old behaviour, not the new reason."""
        by_ref = {"omniproxy:main-model": {"price": 5.0},
                  "omniproxy:cheap-model": {"price": 1.0}}
        kept = route._large_context_verdict("omniproxy:main-model", "omniproxy:cheap-model",
                                            190_000, by_ref, self.CONFIG, answers=difficulty(0))
        self.assertIsNotNone(kept)
        self.assertIn("cost more than it saves", kept["reason"])

    def test_a_priced_upgrade_is_allowed_through(self):
        by_ref = {"omniproxy:main-model": {"price": 1.0},
                  "omniproxy:cheap-model": {"price": 5.0}}
        kept = route._large_context_verdict("omniproxy:main-model", "omniproxy:cheap-model",
                                            190_000, by_ref, self.CONFIG, answers=difficulty(0))
        self.assertIsNone(kept)

    def test_the_same_model_is_never_guarded(self):
        kept = route._large_context_verdict("omniproxy:main-model", "omniproxy:main-model",
                                            190_000, {}, self.CONFIG, answers=difficulty(0))
        self.assertIsNone(kept)


class CacheKeyBoundaryTests(unittest.TestCase):
    def test_the_sticky_side_matches_the_guards_strict_comparison(self):
        cfg = {"sticky_context_tokens": 50_000}
        self.assertFalse(route._sticky_side(49_999, cfg))
        self.assertFalse(route._sticky_side(50_000, cfg))   # `>` in decide(): not over
        self.assertTrue(route._sticky_side(50_001, cfg))
        self.assertTrue(route._sticky_side(190_000, cfg))

    def test_the_default_threshold_is_used_when_config_lacks_one(self):
        default = route.DEFAULT_CONFIG["sticky_context_tokens"]
        self.assertFalse(route._sticky_side(default, {}))
        self.assertTrue(route._sticky_side(default + 1, {}))

    def test_a_malformed_threshold_falls_back_instead_of_raising(self):
        self.assertTrue(route._sticky_side(10**9, {"sticky_context_tokens": "lots"}))

    def test_the_cache_key_separates_the_two_sides_of_one_bucket(self):
        """The regression: 49,999 and 50,001 share the 64,000 context bucket."""
        cfg = {"sticky_context_tokens": 50_000, "tiers": {}, "exclude": []}
        a = route._cache_key("hello", "default", "omniproxy", False, False, 49_999, cfg)
        b = route._cache_key("hello", "default", "omniproxy", False, False, 50_001, cfg)
        self.assertNotEqual(a, b)
        self.assertEqual(route._context_bucket(49_999), route._context_bucket(50_001))

    def test_the_cache_key_still_groups_far_apart_sizes_on_one_side(self):
        """The cache must stay useful: two large contexts share a key."""
        cfg = {"sticky_context_tokens": 50_000, "tiers": {}, "exclude": []}
        a = route._cache_key("hello", "default", "omniproxy", False, False, 190_000, cfg)
        b = route._cache_key("hello", "default", "omniproxy", False, False, 191_000, cfg)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
