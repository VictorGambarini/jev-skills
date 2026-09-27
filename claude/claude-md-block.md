<!-- hermes-jev-skills:lanes BEGIN (managed by install.py; delete this block or run install.py --uninstall to remove) -->
## Model lanes (Jev decides, checks first)

Finish with the smallest model and lowest effort that still gets it right. When delegating work to a subagent:

1. **Pick the lane** with `jev lane classify --task "<the work>"` and delegate to the agent it names: `jev-lane-small` (Haiku, low), `jev-lane-medium` (Sonnet, medium), `jev-lane-high` (Opus, medium), `jev-lane-escalate` (Opus, high). Take the lowest sufficient lane. `keep_current` means do it yourself or ask the person. Skip classifying for trivia you can do in one step yourself.
2. **Check deterministically first** after each cycle: tests, compiler, type checker, linter, `git diff`. Never ask Jev (or yourself) what the toolchain can answer. `jev lane step --task "..." --lane <lane> --attempt <n> --run "<test cmd>" --scope "<path glob>"` runs them and returns continue / retry / verify / escalate / complete.
3. **Escalate one lane at a time, only on evidence**: the same lane failed twice, checks keep failing, security-sensitive code changed, design doubt remains, or Jev is unsure. Never just because a stronger model exists.
4. **Complete only** when the behaviour is implemented, the checks pass, the diff matches the requested scope and nothing is unresolved. Report a failed check; never hide it.

Jev is a decision model only (key from `TYPESAFE_API_KEY` or `jev setup-key`; never print it). If Jev is unavailable the commands fall back to keeping the current model and verifying yourself.
<!-- hermes-jev-skills:lanes END -->
