# Handoffs and compaction on Hermes: what we measured, and what ships

Short version: a handoff is a short note plus a way back to the session it came from. The way back matters more than the note, and Jev does not improve either. Numbers are in the [scorecard](../evals/compaction/results/SCORECARD-2026-09-20.md); you can rerun them on your own sessions with [evals/compaction](../evals/compaction/README.md).

## What the `hermes-handoff` plugin does

Say "handoff" or run `/wrapup`. On a worker thread it:

1. exports the session with the stable CLI (`hermes sessions export --session-id <id> --format jsonl -`) and keeps the user and assistant text;
2. gives the writer the **whole dialogue**, up to 300,000 characters, with a prompt asking for five headings (Working on, State, Decisions, Pointers, Next) in up to **1,200 words**. The writer is the host's own `compression` auxiliary model, so there is no new credential and no new bill line;
3. appends a **Recovery** section no model writes: the session id, the message count, and the two `session_search` calls that work;
4. stores the capsule per lane and injects it once into the next session in that lane.

| | recall alone | with one search |
|---|---|---|
| through 0.13.2: Jev digest of the last 24,000 characters, 400 words | 37.5% | 68.3% |
| **0.14.0: whole dialogue, 1,200 words, Recovery section** | **58.7%** | **75.0%** |

Question by question that is 26 won and 4 lost.

A profile marked confidential (a `CONFIDENTIAL` file in its handoffs folder, or `HANDOFF_CONFIDENTIAL=1`) gets a 400-word breadcrumb with no identifiers and **no** Recovery section, because a pointer into a transcript full of customer detail is what that contract exists to prevent.

## The Jev pre-pass is off by default

`jevkit.compact.select` marks each turn keep or drop and `compact.digest` turns that into a reduced transcript. (It used to mark keep / summarize / drop; the measurement below is of that version.) It was on whenever a key was present. Measured, a capsule written from that digest recalled less than one written from the plain text of the same size (4 questions won, 15 lost), and tied once a search was allowed. Jev's marks were better than marks by recency (11 to 4), so the fault was the digest, which clipped every summarize turn to 400 characters, and not Jev's judgement. The summarize fate is therefore gone: Jev is asked only keep or drop, a drop needs confidence 0.7 or more, and everything else (low-confidence drops, unjudged turns, Jev down, sensitive turns never sent) is kept. A kept turn is never clipped. If the digest is still over its size cap (24,000 characters by default), the oldest kept turns are left out whole, with a note saying how many; dropped turns were already gone, so there is nothing else to cut first. No model summarises anything here, so it adds no latency or cost.

`HANDOFF_JEV=1` turns it back on. It is still the better way to choose turns when a size is fixed: a small local writer, or a hook like this one on `session:compress`:

```python
import sys
sys.path.insert(0, str(hermes_root / "plugins" / "hermes-jev"))
from jevkit import compact

recent = messages[-240:]                       # 40 turns per Jev request, one after another
selection = compact.select(recent, keep_last=8, timeout=10)
if selection["status"] == "ok":               # "partial" means some batches failed: treat it like fail_open
    body, marked = compact.digest(recent, selection, limit=24000), True
else:
    body, marked = plain_tail(messages), False  # on fail_open the digest is worse than the plain text
prompt = compact.handoff_prompt(body, marked=marked)
```

If your writer can read the whole dialogue, skip all of that and send it the whole dialogue.

## Recovery: the two calls, exactly

```
session_search(query="2 to 5 keywords")                        -> session_id + match_message_id
session_search(session_id="<id>", around_message_id=<match>)  -> the messages around the hit
```

`role_filter="user,assistant,tool"` searches tool output too, which is where 48 of our 104 exam answers lived. Do **not** pass `query` and `session_id` together: on Hermes 0.21 that reads the session from the top and ignores the query, even though the built-in compressor's own footer suggests it.

## Not planned: replacing the built-in compressor

Hermes accepts a replacement `ContextEngine` through `ctx.register_context_engine`. Nous evaluated a Jev-driven one and rejected it ([PR 116246](https://github.com/NousResearch/hermes-agent/pull/116246)): it retained twice the context for no more recall, compacted more and more often, and got stuck on a text-heavy session. Nothing we measured argues with that.
