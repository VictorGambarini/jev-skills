# jev-router: a Claude Code mod

A Claude Code mod reaches inside the engine where settings hooks cannot. `jev-router` uses
four of those places, each decided by `jev` (Jev, or your own decision backend):

| Where | What it decides |
|---|---|
| `prompt.submit` (each prompt) | The one installed skill the prompt needs, if any, added as context beside it (`jev hook user-prompt`: same switch, once per skill per session) |
| `turn.step` (each model request) | The turn's **lane** sets its **effort**, and its **model** while the context is under 40k tokens |
| `session.compact` (`/compact` and auto-compaction) | The turns `jev compact-select` marks keep go into the summariser's instructions; no message is dropped |
| `tool.call` on WebFetch / WebSearch | Sentences carrying instructions aimed at an AI are withheld before the model reads them |

## Install

```text
/plugin install jev-router --marketplace VictorGambarini/jev-skills
```

Or from a local checkout, read from the folder itself so `/reload-plugins` picks up edits:

```bash
claude plugin marketplace add ~/github/jev-skills
claude plugin install jev-router@jev-skills
```

For one session only: `claude --plugin-dir ~/github/jev-skills/claude/mods/jev-router`.
It needs the `jev` command (`install.py` links it into `~/.local/bin`) and a decision backend
(`jev doctor`).

## The rules it lays over each turn

- **One classification per prompt**: `jev lane classify` on what you typed (about 0.5 s).
  Lanes map to models in `lanes.json` (default: small → Haiku low, medium → Sonnet medium,
  high → Opus medium, escalate → Opus high). Slash commands and subagents are left alone.
- **Follow-ups**: within 30 minutes of the last turn, the lane drops at most one step a turn.
  A correction ("that's wrong", "still failing") never drops it, and a second correction in a
  row climbs one. Jev's own reading wins whenever it is higher.
- **Sticky model**: above 40k tokens of context the model only moves up. The prompt cache is
  per model, so every switch re-reads the whole context uncached; worth paying once for a
  harder lane, never to save.
- **Memory**: the session's last lane and model are kept in the mod's own store, so
  `--continue`, `--resume` and restarts carry on where the session was.
- **Fails open**: no answer means the turn runs as Claude Code would have. After a failed
  call the mod stops asking for five minutes, so a backend that is down costs one timeout.
- **Skill and lane together**: the skill pick and the lane classification run side by side at submit, so a prompt waits for the slower of the two (about 1 s on a 112-skill catalog), not their sum. Your settings skill hook stands down in sessions the mod handles.
- **One screen per page**: the mod tells `jev` it screens the session, and the settings
  `PostToolUse` hook stands down there, since it sees the page before the mod withholds anything.

The status line shows each turn's decision: `jev: small · haiku-4-5 ·`, `jev: as is`.

## What it saved, so far

Two tasks, headless, same commit, decisions by Jev through OpenRouter:

| Task | Plain | With the mod |
|---|---|---|
| Change one README heading | Opus, $0.194 | Haiku, $0.076 |
| Add a `--quiet` flag with a test | Opus, $0.270 | Sonnet, $0.148 |

Both passed both ways. That shows the mechanism, not the saving on real work: the open
question is how often a cheaper lane fails where Opus would not, which a larger benchmark
with tasks that can fail has to answer.
