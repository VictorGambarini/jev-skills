# Compaction eval

Does letting Jev mark each turn keep / drop (it was keep / summarize / drop when the scorecard was measured) make a better handoff than not
doing that? This measures it on your own sessions. The result on ours is in
[results/](results/).

The method is Nous Research's, from
[hermes-agent PR 116246](https://github.com/NousResearch/hermes-agent/pull/116246), scaled
down to one dependency-free file. That PR tested a different Jev compaction design and
found its ranking tied plain recency, which is why this repo stopped taking its own
compaction claim on faith.

## How it works

For each exported session:

1. A model reads the whole transcript, tool output included, and writes a 15-question
   recall exam with short gold answers: exact identifiers, decisions, root causes,
   constraints, unfinished work, spread across the first, middle and last third.
2. An oracle answers the exam with the whole transcript in view. A question the oracle
   misses is a bad question, and recall is scored only on the ones it gets right.
3. Every **arm** turns the session into the text a writer model sees, and the same writer
   produces a handoff capsule from it.
4. A fresh model answers the exam from the capsule alone (**closed-book**), then again with
   one keyword search of the old session allowed per question (**recovery**), which is what
   a real next session can do with `session_search`.
5. A different model grades each answer against the gold: identifiers must match exactly.

| arm | what the writer sees |
|---|---|
| `plugin_fallback` | the last N characters of the dialogue, every line tagged `[background]`. What the plugin does when Jev is unavailable |
| `tail_plain` | the same text, with a prompt that asks for exact values and mentions no markers. The fair free baseline |
| `jev` | `compact.select` then `compact.digest`. What shipped through 0.13.2 |
| `recency_matched` | Jev's keep / drop **counts**, assigned by recency instead of by Jev. Same budget, same mechanism, no judgement: the gap between this and `jev` is what Jev's judgement is worth |
| `regex_keep` | keep any turn holding an identifier-shaped string. Free |
| `failopen` | every turn kept whole. What a Jev outage gives |
| `*_v2` | the same selections through an alternative digest (keep lines first, identifiers swept from clipped text). It measured worse and was not shipped; it lives only in the eval |
| `full` | the whole dialogue. The ceiling for a capsule of this size |
| `none` | no capsule, search only |

Every arm sees user and assistant text only, because that is what the handoff plugin
exports. The exam is written from everything, so facts that live only in tool output are
counted as the loss they are.

## Running it

```bash
mkdir -p ~/jev-eval/transcripts
hermes sessions export --session-id <id> --format jsonl --redact - > ~/jev-eval/transcripts/one.jsonl
python3 evals/compaction/run_eval.py --transcripts ~/jev-eval/transcripts --out ~/jev-eval/run1
```

Needs an OpenRouter key in the environment or the OS secret store, and a TypeSafe key for
the `jev` arms. Seven sessions of 250 to 600 rows cost about two dollars. Everything is
cached per transcript, so a second run with `--words 1200` or `--anchors 900` only pays for
what changed.

`--out` must be outside this repo and the script refuses otherwise. Exams, capsules and
answers are real session content. Only the scorecard, which holds counts and nothing else,
is safe to publish.

## Reading the result honestly

Fifteen questions per transcript is a blunt instrument. Two arms that share one code path
and differ only in prompt wording came out 8 points apart on 104 questions, so treat
anything under about 10 points as a tie. One model writes the exam and grades it. Gold
answers are not checked by a person. The search in the recovery pass is a plain BM25 over
turns, which is kinder than a real tool that searches every session you have.
