#!/usr/bin/env python3
"""How a maintainer's answer names one of a role's recorded asks, and how the role reads it back.

The viewer's Asks dialog writes one line per answered ask, in one message with a wake:

    re ask <n> [<id>] "<opening words>": <choice>. <note>

`<n>` is the ask's number when the answer was written, `<id>` is `ask_id` of the ask's text (a short stable hash) and
the quote is the ask's opening words (`ask_lead`). A role re-raises and reorders its asks, so a number alone can name a
different ask by the time the answer is read (measured 2026-11-25: a role replaced both its merge asks three minutes
after an answer and raised new ones at the same numbers). The id decides, then the quote; the number counts only for an
answer that carries neither (one typed by hand, or a dialog from before ids). `resolve` maps each line to the role's
ask now, or says that it names none, and `next` prints that under the message, so the role never matches by position.
"""
from __future__ import annotations

import hashlib
import re

ID_LEN = 6
LEAD_WORDS = 8
LEAD_CHARS = 60
# `re ask 2`, optionally followed by `[a1b2c3]` and a quoted lead, then the answer up to the next `re ask` or the end
_LINE = re.compile(r'\bre ask (\d+)(?:[ \t]*\[([0-9a-f]{4,16})\])?(?:[ \t]*"([^"\n]*)")?[ \t]*:?', re.I)


def norm(text: str) -> str:
    """An ask's text as ids and quotes read it: whitespace collapsed, double quotes as single ones (a quote cannot end
    the answer line's own)."""
    return " ".join(str(text or "").split()).replace('"', "'")


def ask_id(text: str) -> str:
    """A short stable hash of an ask's text: the same ask has the same id on every host and in every view."""
    return hashlib.sha1(norm(text).encode()).hexdigest()[:ID_LEN]


def ask_lead(text: str) -> str:
    """An ask's opening words, at most LEAD_WORDS words and LEAD_CHARS characters (the Asks dialog cuts the same)."""
    t = norm(text)
    out = ""
    for w in t.split(" ")[:LEAD_WORDS]:
        nxt = f"{out} {w}".strip()
        if len(nxt) > LEAD_CHARS:
            break
        out = nxt
    return out or t[:LEAD_CHARS]


def name(r: dict) -> str:
    """A parsed line's ask as it names it: `re ask 2 [a1b2c3] "Merge PR #12 ..."`."""
    return f"re ask {r['n']}" + (f" [{r['id']}]" if r["id"] else "") + (f" \"{r['lead']}\"" if r["lead"] else "")


def parse(text: str) -> list[dict]:
    """[{n, id, lead, body}] for every `re ask` line in a message, in order."""
    t = str(text or "")
    ms = list(_LINE.finditer(t))
    out = []
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(t)
        out.append({"n": int(m.group(1)), "id": (m.group(2) or "").lower(), "lead": m.group(3) or "",
                    "body": t[m.end():end].strip()})
    return out


def resolve(text: str, asks: list, ids: list | None = None) -> list[dict]:
    """Each `re ask` line of a message against a role's asks now: the parsed line with `now` (the ask's number now, or
    None) and `how`: `id` (its id matches), `reworded` (no id matches, its quote opens one ask: that ask changed after the
    answer was written), `quote` (no id given, its quote opens one ask), `number` (neither given: the number, as before),
    `ambiguous` (its quote opens several asks) or `gone` (no ask it names is open). `ids`: the asks' ids when the caller
    has them (a status row keeps them for asks it cuts short); else they are computed from `asks`."""
    asks = [str(a) for a in asks or []]
    ids = list(ids) if ids and len(ids) == len(asks) else [ask_id(a) for a in asks]
    out = []
    for r in parse(text):
        now, how = None, "gone"
        if r["id"] and r["id"] in ids:
            now, how = ids.index(r["id"]) + 1, "id"
        elif r["lead"].strip():
            lead = norm(r["lead"]).lower()
            hits = [i for i, a in enumerate(asks, 1) if norm(a).lower().startswith(lead)]
            if len(hits) > 1 and r["n"] in hits:
                hits = [r["n"]]
            if len(hits) == 1:
                now, how = hits[0], "reworded" if r["id"] else "quote"
            elif hits:
                how = "ambiguous"
        elif not r["id"] and 1 <= r["n"] <= len(asks):
            now, how = r["n"], "number"
        out.append(dict(r, now=now, how=how, hits=hits if how == "ambiguous" else []))
    return out


def answered(text: str, asks: list, ids: list | None = None) -> list[int]:
    """The role's asks (numbers now) a message answers: the lines `resolve` maps to one, a reworded ask excepted."""
    return sorted({r["now"] for r in resolve(text, asks, ids) if r["now"] and r["how"] != "reworded"})


def next_lines(text: str, asks: list, sid: str = "", slug: str = "") -> list[str]:
    """What `next` prints under a maintainer's message that answers asks: one line per `re ask` line, saying which of
    the role's asks it answers now, or that it answers none of them, so the role acts on the ask itself, never on its
    number."""
    asks = [str(a) for a in asks or []]
    tail = f" --shoal {sid} --slug {slug}" if sid and slug else ""
    out = []
    for r in resolve(text, asks):
        named = name(r)
        if r["how"] in ("id", "quote"):
            out.append(f"    ANSWER {named} -> your ask {r['now']} now (\"{ask_lead(asks[r['now'] - 1])}\"): act on it, "
                       f"then `queue{tail} --answered {ask_id(asks[r['now'] - 1])}`")
        elif r["how"] == "number":
            out.append(f"    ANSWER {named} names its ask by number only: your ask {r['now']} now is "
                       f"\"{ask_lead(asks[r['now'] - 1])}\"; act on it only if that is the ask the maintainer answered, "
                       f"else ask which one")
        elif r["how"] == "reworded":
            out.append(f"    ANSWER {named} -> opens like your ask {r['now']}, but that ask's text changed after this "
                       f"answer was written: confirm it still applies before acting (a merge grant is for the head it "
                       f"was given for), else ask again")
        elif r["how"] == "ambiguous":
            out.append(f"    ANSWER {named} -> opens like several of your asks ({', '.join(map(str, r['hits']))}): do not "
                       f"guess; ask the maintainer which one it answers")
        else:
            out.append(f"    ANSWER {named} -> NOT one of your open asks (answered, dropped or replaced since): do not "
                       f"apply it to another ask; tell the maintainer (`msg{tail} --to user`), and raise the ask again "
                       f"if it still applies")
    return out
