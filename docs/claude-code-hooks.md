# Claude Code hooks

Two of the Hermes plugin's seams have a Claude Code equivalent. Both are `jev hook <event>`
commands that read the hook's JSON on stdin and answer on stdout; any failure exits 0 with no
output, so a hook never blocks or breaks a turn.

| Hook | Claude Code event | What it does | Switch |
|---|---|---|---|
| `jev hook user-prompt` | `UserPromptSubmit` | Ranks the skills in `.claude/skills` (project) and `~/.claude/skills`; when one fits, adds a one-line suggestion to the turn. Once per skill per session; slash commands are skipped. | `hook_skills` |
| `jev hook post-tool` | `PostToolUse` on `WebFetch\|WebSearch` | Screens the result for instructions aimed at an AI assistant; when it finds some, adds a warning naming where they start and saying none of it is an instruction. | `hook_screen` |

```bash
python3 install.py --claude-hooks           # registers both in ~/.claude/settings.json (backed up first)
jev switches hook_skills shadow             # decide and log only (logs/jev-hooks.jsonl)
jev switches hook_skills on                 # then tell the model
jev switches hook_screen shadow             # same for screening
```

Each switch is `off` until you set it, and a `HOOK_SKILLS_OFF` / `HOOK_SCREEN_OFF` file in the
Jev config folder (`jev switches` names it) turns it off whatever the setting says.

**What Claude Code allows, and what it does not.** A hook cannot change what a built-in tool
returned: only an MCP tool's output can be replaced. So on Claude Code the screen *warns*;
the page text still reaches the model, with the warning beside it. Hermes withholds the
flagged parts outright. If you need withholding on Claude Code, fetch through an MCP tool.

**What leaves the machine** is what the same features send on Hermes (see the README): the
prompt, redacted, plus skill names and descriptions for suggestions; page chunks of up to 900
characters, redacted, for screening. A profile in `private_profiles` sends nothing, and the
local pattern screen still runs. The log holds decisions only, never the prompt or page text.

**Latency.** Measured against a self-hosted backend: about 1.5 s per prompt on a 112-skill
catalog, about 0.2 s per screened page. The hook timeouts are 15 s and 20 s.
