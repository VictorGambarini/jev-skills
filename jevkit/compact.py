"""Compaction and handoffs: Jev marks what survives, a text model only writes it up.

Jev cannot summarize. What it can do, in one fast request per 40 turns, is mark each turn
keep or drop, nothing in between. It judges a long turn on its first and last 350
characters, redacted, and sees no other turn while it does. When in doubt a turn is kept,
and a kept turn is kept whole: there is no clipped middle fate and no summariser.

What that is worth was measured (evals/compaction, seven real sessions, 104 questions):
Jev's marks beat the same number of marks handed out by recency, 11 questions to 4, so the
judgement is real. But a handoff written from the digest built on those marks recalled less
than one written from the plain tail of the same size, 4 to 15. The clipping (the old
"summarize" fate) cost more than the judgement earned, so it is gone. Use `select` to
choose turns when a budget forces a choice; do
not expect it to improve a handoff, and see `handoff.recovery_block` for what did.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

TURN_CHARS = 700
BATCH = 40
# Counting turns is not enough, the same lesson rerank.py learned first: the client measures
# the JSON-ENCODED state, and one CJK character encodes to six. A 40-turn Japanese batch
# came to 120,601 characters against a 60,000 limit, so every batch failed as
# state_too_large while the result still said "ok" (reported as issue #3). Leave room for
# the questions, which repeat the two fate descriptions for every turn.
STATE_BUDGET = 40_000
_PER_TURN_OVERHEAD = 16
# Dropping is the only irreversible fate, so it needs a confident answer; anything less is kept.
DROP_CONFIDENCE = 0.7
FATE = {
    "keep": "Carries a decision, a constraint, a user preference, an unfinished task, an exact value, path, id, "
            "command or error that later work depends on, or is background whose gist later work needs",
    "drop": "Chatter, acknowledgements, superseded attempts, repeated output, or detail nothing later depends on",
}


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    # A tool-call-only assistant row has content None. str(None) is "None": four characters
    # that were sent to Jev, paid for, judged, and then written into the digest as a turn.
    return content if isinstance(content, str) else ("" if content is None else str(content))


def _pack(messages: Sequence[Mapping[str, Any]], judged: Sequence[int]) -> List[List[int]]:
    """Group turn indexes into requests that fit, by encoded size and not by count."""
    batches: List[List[int]] = []
    current: List[int] = []
    used = 0
    for index in judged:
        cost = len(json.dumps(privacy.redact(_text(messages[index]), TURN_CHARS))) + _PER_TURN_OVERHEAD
        if current and (len(current) >= BATCH or used + cost > STATE_BUDGET):
            batches.append(current)
            current, used = [], 0
        current.append(index)
        used += cost
    if current:
        batches.append(current)
    return batches


def select(
    messages: Sequence[Mapping[str, Any]], *, keep_last: int = 6, timeout: float = 8.0,
    transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    """Return a fate for every message index. The last ``keep_last`` are always kept."""
    total = len(messages)
    fates: Dict[int, str] = {index: "keep" for index in range(max(0, total - keep_last), total)}
    judged = [index for index in range(total) if index not in fates]
    for index in judged:
        fates[index] = "keep"                 # the fail-open default: nothing is dropped unless Jev said so
    judged = [i for i in judged if messages[i].get("role") != "system" and _text(messages[i]).strip()]

    calls, errors, latency, judged_ok = 0, [], 0, set()
    for group in _pack(messages, judged):
        sendable = [i for i in group if not privacy.is_sensitive(_text(messages[i]))]
        if not sendable:
            continue
        state = {"turns": {f"T{i}": f"{messages[i].get('role', 'user')}: {privacy.redact(_text(messages[i]), TURN_CHARS)}"
                           for i in sendable}}
        questions = {f"t{i}": client.choice(f"For continuing this work later, what should happen to turn T{i}?", FATE)
                     for i in sendable}
        try:
            reply = client.ask(state, questions, timeout=timeout, transport=transport)
        except client.JevError as error:
            errors.append(error.code)
            continue
        calls += 1
        latency += reply["latency_ms"]
        judged_ok.update(sendable)
        for i in sendable:
            answer = reply["answers"][f"t{i}"]
            if answer["choice"] == "drop" and answer["confidence"] >= DROP_CONFIDENCE:
                fates[i] = "drop"

    counts = {fate: sum(1 for value in fates.values() if value == fate) for fate in FATE}
    # "ok" used to mean "at least one batch worked", so one good batch hid every failed one
    # and forty turns sat at the keep default while the caller was told all was well.
    # The compaction skill tells an agent to use the plain transcript on anything but ok.
    if not judged:
        status = "ok"
    elif not calls:
        status = "fail_open"
    else:
        status = "partial" if errors else "ok"
    return {"status": status, "fates": {str(k): v for k, v in sorted(fates.items())},
            "counts": counts, "jev_calls": calls, "errors": errors, "latency_ms": latency,
            "unjudged": sorted(i for i in judged if i not in judged_ok)}


def digest(messages: Sequence[Mapping[str, Any]], selection: Mapping[str, Any], limit: int = 24000) -> str:
    """The reduced transcript to hand to the writing model: every turn not dropped, whole.

    A kept turn is never clipped. If the result is still over ``limit``, the oldest kept
    turns are left out whole (dropped turns were already gone) and a note at the top says
    how many, instead of cutting any turn mid-sentence.
    """
    fates = selection["fates"]
    lines: List[str] = []
    for index, message in enumerate(messages):
        if fates.get(str(index), "keep") == "drop":
            continue
        body = _text(message).strip()
        if not body:
            continue
        lines.append(f"[KEEP VERBATIM] {message.get('role', 'user')}: {body}")
    text = "\n\n".join(lines)
    if len(text) <= limit:
        return text
    left, used = 0, 0
    for line in reversed(lines):
        if used + len(line) + 2 > limit - 120 and used:
            break
        used += len(line) + 2
        left += 1
    omitted = len(lines) - left
    note = f"[note] {omitted} earlier kept turn(s) did not fit and are not shown."
    return "\n\n".join([note] + lines[len(lines) - left:])


# The five headings a handoff needs. Fewer and the next session starts by rediscovering what
# was already decided.
HANDOFF_SECTIONS = ("Working on", "State", "Decisions", "Pointers", "Next")

# The budget was 400 words, on the theory that anything longer reads like an essay. Measured
# (evals/compaction): with the writer reading the whole dialogue, 1,200 words answered 58.7%
# of a recall exam closed-book against 46.2% at 400, 16 questions won and 3 lost. The writer
# used about 820 of them. A confidential capsule stays at 400: it is a breadcrumb on purpose.
HANDOFF_WORDS = 1200
CONFIDENTIAL_WORDS = 400

_PROMPT = """Write a handoff so a fresh session can pick this work up cold.

Use exactly these five headings, in this order, nothing before or after:
## Working on
## State
## Decisions
## Pointers
## Next

Rules:
- Under {words} words total.
{carry}- Pointers means exact paths, ids, URLs, ports, branch names, commands. No prose there.
- Next means what the following session should actually do first, concretely.
- Write nothing you cannot support from the transcript below. No guessing, no filler,
  no "the user seems to want". If something is unknown, say it is unknown.
- Plain sentences. No bullets inside a section unless listing pointers.
"""

_CARRY_MARKED = (
    "- Every line marked [KEEP VERBATIM] was judged worth carrying: a decision, a constraint, an exact\n"
    "  value, a path, an id, a command, an error, or background later work needs. Carry decisions,\n"
    "  values and identifiers through UNCHANGED. Do not paraphrase them.\n")
# With no marks there is nothing to tell the writer about marks. This is the wording the
# eval's winning arm used, so what ships is what was measured.
_CARRY_PLAIN = (
    "- Carry every decision, constraint, exact value, path, id, command and error string through\n"
    "  UNCHANGED. Do not paraphrase them.\n")

HANDOFF_PROMPT = _PROMPT.format(words=HANDOFF_WORDS, carry=_CARRY_MARKED)



CONFIDENTIAL_RULES = """
CONFIDENTIALITY — these override the rules above wherever they conflict:
- This handoff is a BREADCRUMB, not a summary. Record only: the task, which classes of
  source were checked, what evidence is still missing, who owns or must approve it, and
  the next safe action.
- Never carry a person's name, an email address, a phone number, a street address, a
  document or file name, a message id, a link, an account or invoice number, or any
  payment, health or disability detail. Write "the customer", "the staff member", "the
  quote in the shared drive" instead.
- A business location IS an operational fact, not personal data. Keep a site, branch,
  city or depot by name when the work depends on it — "ship to Fort Walton, not Destin"
  must survive verbatim. Dropping it to "the approved location" destroys the only thing
  that sentence was for. The line is: a place a business operates from, yes; a place a
  person lives, no.
- This REPLACES the instruction to keep identifiers verbatim. Where a [KEEP VERBATIM]
  line contains an identifier of that kind, carry the decision it expresses and drop the
  identifier. "Call Jane Doe on 555-0134 before Tuesday" becomes "call the customer back
  before Tuesday" — the deadline survives, the person does not.
- Internal technical pointers — file paths on our own servers, branch names, commands,
  ports, error strings — are fine and should be kept.
- If following these rules would leave a section with nothing to say, write "nothing
  recorded" under it rather than reaching for detail you are not allowed to keep.
"""


def redact_capsule(text: str, limit: int = 6000) -> str:
    """Mechanical backstop over a written capsule: emails, phones, tokens, long ids.

    The prompt above is the real control, because only the writer knows that "Doe"
    is a customer. This catches the shapes a regex *can* be sure about, so a writer that
    ignores its instructions still cannot leave a phone number on disk. Both layers
    exist because neither is sufficient: one is reliable but blind, the other sees but
    can be disobeyed.
    """
    return _GUID.sub("[id]", privacy.redact(text, limit=limit))


_GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def handoff_prompt(digest_text: str, previous: str = "", *, confidential: bool = False,
                   marked: bool = True, words: Optional[int] = None) -> str:
    """The full prompt for whatever text model writes the capsule. Jev cannot write it.

    ``marked`` says whether ``digest_text`` carries [KEEP VERBATIM] tags. A
    plain transcript must be sent with ``marked=False``: the marked prompt tells the writer
    every line was judged, which is the wrong thing to say about a transcript nobody has
    filtered.
    """
    budget = int(words) if words else (CONFIDENTIAL_WORDS if confidential else HANDOFF_WORDS)
    prompt = _PROMPT.format(words=max(100, budget), carry=_CARRY_MARKED if marked else _CARRY_PLAIN)
    prompt += CONFIDENTIAL_RULES if confidential else ""
    if previous.strip():
        # Under confidentiality this sentence used to end "Do not lose identifiers" - appended
        # AFTER the rules that forbid carrying them, so the last instruction the writer read
        # contradicted the contract. A previous capsule is also the likeliest place for an
        # old identifier to be hiding, which makes it the worst place to say that.
        keep = ("Do not lose identifiers." if not confidential else
                "The confidentiality rules above still apply: if the previous handoff carries an identifier "
                "they forbid, do NOT carry it forward.")
        prompt += ("\nA PREVIOUS handoff for this same work is below. Carry forward anything still "
                   "true, especially Pointers, and fold in what has happened since. " + keep +
                   "\n\n<previous_handoff>\n" + previous.strip()[-12000:] + "\n</previous_handoff>\n")
    label = "TRANSCRIPT (already filtered; read the markers)" if marked else "TRANSCRIPT"
    return prompt + "\n\n" + label + ":\n\n" + digest_text + "\n"


def looks_like_capsule(text: str) -> bool:
    """A cheap check that the writer produced a handoff and not an apology or a refusal."""
    if not text or len(text.strip()) < 40:
        return False
    found = sum(1 for heading in HANDOFF_SECTIONS if f"## {heading}" in text)
    return found >= 3


def should_compact(used_tokens: int, window_tokens: int, *, soft: float = 0.6, hard: float = 0.85) -> Dict[str, Any]:
    """Pure arithmetic; no model needed to know the window is nearly full."""
    ratio = used_tokens / window_tokens if window_tokens else 0.0
    return {"ratio": round(ratio, 3), "compact": ratio >= soft, "urgent": ratio >= hard}
