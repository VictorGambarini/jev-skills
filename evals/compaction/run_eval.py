#!/usr/bin/env python3
"""Does Jev selection make a handoff better? Measured here, not assumed.

The compaction skill says a handoff written from Jev's keep / drop digest
"stops losing the one line that mattered". Nothing in this repo ever tested that. Nous
Research tested a different Jev compaction design against real sessions and found its
ranking tied plain recency (hermes-agent PR 116246), so the claim needs its own numbers.

The method is theirs, scaled down: for each real transcript a model writes a recall exam
with gold answers, every arm builds a capsule, a model answers the exam from the capsule
alone, and a different model grades the answers. "Recovery" repeats the exam with one
search of the old session allowed, which is what a real next session can do.

Arms (each is one way to turn a transcript into the text a writer model sees):

  plugin_fallback   what ships when Jev is unavailable: every turn tagged [background]
  tail_plain        no Jev, a prompt that asks for exact values: the fair free baseline
  jev               what ships: compact.select -> compact.digest -> handoff_prompt
  recency_matched   Jev's keep/drop COUNTS, assigned by recency instead of by
                    Jev. Isolates Jev's judgement from the mechanism around it
  regex_keep        keep any turn holding an identifier-shaped string; free
  failopen          every turn kept, whole: what Jev being down gives
  *_v2              the same selections through digest_v2 below (keep lines placed first,
                    identifier sweep on clipped background). An experiment that lost
  full              the writer reads the whole dialogue: the ceiling for a capsule
  none              no capsule at all (only meaningful with recovery)

Nothing here prints or stores a key. Transcripts, questions and capsules are real session
content: they are written to --out, which must be outside the repo, and only numbers go
into the scorecard.

    python3 evals/compaction/run_eval.py --transcripts ~/jev-eval/transcripts --out ~/jev-eval/run1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jevkit import compact, plan as _plan, privacy  # noqa: E402

EXAM_MODEL = "google/gemini-3.8-flash"
WRITER_MODEL = "z-ai/glm-5.3-flash"
ANSWER_MODEL = "z-ai/glm-5.3-flash"
JUDGE_MODEL = "google/gemini-3.8-flash"

DIGEST_CHARS = 24_000          # handoff.TRANSCRIPT_CHARS: what the shipped plugin allows
EXAM_TURN_CHARS = 6_000
EXAM_TOTAL_CHARS = 700_000
ARMS = ("plugin_fallback", "tail_plain", "jev", "recency_matched", "regex_keep", "failopen",
        "jev_v2", "recency_matched_v2", "regex_keep_v2", "failopen_v2", "full", "none")

# ── model calls ──────────────────────────────────────────────────────────────

SPEND: Dict[str, Dict[str, float]] = {}


def _credentials() -> Dict[str, str]:
    found = _plan.resolve_credentials()
    if not found["key"]:
        sys.exit("no OpenRouter key in the environment or the secret store")
    return found


def chat(model: str, system: str, user: str, *, max_tokens: int = 4000, want_json: bool = False,
         purpose: str = "other", timeout: float = 240.0) -> str:
    creds = _credentials()
    body: Dict[str, Any] = {
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "usage": {"include": True}, "reasoning": {"effort": "low"},
    }
    if want_json:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json", "authorization": "Bearer " + creds["key"],
               "x-title": "hermes-jev-skills compaction eval"}
    last = ""
    for attempt in range(5):
        try:
            request = urllib.request.Request(creds["base_url"] + "/chat/completions", data=data,
                                             headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                reply = json.loads(response.read().decode("utf-8", "replace"))
            text = (((reply.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            usage = reply.get("usage") or {}
            bucket = SPEND.setdefault(purpose, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            bucket["calls"] += 1
            bucket["input_tokens"] += int(usage.get("prompt_tokens") or 0)
            bucket["output_tokens"] += int(usage.get("completion_tokens") or 0)
            bucket["cost_usd"] += float(usage.get("cost") or 0.0)
            if text.strip():
                return text
            last = "empty reply"
        except urllib.error.HTTPError as error:
            last = f"http_{error.code}"
            if error.code not in (408, 409, 425, 429, 500, 502, 503, 504):
                break
        except Exception as error:  # noqa: BLE001 - one flaky call must not end a long run
            last = type(error).__name__
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{purpose}: {model} failed ({last})")


def chat_json(*args: Any, **kwargs: Any) -> Any:
    """`chat` whose reply has to parse. A model that returns a cut-off string gets asked again.

    One malformed reply used to end a transcript's whole run and throw away twenty minutes
    of cached work behind it. Temperature is 0, so a retry is only useful because providers
    are not as deterministic as that implies; three tries, then the error stands.
    """
    last: Optional[Exception] = None
    for _ in range(3):
        try:
            return parse_json(chat(*args, want_json=True, **kwargs))
        except ValueError as error:
            last = error
    raise RuntimeError(f"{kwargs.get('purpose', 'call')}: reply never parsed as JSON ({last})")


def parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ── transcripts ──────────────────────────────────────────────────────────────

def _said(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return content.strip() if isinstance(content, str) else ""


def _content(message: Mapping[str, Any]) -> str:
    text = _said(message)
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            fn = (call or {}).get("function") or {}
            text += f"\n[tool call {fn.get('name', '?')}({str(fn.get('arguments', ''))[:1500]})]"
    return text.strip()


def load(path: Path) -> List[Dict[str, str]]:
    """Every row, tool rows included. `dialogue()` narrows it to what the plugin exports."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    messages: List[Any] = []
    try:
        blob = json.loads(raw)
        messages = blob.get("messages", []) if isinstance(blob, dict) else blob
    except ValueError:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("messages"), list):
                messages.extend(row["messages"])
            elif isinstance(row, dict) and row.get("role"):
                messages.append(row)
    out = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") in ("user", "assistant", "tool"):
            text = _content(message)
            if text:
                out.append({"role": message["role"], "content": text, "said": _said(message)})
    return out


def dialogue(rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """handoff.export_messages keeps user and assistant turns only; so does every arm."""
    # The words only. The exam and the search see tool-call arguments too (`content`); the
    # shipped export does not, so no arm may either.
    return [{"role": r["role"], "content": r.get("said", r["content"])} for r in rows
            if r["role"] in ("user", "assistant") and r.get("said", r["content"])]


def _elide(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n[… {len(text) - limit} chars omitted …]\n" + text[-half:]


def render(rows: Sequence[Mapping[str, str]], per_turn: int, total: int) -> str:
    lines = [f"T{i} [{r['role']}]: {_elide(r['content'], per_turn)}" for i, r in enumerate(rows)]
    text = "\n\n".join(lines)
    while len(text) > total and per_turn > 600:
        per_turn //= 2
        text = "\n\n".join(f"T{i} [{r['role']}]: {_elide(r['content'], per_turn)}" for i, r in enumerate(rows))
    return text if len(text) <= total else text[-total:]


# ── the exam ─────────────────────────────────────────────────────────────────

EXAM_SYSTEM = """You write recall exams about a working session between a person and an AI agent.
The exam tests whether a HANDOFF written from this session would let a fresh session carry on
the work without re-discovering what was already established."""

EXAM_USER = """Below is a session transcript. Each turn is numbered T0, T1, ...

Write exactly {n} questions. Rules:
- Each question is about a fact the NEXT session would genuinely need in order to continue this
  work: an exact identifier (path, id, command, port, config key, branch, version, error string),
  a decision and its reason, a root cause that was found, a constraint or preference the person
  stated, or a piece of work left unfinished.
- Spread them across the WHOLE session: about a third from the first third of the turns, a third
  from the middle, a third from the last third. Do not cluster at the end.
- At least 6 of the questions must have an exact identifier as the answer.
- The gold answer must be short (25 words at most), specific, and checkable. No opinions.
- Every question must be answerable from the transcript alone and must make sense to someone who
  has not read it (name the thing you are asking about).
- Never ask about passwords, API keys, tokens or any other secret.
- "turn" is the number of the single turn that best supports the answer.

Return JSON only: {{"questions": [{{"id": "q1", "question": "...", "answer": "...",
"kind": "identifier|decision|root_cause|constraint|open_work", "turn": 12}}, ...]}}

TRANSCRIPT
{transcript}"""


def make_exam(rows: Sequence[Mapping[str, str]], n: int) -> List[Dict[str, Any]]:
    text = render(rows, EXAM_TURN_CHARS, EXAM_TOTAL_CHARS)
    reply = chat_json(EXAM_MODEL, EXAM_SYSTEM, EXAM_USER.format(n=n, transcript=text),
                      max_tokens=6000, purpose="exam")
    questions = []
    for index, item in enumerate(reply.get("questions") or []):
        if not isinstance(item, dict) or not item.get("question") or not item.get("answer"):
            continue
        turn = item.get("turn")
        turn = int(turn) if isinstance(turn, (int, float)) and 0 <= int(turn) < len(rows) else None
        questions.append({"id": f"q{index + 1}", "question": str(item["question"]), "answer": str(item["answer"]),
                          "kind": str(item.get("kind") or "other"), "turn": turn,
                          "evidence_role": rows[turn]["role"] if turn is not None else "unknown",
                          "third": (min(2, turn * 3 // max(1, len(rows))) if turn is not None else None)})
    return questions[:n]


# ── arms ─────────────────────────────────────────────────────────────────────

_IDENT = re.compile(
    r"(/[\w.@~-]+){2,}|https?://\S+|\b[0-9a-f]{7,40}\b|\b[\w.-]+\.(py|js|ts|json|ya?ml|md|sh|toml|db|log|plist)\b"
    r"|`[^`\n]{3,}`|\b(error|exception|traceback|failed|denied|refused)\b|\bport\s*\d{2,5}\b|:\d{4,5}\b|--[a-z][\w-]+",
    re.IGNORECASE)


# ── anchors: a second experiment that lost ───────────────────────────────────
# A free, regex-harvested list of the identifiers a session used, appended to the capsule
# outside its word budget. Nous credit a list like it with a large closed-book gain. Here it
# changed nothing (49.0% against 48.1%; 4 questions won, 3 lost) and, given to a session
# with no capsule, it HURT: 43.3% against 56.7% with one search, 4 won and 18 lost, because
# a list of plausible identifiers is an invitation to stop searching. It never shipped.

SWEEP_CHARS = 260

_SWEEP = re.compile(
    r"https?://[^\s)>\]\"']+"                                # links
    r"|(?:~|\.{1,2})?(?:/[\w.@+~-]+){2,}"                     # paths
    r"|`[^`\n]{3,80}`"                                        # anything the writer put in backticks
    r"|\b[\w-]+\.(?:py|js|mjs|ts|tsx|json|ya?ml|md|sh|toml|db|log|plist|service|conf|env|sql|html|css)\b"
    r"|\b[0-9a-f]{7,40}\b"                                   # commit and content hashes
    r"|\b(?:port|pid|id|uid|line|exit(?: code)?|status|version|v)[ =:#]*\d[\w.]*"
    r"|:\d{2,5}\b"                                           # :8080
    r"|--[a-z][\w-]+"                                         # flags
    r"|\b[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}\b"                      # ENV_NAMES and CONSTANTS
    r"|\b\w+(?:Error|Exception|Warning)\b"                   # error class names
    r"|\b[\w-]+(?:[./][\w-]+){2,}\b",                        # dotted and slashed names: a.b.c, org/repo/x
    re.IGNORECASE)


def sweep(text: str, limit: int = SWEEP_CHARS) -> str:
    """The identifier-shaped strings in ``text``, in order, de-duplicated, within ``limit``.

    A regex cannot tell which identifier matters. It does not have to: the writer can, as
    long as the identifier is still on the page when the writer reads it.
    """
    seen, out, used = set(), [], 0
    for match in _SWEEP.finditer(text):
        token = match.group(0).strip("`.,;:)('\"")
        if len(token) < 3 or token.lower() in seen or privacy.is_sensitive(token):
            continue
        if used + len(token) + 2 > limit:
            break
        seen.add(token.lower())
        out.append(token)
        used += len(token) + 2
    return ", ".join(out)


def anchors(messages: Sequence[Mapping[str, Any]], limit: int = 900) -> List[str]:
    """Identifier-shaped strings from the WHOLE transcript, most-repeated first.

    A capsule is a few hundred words. A working session names dozens of paths, ids, ports
    and commands, and the ones the writer leaves out are exactly what the next session
    goes looking for. This list costs no model call and sits outside the word budget, so
    the writer's choices stop being the only way an identifier survives.

    Nothing here knows which of them matter. Repetition is the only signal used: a string
    the session came back to three times outranks one it printed once.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for index, message in enumerate(messages):
        body = compact._text(message)
        if not body or privacy.is_sensitive(body):
            continue
        for match in _SWEEP.finditer(body):
            token = match.group(0).strip("`.,;:)('\"")
            if len(token) < 4 or len(token) > 120:
                continue
            entry = seen.setdefault(token.lower(), {"token": token, "count": 0, "last": index})
            entry["count"] += 1
            entry["last"] = index
    out, used = [], 0
    for entry in sorted(seen.values(), key=lambda e: (-e["count"], -e["last"])):
        cost = len(entry["token"]) + 2
        if used + cost > limit:
            continue
        out.append(entry["token"])
        used += cost
    return out


# ── digest_v2: an experiment that lost, kept here so the result can be reproduced ────
# The idea was that compact.digest throws away early keep lines (it cuts the finished text
# with text[-limit:]) and clipped a "summarize" turn (a fate since removed) to its first 400 characters, so a digest
# that places keep lines first and sweeps identifiers out of the clipped part should recall
# more. Measured on seven sessions it recalled LESS than the digest it was meant to replace
# (paired, closed-book: 5 wins, 12 losses), so it never shipped in jevkit.

V2_KEEP_LINE_CHARS = 2_400
V2_BACKGROUND_HEAD_CHARS = 300


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = (limit * 2) // 3
    return text[:head] + f" […{len(text) - limit} chars…] " + text[-(limit - head):]


def digest_v2(messages: Sequence[Mapping[str, Any]], selection: Mapping[str, Any], limit: int = 24000) -> str:
    """The reduced transcript, spent like a budget: keep lines first, then the newest background.

    Order on the page is still chronological. What changes is which lines are there. Every
    keep line is placed before any background line is considered, each capped head-and-tail
    so one pasted log cannot crowd out the rest. Background lines carry their opening plus a
    sweep of the identifiers in the part that was cut. When something does not fit, the
    digest says how much was left out instead of silently starting mid-sentence.
    """
    fates = selection["fates"]
    entries: List[Dict[str, Any]] = []
    for index, message in enumerate(messages):
        fate = fates.get(str(index), "keep")
        body = compact._text(message).strip()
        if fate == "drop" or not body:
            continue
        if privacy.is_sensitive(body):
            # It was never sent to Jev; it must not reach the writer or the disk raw either.
            body = privacy.redact(body, V2_KEEP_LINE_CHARS)
        role = message.get("role", "user")
        if fate == "keep":
            line = f"[KEEP VERBATIM] {role}: {_head_tail(body, V2_KEEP_LINE_CHARS)}"
        else:
            line = f"[background] {role}: {body[:V2_BACKGROUND_HEAD_CHARS]}"
            if len(body) > V2_BACKGROUND_HEAD_CHARS:
                found = sweep(body[V2_BACKGROUND_HEAD_CHARS:])
                line += " […]" + (f" [also mentions: {found}]" if found else "")
        entries.append({"index": index, "keep": fate == "keep", "line": line})

    chosen, used = set(), 0
    # Newest first inside each class: if even the keep lines overflow, the oldest go, and
    # the note below says so.
    for wanted in (True, False):
        for entry in sorted((e for e in entries if e["keep"] is wanted), key=lambda e: -e["index"]):
            cost = len(entry["line"]) + 2
            if used + cost > limit - 120:
                continue
            chosen.add(entry["index"])
            used += cost
    lines = [e["line"] for e in entries if e["index"] in chosen]
    left_keep = sum(1 for e in entries if e["keep"] and e["index"] not in chosen)
    left_back = sum(1 for e in entries if not e["keep"] and e["index"] not in chosen)
    if left_keep or left_back:
        lines.insert(0, f"[note] {left_keep} keep line(s) and {left_back} background line(s) from earlier in the "
                        f"session did not fit and are not shown.")
    return "\n\n".join(lines)



def _words(prompt: str, words: int) -> str:
    return re.sub(r"Under \d+ words total\.", f"Under {words} words total.", prompt, count=1)


def _plain(turns: Sequence[Mapping[str, str]], tag: str, limit: int) -> str:
    prefix = f"[{tag}] " if tag else ""
    return "\n\n".join(f"{prefix}{m['role']}: {m['content']}" for m in turns)[-limit:]


def _selection(fates: Mapping[int, str]) -> Dict[str, Any]:
    return {"fates": {str(k): v for k, v in sorted(fates.items())}}


def build_input(arm: str, turns: List[Dict[str, str]], limit: int, words: int,
                jev_selection: Optional[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """The exact prompt a writer would receive for this arm, plus what it cost to make."""
    meta: Dict[str, Any] = {"jev_calls": 0, "jev_ms": 0}
    total = len(turns)
    if arm == "none":
        return "", meta
    if arm == "plugin_fallback":
        # What ships with no Jev: the plain tail, untagged (handoff.py sends marked=False).
        return _words(compact.handoff_prompt(_plain(turns, "", limit), marked=False), words), meta
    if arm == "tail_plain":
        return _words(compact.handoff_prompt(_plain(turns, "", limit), marked=False), words), meta
    if arm == "full":
        return _words(compact.handoff_prompt(_plain(turns, "", 600_000), marked=False), words), meta
    selector, _, version = arm.partition("_v2")
    make_digest = digest_v2 if arm.endswith("_v2") else compact.digest
    if selector in ("regex_keep", "failopen"):
        # failopen: Jev down, every turn kept whole. regex_keep: identifier-shaped turns and the
        # tail kept, the rest dropped, with no judgement at all.
        fates = {i: ("keep" if selector == "failopen" or i >= total - 8 or _IDENT.search(m["content"])
                     else "drop") for i, m in enumerate(turns)}
        meta["counts"] = {f: sum(1 for v in fates.values() if v == f) for f in compact.FATE}
        return _words(compact.handoff_prompt(make_digest(turns, _selection(fates), limit)), words), meta
    if jev_selection is None:
        raise RuntimeError("the jev arm has to run before the arms matched to it")
    if selector == "jev":
        meta.update({"jev_calls": jev_selection["jev_calls"], "jev_ms": jev_selection["latency_ms"],
                     "jev_status": jev_selection["status"], "counts": jev_selection["counts"]})
        return _words(compact.handoff_prompt(make_digest(turns, jev_selection, limit)), words), meta
    if selector == "recency_matched":
        counts = jev_selection["counts"]
        drop = int(counts.get("drop", 0))
        fates = {i: "drop" if i < drop else "keep" for i in range(total)}   # the oldest are dropped
        meta["counts"] = {f: sum(1 for v in fates.values() if v == f) for f in compact.FATE}
        return _words(compact.handoff_prompt(make_digest(turns, _selection(fates), limit)), words), meta
    raise ValueError(arm)


def write_capsule(prompt: str) -> str:
    if not prompt:
        return ""
    return chat(WRITER_MODEL, "You write handoffs exactly as instructed.", prompt,
                max_tokens=6000, purpose="writer").strip()


# ── answering and grading ────────────────────────────────────────────────────

ANSWER_SYSTEM = "You are a fresh session that has only a handoff note from the previous session."

CLOSED_USER = """HANDOFF NOTE (this is everything you know):
<handoff>
{capsule}
</handoff>

Answer each question using ONLY the handoff note. If the note does not contain the answer, the
answer is exactly "unknown". Never guess, never use outside knowledge. Keep answers short.

QUESTIONS
{questions}

Return JSON only: {{"answers": {{"q1": "...", "q2": "..."}}}}"""

SEARCH_USER = """HANDOFF NOTE (this is everything you know so far):
<handoff>
{capsule}
</handoff>

You also have ONE search of the previous session's full transcript per question. For each
question: if the handoff note already answers it, give the answer. If it does not, give a search
query instead: 2 to 6 plain keywords most likely to appear next to the answer in the old session.

QUESTIONS
{questions}

Return JSON only: {{"q1": {{"answer": "..."}}, "q2": {{"search": "keywords here"}}, ...}}"""

FOUND_USER = """HANDOFF NOTE:
<handoff>
{capsule}
</handoff>

For each question below you ran one search of the previous session. The snippets it returned are
shown. Answer from the handoff note and the snippets ONLY. If neither contains the answer, the
answer is exactly "unknown". Never guess. Keep answers short.

{blocks}

Return JSON only: {{"answers": {{"q1": "...", "q2": "..."}}}}"""

JUDGE_SYSTEM = "You grade short answers against gold answers. You are strict and consistent."

JUDGE_USER = """Grade each candidate answer against its gold answer.
- "correct": states the same fact. For identifiers (paths, ids, commands, ports, versions, keys,
  error strings) the identifying part must match exactly; extra correct words are fine.
- "partial": right direction but missing or wrong in a detail that matters.
- "wrong": a different fact, or a confident answer that contradicts the gold.
- "unknown": the candidate says unknown, or gives no answer.

{items}

Return JSON only: {{"grades": {{"q1": "correct|partial|wrong|unknown", ...}}}}"""

_WORD = re.compile(r"[A-Za-z0-9_./:@-]{2,}")


class Index:
    """A stand-in for a session search tool: BM25 over turns, snippets around the hits."""

    def __init__(self, rows: Sequence[Mapping[str, str]]):
        self.rows = rows
        self.docs = [[w.lower() for w in _WORD.findall(r["content"])] for r in rows]
        self.df: Dict[str, int] = {}
        for doc in self.docs:
            for word in set(doc):
                self.df[word] = self.df.get(word, 0) + 1
        self.avg = (sum(len(d) for d in self.docs) / len(self.docs)) if self.docs else 1.0

    def search(self, query: str, top: int = 3, snippet: int = 700) -> List[str]:
        terms = [w.lower() for w in _WORD.findall(query)]
        if not terms:
            return []
        scored = []
        n = len(self.docs)
        for i, doc in enumerate(self.docs):
            if not doc:
                continue
            score = 0.0
            for term in terms:
                tf = doc.count(term)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df.get(term, 0) + 0.5) / (self.df.get(term, 0) + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(doc) / self.avg))
            if score:
                scored.append((score, i))
        out = []
        for _, i in sorted(scored, reverse=True)[:top]:
            text = self.rows[i]["content"]
            low = text.lower()
            at = min((low.find(t) for t in terms if t in low), default=0)
            start = max(0, at - snippet // 3)
            out.append(f"T{i} [{self.rows[i]['role']}]: …{text[start:start + snippet]}…")
        return out


def _qblock(questions: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(f"{q['id']}: {q['question']}" for q in questions)


def answer_closed(capsule: str, questions: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    if not capsule.strip():
        return {q["id"]: "unknown" for q in questions}
    reply = chat_json(ANSWER_MODEL, ANSWER_SYSTEM, CLOSED_USER.format(capsule=capsule, questions=_qblock(questions)),
                      max_tokens=3000, purpose="answer")
    answers = reply.get("answers") or {}
    return {q["id"]: str(answers.get(q["id"], "unknown")) for q in questions}


def answer_recovery(capsule: str, questions: Sequence[Mapping[str, Any]], index: Index) -> Tuple[Dict[str, str], int]:
    first = chat_json(ANSWER_MODEL, ANSWER_SYSTEM,
                      SEARCH_USER.format(capsule=capsule or "(there is no handoff note)", questions=_qblock(questions)),
                      max_tokens=3000, purpose="answer")
    answers: Dict[str, str] = {}
    blocks = []
    for q in questions:
        item = first.get(q["id"]) if isinstance(first, dict) else None
        if isinstance(item, dict) and item.get("answer") and str(item["answer"]).strip().lower() != "unknown":
            answers[q["id"]] = str(item["answer"])
            continue
        query = str((item or {}).get("search") or q["question"]) if isinstance(item, dict) else q["question"]
        hits = index.search(query)
        blocks.append(f"{q['id']}: {q['question']}\nsearch: {query}\n" + ("\n".join(hits) if hits else "(no results)"))
    searched = len(blocks)
    if blocks:
        reply = chat_json(ANSWER_MODEL, ANSWER_SYSTEM,
                          FOUND_USER.format(capsule=capsule or "(there is no handoff note)", blocks="\n\n".join(blocks)),
                          max_tokens=3000, purpose="answer")
        found = reply.get("answers") or {}
        for q in questions:
            if q["id"] not in answers:
                answers[q["id"]] = str(found.get(q["id"], "unknown"))
    return answers, searched


def grade(questions: Sequence[Mapping[str, Any]], answers: Mapping[str, str]) -> Dict[str, str]:
    items = "\n\n".join(f"{q['id']}\nquestion: {q['question']}\ngold: {q['answer']}\ncandidate: {answers.get(q['id'], 'unknown')}"
                        for q in questions)
    reply = chat_json(JUDGE_MODEL, JUDGE_SYSTEM, JUDGE_USER.format(items=items), max_tokens=2000, purpose="judge")
    grades = reply.get("grades") or {}
    return {q["id"]: (str(grades.get(q["id"], "unknown")).lower() if str(grades.get(q["id"], "")).lower()
                      in ("correct", "partial", "wrong", "unknown") else "unknown") for q in questions}


def oracle(rows: Sequence[Mapping[str, str]], questions: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Answers with the whole transcript in view. A question this gets wrong is a bad question."""
    text = render(rows, EXAM_TURN_CHARS, EXAM_TOTAL_CHARS)
    reply = chat_json(EXAM_MODEL, "You answer questions about a transcript, briefly and exactly.",
                      f"TRANSCRIPT\n{text}\n\nQUESTIONS\n{_qblock(questions)}\n\n"
                      'Return JSON only: {"answers": {"q1": "..."}}', max_tokens=3000, purpose="oracle")
    answers = reply.get("answers") or {}
    return {q["id"]: str(answers.get(q["id"], "unknown")) for q in questions}


# ── one transcript ───────────────────────────────────────────────────────────

def run_transcript(path: Path, out: Path, arms: Sequence[str], n: int, limit: int, words: int,
                   recovery: bool, anchor_chars: int = 0) -> Dict[str, Any]:
    rows = load(path)
    turns = dialogue(rows)
    key = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    work = out / key
    work.mkdir(parents=True, exist_ok=True)
    stats = {"rows": len(rows), "dialogue_turns": len(turns), "tool_rows": sum(1 for r in rows if r["role"] == "tool"),
             "chars": sum(len(r["content"]) for r in rows), "dialogue_chars": sum(len(t["content"]) for t in turns)}

    exam_file = work / f"exam-{n}.json"
    if exam_file.exists():
        questions = json.loads(exam_file.read_text())
    else:
        questions = make_exam(rows, n)
        exam_file.write_text(json.dumps(questions, indent=2))
    oracle_file = work / f"oracle-{n}.json"
    if oracle_file.exists():
        oracle_grades = json.loads(oracle_file.read_text())
    else:
        oracle_grades = grade(questions, oracle(rows, questions))
        oracle_file.write_text(json.dumps(oracle_grades, indent=2))
    sound = [q for q in questions if oracle_grades.get(q["id"]) == "correct"]

    index = Index(rows)
    jev_selection = None
    needs_jev = [a for a in arms if a.partition("_v2")[0] in ("jev", "recency_matched")]
    if needs_jev:
        selection_file = work / "jev-selection.json"
        if selection_file.exists():
            jev_selection = json.loads(selection_file.read_text())
        else:
            jev_selection = compact.select(turns, keep_last=8)
            selection_file.write_text(json.dumps(jev_selection, indent=2))
    results: Dict[str, Any] = {}
    for arm in arms:
        tag = f"{arm}-{limit}-{words}"
        cache = work / f"arm-{tag}.json"
        if cache.exists():
            record = json.loads(cache.read_text())
        else:
            started = time.monotonic()
            prompt, meta = build_input(arm, turns, limit, words, jev_selection)
            capsule = write_capsule(prompt)
            record = {"arm": arm, "meta": meta, "writer_input_chars": len(prompt), "capsule_chars": len(capsule),
                      "capsule_words": len(capsule.split()), "seconds": round(time.monotonic() - started, 2),
                      "capsule": capsule}
            cache.write_text(json.dumps(record, indent=2))
        capsule = record["capsule"]
        if anchor_chars and (capsule or arm == "none"):
            # Free, mechanical, outside the word budget: what the plugin can append itself.
            found = anchors(turns, anchor_chars)
            capsule = (capsule + "\n\n## Identifiers seen in the previous session (unverified, most repeated first)\n"
                       + ", ".join(found)).strip()
        suffix = f"-a{anchor_chars}" if anchor_chars else ""
        for mode in (("closed", "recovery") if recovery else ("closed",)):
            if arm == "none" and mode == "closed" and not anchor_chars:
                continue
            graded_file = work / f"graded-{tag}{suffix}-{mode}-{n}.json"
            if graded_file.exists():
                graded = json.loads(graded_file.read_text())
            else:
                searched = 0
                if mode == "closed":
                    answers = answer_closed(capsule, questions)
                else:
                    answers, searched = answer_recovery(capsule, questions, index)
                graded = {"answers": answers, "grades": grade(questions, answers), "searched": searched}
                graded_file.write_text(json.dumps(graded, indent=2))
            record.setdefault("scores", {})[mode] = score(questions, graded["grades"], sound) | {"searched": graded["searched"]}
        results[arm] = {k: v for k, v in record.items() if k != "capsule"}
    return {"key": key, "stats": stats, "questions": len(questions), "sound_questions": len(sound), "arms": results}


def score(questions: Sequence[Mapping[str, Any]], grades: Mapping[str, str],
          sound: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    def pct(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if not items:
            return {"n": 0}
        got = [grades.get(q["id"], "unknown") for q in items]
        return {"n": len(items), "correct": got.count("correct"), "partial": got.count("partial"),
                "wrong": got.count("wrong"), "unknown": got.count("unknown")}
    out = {"all": pct(questions), "sound": pct(sound)}
    for field in ("kind", "evidence_role", "third"):
        out[field] = {str(v): pct([q for q in sound if q.get(field) == v])
                      for v in sorted({q.get(field) for q in sound}, key=str)}
    out["per_question"] = {q["id"]: grades.get(q["id"], "unknown") for q in questions}
    return out


# ── the scorecard ────────────────────────────────────────────────────────────

def scorecard(runs: Sequence[Mapping[str, Any]], arms: Sequence[str], limit: int, words: int) -> str:
    lines = ["# Compaction eval scorecard", "",
             f"{len(runs)} transcripts, digest limit {limit:,} chars, capsule budget {words} words. "
             f"Writer `{WRITER_MODEL}`, answers `{ANSWER_MODEL}`, exam and judge `{EXAM_MODEL}`.", "",
             "Recall is scored on the questions an oracle with the whole transcript answered correctly "
             "(a question the oracle misses is a bad question, not a finding).", "",
             "| arm | closed-book recall | with one search | wrong (closed) | writer input chars | capsule words | Jev calls | Jev s |",
             "|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        rows = [r["arms"][arm] for r in runs if arm in r["arms"]]
        if not rows:
            continue

        def total(mode: str, field: str) -> int:
            return sum(((row.get("scores") or {}).get(mode) or {}).get("sound", {}).get(field, 0) for row in rows)

        def rate(mode: str) -> str:
            n = total(mode, "n")
            return f"{100 * total(mode, 'correct') / n:.1f}% ({total(mode, 'correct')}/{n})" if n else "-"
        wrong = total("closed", "wrong")
        lines.append(f"| {arm} | {rate('closed')} | {rate('recovery')} | {wrong} | "
                     f"{sum(r['writer_input_chars'] for r in rows) // len(rows):,} | "
                     f"{sum(r['capsule_words'] for r in rows) // len(rows)} | "
                     f"{sum(r['meta'].get('jev_calls', 0) for r in rows)} | "
                     f"{sum(r['meta'].get('jev_ms', 0) for r in rows) / 1000:.1f} |")
    lines += ["", "## Transcripts", "", "| # | rows | dialogue turns | tool rows | chars | sound questions |", "|---|---|---|---|---|---|"]
    for i, run in enumerate(runs, 1):
        s = run["stats"]
        lines.append(f"| t{i} | {s['rows']} | {s['dialogue_turns']} | {s['tool_rows']} | {s['chars']:,} | "
                     f"{run['sound_questions']}/{run['questions']} |")
    lines += ["", "## Spend", "", "| purpose | calls | input tokens | output tokens | $ |", "|---|---|---|---|---|"]
    for purpose, b in sorted(SPEND.items()):
        lines.append(f"| {purpose} | {int(b['calls'])} | {int(b['input_tokens']):,} | {int(b['output_tokens']):,} | {b['cost_usd']:.4f} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--transcripts", required=True, help="a folder of exported sessions (.json or .jsonl)")
    parser.add_argument("--out", required=True, help="where exams, capsules and grades go. NOT inside the repo")
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--questions", type=int, default=15)
    parser.add_argument("--limit", type=int, default=DIGEST_CHARS)
    parser.add_argument("--words", type=int, default=400)
    parser.add_argument("--no-recovery", action="store_true")
    parser.add_argument("--anchors", type=int, default=0,
                        help="append this many chars of compact.anchors() to every capsule before answering")
    args = parser.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    if ROOT in out.parents or out == ROOT:
        sys.exit("--out is inside the repo. Exams and capsules are real session content; put them elsewhere.")
    out.mkdir(parents=True, exist_ok=True)
    arms = [a for a in args.arms.split(",") if a in ARMS]
    files = sorted(p for p in Path(args.transcripts).expanduser().iterdir() if p.suffix in (".json", ".jsonl"))
    if not files:
        sys.exit("no transcripts found")

    runs = []
    for number, path in enumerate(files, 1):
        print(f"[{number}/{len(files)}] {path.name}", file=sys.stderr, flush=True)
        try:
            runs.append(run_transcript(path, out, arms, args.questions, args.limit, args.words,
                                       not args.no_recovery, args.anchors))
        except Exception as error:  # noqa: BLE001 - keep the transcripts that did work
            print(f"  failed: {type(error).__name__}: {str(error)[:200]}", file=sys.stderr, flush=True)
    tag = f"{args.limit}-{args.words}" + (f"-a{args.anchors}" if args.anchors else "")
    (out / f"results-{tag}.json").write_text(json.dumps({"runs": runs, "spend": SPEND}, indent=2))
    card = scorecard(runs, arms, args.limit, args.words)
    (out / f"SCORECARD-{tag}.md").write_text(card)
    print(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
