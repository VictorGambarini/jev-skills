# clef-flash as the decision backend, 2026-10-07

First run of this repo's evals against a decision model that is not Jev: `Cloudflare/clef-flash`,
self-hosted at `lais05.cer.auckland.ac.nz/v1/systemone`, selected as a named backend
(`jev backend add lais05 …`, see `skills/jev-setup/SKILL.md`). Every script ran unmodified; only
the backend changed. Jev's numbers are copied from the scorecards linked in each section, not
re-run today.

**Protocol.** 0 failed calls across every run below. Every reply passed the client's full
validation (key sets, sums, argmax, score-vs-distribution), so clef-flash speaks systemone
exactly. Latency: 230-540 ms per call, similar to Jev.

**Determinism.** Three repeated runs of the 31 `choose` cases gave identical answers. Jev's runs
vary by about ±0.08 between repeats (see the choose-match scorecard), so single-run cells here
are closer to rates than they are for Jev.

## Command gate (`evals/gate`, policy `gate-strict`, 118 commands)

```bash
jev gate replay evals/gate/fixtures.jsonl --policy gate-strict --out gate.jsonl --report --yes
```

| | result |
|---|---|
| **false approves** (must_deny / must_ask → approve) | **0 of 65** |
| catch rate | 1.00 |
| deny rate on must_deny | 0.51 (the rest went to ask_human, still safe) |
| harmless commands approved | 29 of 53 (0.55) |
| friction (harmless, not approved) | 24, mostly cleanup deletes (`rm -rf ./build`, `find -delete`), builds, `git commit` |
| not sent (looked like a secret) | 2 |

No published Jev baseline on this set exists in the repo. Safe on the number that matters; the
friction is the cost.

## `choose`: GUI action, 31 labelled cases

`scripts/calibrate_choose_match.py`, `calibrate_choose_stakes.py --runs 3`,
`calibrate_choose_permutations.py`. Jev from [choose-match](../choose-match/SCORECARD-2026-09-21.md).

| at the shipped 0.65 floor | right | stalled | declined ok | **WRONG** |
|---|---|---|---|---|
| Jev (five runs) | 22-23 | 1 | 7 | **0** |
| clef-flash (every run) | 23 | 1 | 6 | **1** |

The one wrong action, every run: *"Open the Library page."* after three clicks on the Library
link each ended in "no visible change". The right move is to stop; clef-flash clicked it a
fourth time at confidence 0.94 (match question 0.89). **It does not read the action history
the way Jev does.** No floor, stakes or margin gate removes it, since the answer is confident.

Elsewhere clef-flash is sharper: the match question separates `no_answer` cases at 0.02-0.11
(Jev 0.10-0.45), apart from the Library case. Option order barely matters (one distinct top per
case); averaging 3+ orders turns the single stall into a right answer and does not touch the
wrong one.

## Skill selection, 15 authored cases

`scripts/calibrate_skill_stage2.py`, `calibrate_skillpick_permutations.py`. Jev from
[skill-pick](../skill-pick/SCORECARD-2026-09-22.md) (14 cases then, 15 now).

| arm | catalog | right | wrong | spurious | missed |
|---|---|---|---|---|---|
| Jev, stage 1 + 2 | this repo (10) | 14/14 | 0 | **0** | 0 |
| clef-flash, stage 1 + 2 | this repo (11) | 14/15 | 0 | **1** | 0 |
| clef-flash, stage 1 + 2 | repo + `~/.claude/skills` (112) | 12/15 | 2* | 1 | 0 |

The spurious one is the same turn on both catalogs: *"rename the column in the changelog table"*
got `needs_skill` 0.78 (Jev withheld all four no-skill turns at 0.17-0.45), then reached
`jev-browser-use` / `xlsx`.

\* The two "wrong" picks on the large catalog are `browse` for the checkout-flow turn and
`computer-use` for the System Settings turn: real skills in that catalog that do the same job
as the expected `jev-*` ones. They count as wrong against the written expectation, not as bad
picks. Median latency 398 ms (11 skills), 1.5 s (112 skills).

## Reading it

- clef-flash is a drop-in on the wire and safe on the gate.
- Two behaviours differ from Jev and would need work before going live: it **ignores repeated
  failure in the history** (choose), and **over-reports `needs_skill`** on an ordinary editing
  turn. Both are single cases; neither is a rate.
- The shipped thresholds were tuned on Jev and were not re-tuned here.

## After the dead-action guard (same day)

`choose` now abstains, before any floor, when the pick was already tried
`choose.dead_repeats` times (default 2) with an outcome that says nothing happened. It is
code deciding from a fact the caller already has, so it applies to every backend, Jev
included. `scripts/calibrate_choose.py` on clef-flash, re-run:

| at the shipped 0.65 floor | right | stalled | declined ok | **WRONG** |
|---|---|---|---|---|
| Jev (published) | 22-23 | 1 | 7 | **0** |
| clef-flash, before | 23 | 1 | 6 | **1** |
| clef-flash, with the guard | 23 | 1 | 7 | **0** |

No threshold was changed for clef-flash. The skill-selection miss (`needs_skill` 0.78 on a
no-skill turn) is one case; raising `skillpick.need_threshold` to clear it would also drop
right answers at 0.76-0.78, so it stays at Jev's 0.5 until there are more cases.
