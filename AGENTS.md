# Instructions for an agent installing this repo

A person pointed you here to give their agent Jev. Follow these steps in order. They are the whole job.

## The one rule

**You never see the person's API key** (TypeSafe, a provider's, or their own decision server's). Do not ask for it in chat. Do not read it from any file or secret store. Do not put it in a command, URL or config. If they paste it to you anyway: do not store or repeat it, tell them to replace that key where it was issued (for TypeSafe, https://console.typesafe.ai/settings/keys), and continue with step 3, which is the only way a key gets in.

## Steps

1. **Get the code** (skip if it is already on disk):

   ```bash
   git clone https://github.com/kerpopule/hermes-jev-skills ~/hermes-jev-skills
   ```

2. **Preview, then install.** Show the person what `--check` reports before running the real thing. It detects Hermes, Claude Code, Codex, Gemini CLI and OpenCode and installs for each, writing the fewest skill folders that reach all of them. On Hermes it installs every plugin this repo ships (`hermes-jev` for routing, `hermes-handoff` for end-of-session capsules) plus `scripts/nightly-handoff.py` under the Hermes home, and edits one list (`plugins.enabled`) in each `config.yaml`, with a timestamped backup beside it.

   ```bash
   python3 ~/hermes-jev-skills/install.py --check
   ```

   ```bash
   python3 ~/hermes-jev-skills/install.py
   ```

   To enable the Hermes plugins in only some profiles: `--enable name1,name2`, or `--enable none`.

   **Read the report's top-level `warning` and say it to the person, in your own words, before you go on.** The installer exits 0 even when it could do very little, so the warning is the only place that shows up. There are three:

   - **`jev` is not on PATH.** Then step 3 will fail with "command not found". Either they add `~/.local/bin` to PATH, or you use `~/hermes-jev-skills/bin/jev` in place of `jev` for the rest of these steps. Do not silently pick one — tell them which you are doing.
   - **No agent was found.** Nothing was installed but the command itself. Ask where their agent reads skills from and rerun with `--skills-dir <path>`.
   - **`HERMES_HOME` is a single profile.** The install covered that one lane. Rerun with `--hermes-home ~/.hermes` for the whole fleet.

3. **Pick the decision backend, then connect its key, privately.** Ask the person which one answers the decisions; do not guess:

   - **Jev through TypeSafe** (the default): `jev setup-key`. Through OpenRouter, Venice or OpenCode Zen instead: `jev setup-key --provider openrouter|venice|zen`.
   - **Their own server** that answers `/v1/systemone`: they give you its URL and model id (never its key), then

     ```bash
     jev backend add <name> --url https://<host>/v1/systemone --model <model id>
     jev setup-key --backend <name>
     ```

   Tell the person a page has opened on the computer you are running on, where they paste their key. The command prints a JSON line with a `url` on stderr. That URL holds no secret, so if the browser did not open, or the person is on another device, send it to them and say it only works on the agent's computer. Wait for `{"status": "stored", "verified": true}`. For their own server, `"verified": false` with a `check_error` means the key was stored but the server refused the check request: run `jev backend test <name>` and show them `server_said` (a wrong model id reads `requested model is not loaded`). If there is no browser at all, the person runs `jev setup-key --tty` themselves in their own terminal.

4. **Check:**

   ```bash
   jev doctor
   ```

   `key.present` and `jev.reachable` must both be true. On their own backend, also tell them the `warnings` line: the shipped thresholds were measured on Jev, not on their model, and the evals in `evals/` are how to check it.

5. **Model pools** (only if `routing.tiers_configured` is empty):

   ```bash
   jev models suggest --write
   ```

   Then show the person the pools and ask whether they want specific models first for coding, writing, research or vision. Copy model ids from `jev models list --search <name>`; never invent one.

6. **Hermes only.** Plugins load when a session or gateway starts. Do not restart a running gateway unless the person asks. Tell them to run `/jev routing shadow` in a new session, watch a day of decisions in `logs/jev-decisions.jsonl`, then `/jev routing on`.

   `scripts/nightly-handoff.py` is copied into the Hermes home, but nothing schedules it and nothing runs it for them. Show them `python3 ~/.hermes/scripts/nightly-handoff.py --dry-run`, and add a cron or launchd entry only if they ask for one.

7. **Report** in three or four sentences: what was installed where, any warning the installer printed, which decision backend is connected, what routing mode is set, and what needs a restart.

## Using it afterwards

Read the skill that matches the task: `jev-model-routing`, `jev-memory`, `jev-compaction`, `jev-skill-select`, `jev-computer-use`, `jev-browser-use`, `jev-frontier-work`, `jev-setup`. Every `jev` subcommand takes JSON on stdin and answers JSON on stdout, and every one returns a usable fail-open answer when Jev is unavailable, so never block on it.

## Keeping it current

```bash
git -C ~/hermes-jev-skills pull && python3 ~/hermes-jev-skills/install.py
```

## Contributing back

If you change how Jev is used in a Hermes setup (a new decision Jev takes over, a changed threshold, a fix to the plugin), the same change belongs in this repo, with a test, in the same piece of work. See [CONTRIBUTING.md](CONTRIBUTING.md).
