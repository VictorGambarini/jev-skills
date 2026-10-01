# Operational follow-up: 2026-10-01

This is a sanitized aggregate from one private fleet's scheduled 24-hour observation
window ending on 2026-10-01. It is not a cross-install benchmark or a quality claim.
No prompts, decision logs, profile identifiers, credentials or machine paths are included.

## What usage tells us

- Skill selection: 44 suggestions shown, 18 subsequently loaded; 6 repeats avoided,
  1 suggestion suppressed, and 10 fail-open records. Loading is correlation, not proof
  that the suggestion caused the load or improved the work.
- Web screening: 25 results screened, no flags or fail-open records, median 158 ms.
  A quiet sample with unknown attack prevalence establishes neither recall nor safety.
- Routing: 81 shadow decisions, no would-switch or applied switches. These are shadow
  observations, not demonstrated savings or a reason to change an owner's live mode.

## Changes motivated by the evidence

The old skill log recorded `status: fail_open` but not its cause. Retrospective inspection
therefore cannot distinguish intentional privacy/catalog skips from timeouts or invalid
answers. The plugin now logs only a closed, bounded `reason_code`; arbitrary error text
stays out of the log. Collect a fresh window before changing thresholds or budgets.

PR #32 fixes effort below a resolved routing tier and guards large contexts even when
models have no catalog price. Integration review reproduced an additional path where a
large-context keep lost a risk-based effort floor; `effort_tier` now preserves that floor
without initiating a switch or escalation.

Issue #31's denied-symlink path now uses a checkout-pinned shell launcher rather than a
broken copy of `bin/jev`. Profile plugin/skill directories fall back to copies. Owned
launchers can be reinstalled or uninstalled; foreign or modified launchers stay untouched.
Git Bash is required for the shell CLI on Windows. The dedicated Windows workflow tests
Python 3.10 and 3.13 with symlink denial injected; it is not proof that every Windows
installer or native PowerShell workflow is supported.

## Boundaries that remain

Nous catalog refresh is authenticated only on explicit refresh, on its exact HTTPS
host allowlist, with redirects refused. Cached per-turn reads stay offline. OpenRouter
fallback prices are estimates, not Nous billing quotes.

Issue #25 stays open. Existing public transfer receipts and the frozen held-out protocol
remain the source of truth; no paid replay, threshold tuning or new attack-recall claim
was performed in this update. Use `evals/web-screen/HELDOUT.md` before proposing another
measurement, and report missing rows, fail-open denominators and clean false positives.

The README's star-history chart is a live public GitHub-star visualization, not a
performance chart. Its image endpoint was fetched and parsed as SVG during this update.
