"""The deterministic planner: clause by clause, keywords in English, Hindi and Kannada.

Pure: `now` is passed in, nothing reads the clock, the network or the environment. A clause that
is unclear is ignored (or turned into a short question), never guessed. This module never writes
an email address or a phone number: a recipient is the name as spoken, and the engine resolves it
against the person's saved contacts. It never proposes `gmail.send`, only `gmail.draft_reply`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.personal.dates import IST, parse_when
from app.personal.voice import reminder_request
from app.tasks.kinds import is_phone

_W = r"[\wऀ-ॿಀ-೿]"
_B0 = rf"(?<!{_W})"
_B1 = rf"(?!{_W})"


def _words(*alts: str) -> str:
    return _B0 + "(?:" + "|".join(alts) + ")" + _B1


# ---- triggers per category -------------------------------------------------------------------

_WHATSAPP = re.compile(
    _words(
        r"whats\s?app",
        r"व्हाट्स\s?ऐप",
        r"व्हाट्स\s?एप",
        r"वॉट्स\s?ऐप",
        r"व्हॉट्स\s?ऐप",
        r"व्हाट्सअप",
        r"ವಾಟ್ಸ್?\s?ಆಪ್",
        r"ವಾಟ್ಸ್?\s?ಅಪ್",
        r"ವಾಟ್ಸಾಪ್",
    ),
    re.IGNORECASE,
)
_MESSAGE = re.compile(
    _words(
        r"messages?",
        r"msg",
        r"text",
        r"sms",
        r"मैसेज",
        r"मेसेज",
        r"संदेश",
        r"ಮೆಸೇಜ್",
        r"ಸಂದೇಶ[\wऀ-ॿ\u0c80-\u0cff]*",
    ),
    re.IGNORECASE,
)
_EMAIL = re.compile(
    _words(
        r"e-?mails?",
        r"mails?",
        r"inbox",
        r"gmail",
        r"ईमेल",
        r"मेल",
        r"इनबॉक्स",
        r"जीमेल",
        r"ಇಮೇಲ್[\wऀ-ॿ\u0c80-\u0cff]*",
        r"ಮೇಲ್[\wऀ-ॿ\u0c80-\u0cff]*",
        r"ಇನ್‌ಬಾಕ್ಸ್",
        r"ಜಿಮೇಲ್",
    ),
    re.IGNORECASE,
)
_CALENDAR = re.compile(
    _words(
        r"calendar",
        r"availability",
        r"am\s+i\s+free",
        r"are\s+we\s+free",
        r"meetings?",
        r"schedule",
        r"appointments?",
        r"events?",
        r"कैलेंडर",
        r"मीटिंग",
        r"बैठक",
        r"शेड्यूल",
        r"अपॉइंटमेंट",
        r"इवेंट",
        r"ಕ್ಯಾಲೆಂಡರ್",
        r"ಮೀಟಿಂಗ್[\wऀ-ॿ\u0c80-\u0cff]*",
        r"ಸಭೆ[\wऀ-ॿ\u0c80-\u0cff]*",
        r"ಅಪಾಯಿಂಟ್‌?ಮೆಂಟ್",
        r"ವೇಳಾಪಟ್ಟಿ",
    ),
    re.IGNORECASE,
)
_DAY = re.compile(
    r"plan\s+(?:out\s+)?(?:my|the)\s+(?:whole\s+)?day|(?<!\w)my\s+day(?!\w)|(?<!\w)agenda(?!\w)"
    r"|मेरा\s+दिन|दिन\s+की\s+योजना|आज\s+का\s+प्लान|mera\s+din|ನನ್ನ\s+ದಿನ|ದಿನದ\s+ಯೋಜನೆ",
    re.IGNORECASE,
)
_REMINDER = re.compile(
    r"remind(?:\s+me|er)\b|याद\s*दिला|रिमाइंडर|ನೆನಪಿಸ|ರಿಮೈಂಡರ್|yaad\s*dila",
    re.IGNORECASE,
)

# A verb that starts a new action: used to decide whether "and" separates two requests.
_ACTION_VERB = (
    r"(?:check|schedule|remind|send|message|text|whatsapp|e-?mail|mail|plan|draft|reply|show|"
    r"cancel|reschedule|set\s+up|set|book|arrange|create|add|find|search|read|see|tell|look)\b"
)


def _has_trigger(clause: str) -> bool:
    return any(p.search(clause) for p in (_WHATSAPP, _MESSAGE, _EMAIL, _CALENDAR, _DAY, _REMINDER))


_REPLY_TO = re.compile(r"^\s*(?:please\s+)?(?:reply|respond)\s+to\s+\S", re.IGNORECASE)


def trigger_categories(text: str) -> set[str]:
    found: set[str] = set()
    if _REPLY_TO.search(text):
        found.add("email")
    for name, pattern in (
        ("whatsapp", _WHATSAPP),
        ("message", _MESSAGE),
        ("email", _EMAIL),
        ("calendar", _CALENDAR),
        ("day", _DAY),
        ("reminder", _REMINDER),
    ):
        if pattern.search(text):
            found.add(name)
    return found


# ---- clause splitting ------------------------------------------------------------------------

_SENTENCE = re.compile(
    r"[;।?!]+|(?<!a\.m)(?<!p\.m)(?<!\d)(?<!\bDr)(?<!\bMr)(?<!\bMs)(?<!\bMrs)\.\s+(?=[A-Z"
    r"ऀ-ॿಀ-೿])"
)
_THEN = re.compile(
    rf"(?:{_B0}(?:and\s+then|then|after\s+that|afterwards|फिर|फ़िर|phir|उसके\s+बाद|ಆಮೇಲೆ|ನಂತರ)"
    rf"{_B1})",
    re.IGNORECASE,
)
_CONJ = re.compile(rf"{_B0}(?:और|aur|ಮತ್ತು){_B1}")
_AND = re.compile(rf"\band\s+(?={_ACTION_VERB})", re.IGNORECASE)
_LEAD = re.compile(
    rf"^\s*(?:(?:and|also|then|plus|please|और|aur|ಮತ್ತು|फिर|phir|ನಂತರ|ಆಮೇಲೆ)"
    rf"{_B1}[\s,]*)+",
    re.IGNORECASE,
)


def _split(text: str) -> list[str]:
    pieces: list[str] = []
    for sentence in _SENTENCE.split(text):
        for part in _THEN.split(sentence):
            for comma_part in part.split(","):
                pieces.extend(_AND.split(comma_part))
    # Hindi and Kannada "and": split only when the next piece holds an action keyword.
    refined: list[str] = []
    for piece in pieces:
        parts = _CONJ.split(piece)
        merged = parts[0]
        for nxt in parts[1:]:
            if _has_trigger(nxt) and _has_trigger(merged):
                refined.append(merged)
                merged = nxt
            else:
                merged = f"{merged} और {nxt}"
        refined.append(merged)
    clauses: list[str] = []
    for piece in refined:
        clause = _LEAD.sub("", piece).strip(" \t\n.,।")
        if not clause:
            continue
        # "Meeting with Rahul, tomorrow afternoon": a bare date/time joins the clause before.
        if clauses and not _has_trigger(clause) and parse_when(clause, _EPOCH) is not None:
            clauses[-1] = f"{clauses[-1]} {clause}"
            continue
        clauses.append(clause)
    return clauses


_EPOCH = datetime(2026, 1, 1, 0, 0, tzinfo=IST)  # only to test "is this a date phrase"

# ---- helpers ---------------------------------------------------------------------------------

_NAME_FILLER = {
    "send",
    "a",
    "an",
    "the",
    "whatsapp",
    "whats",
    "app",
    "message",
    "messages",
    "msg",
    "text",
    "sms",
    "to",
    "please",
    "also",
    "and",
    "quick",
    "short",
    "note",
    "email",
    "mail",
    "write",
    "draft",
    "compose",
    "reply",
    "respond",
    "ping",
    "dm",
    "on",
}
_HI_NAME_FILLER = {
    "और",
    "aur",
    "फिर",
    "phir",
    "मुझे",
    "mujhe",
    "व्हाट्सऐप",
    "व्हाट्सएप",
    "वॉट्सऐप",
    "whatsapp",
    "मैसेज",
    "संदेश",
    "ईमेल",
    "मेल",
    "please",
}
_QUOTED = re.compile('["\u201c\u201d\u2018]([^"\u201c\u201d]{1,1000})["\u201c\u201d\u2019]')
_EN_MARK = re.compile(
    r"\b(?:saying(?:\s+that)?|that\s+says|which\s+says|says|telling\s+(?:him|her|them)"
    r"(?:\s+that)?|asking(?:\s+(?:him|her|them))?(?:\s+(?:to|if|whether))?|with\s+the\s+message"
    r"|that|about|regarding)\b[\s:,-]*|:\s*",
    re.IGNORECASE,
)
_HI_MARK = re.compile(
    rf"(?:{_B0}(?:कहो\s+कि|बोलो\s+कि|बताओ\s+कि|कि|keh[\wऀ-ॿ\u0c80-\u0cff]*\s+ki|bolo\s+ki|ki){_B1})"
)
_HI_TO = re.compile(rf"{_B0}(?:को|ko){_B1}", re.IGNORECASE)
_KN_MARK = re.compile(r"ಎಂದು|ಅಂತ")
_KN_FILLER = re.compile(
    r"ವಾಟ್ಸ್?\s?ಆಪ್|ವಾಟ್ಸ್?\s?ಅಪ್|ವಾಟ್ಸಾಪ್|ಮೆಸೇಜ್|ಸಂದೇಶ[\wऀ-ॿ\u0c80-\u0cff]*|ಕಳುಹಿಸಿ|ಕಳಿಸಿ|ಮಾಡಿ|ಇಮೇಲ್|ಬರೆಯಿರಿ"
)


def _tidy(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\n,.;:-'\"“”।")


def _cap(name: str) -> str:
    return " ".join(
        w[:1].upper() + w[1:] if w.islower() and w.isascii() else w for w in name.split()
    )


def _good_name(name: str, *, allow_phone: bool) -> str | None:
    """A spoken recipient. Never an address; a phone number only if the person typed one."""
    name = _tidy(name)
    if not name or len(name) > 60 or "@" in name:
        return None
    if allow_phone and is_phone(name.replace(" ", "")):
        return name.replace(" ", "")
    if any(ch.isdigit() for ch in name):
        return None
    return name


def _kn_strip_dative(word: str) -> str:
    word = word.replace("‌", "")
    for suffix in ("ನಿಗೆ", "ಗೆ"):
        if word.endswith(suffix) and len(word) > len(suffix):
            return word[: -len(suffix)]
    return word


def _message_parts(clause: str) -> tuple[str | None, str | None]:
    """(recipient name, body) as spoken. Either may be None when the clause does not give it."""
    quoted = _QUOTED.search(clause)
    # Kannada: "<name>ಗೆ <body> ಎಂದು ವಾಟ್ಸಾಪ್ ಮಾಡಿ"
    if re.search(r"[ಀ-೿]", clause):
        tokens = clause.split()
        index = next((i for i, t in enumerate(tokens) if re.search(r"ಗೆ$", t)), None)
        name = _kn_strip_dative(tokens[index]) if index is not None else None
        mark = _KN_MARK.search(clause)
        body = None
        if mark and index is not None:
            before = " ".join(tokens[index + 1 :])
            cut = _KN_MARK.search(before)
            if cut:
                body = _tidy(_KN_FILLER.sub(" ", before[: cut.start()]))
        if quoted:
            body = _tidy(quoted.group(1))
        return name, body or None
    english = _EN_MARK.search(clause)
    if english is not None and not re.search(r"[ऀ-ॿ]", clause):
        pre = clause[: english.start()]
        body = quoted.group(1) if quoted else clause[english.end() :]
        if quoted:
            pre = clause[: quoted.start()]
        words = [w for w in re.findall(r"[^\s,]+", pre) if w.lower() not in _NAME_FILLER]
        return " ".join(words) or None, _tidy(body) or None
    if quoted and not re.search(r"[ऀ-ॿ]|\b(?:ko|ki)\b", clause):
        pre = clause[: quoted.start()]
        words = [w for w in re.findall(r"[^\s,]+", pre) if w.lower() not in _NAME_FILLER]
        return " ".join(words) or None, _tidy(quoted.group(1)) or None
    # Hindi, Devanagari or romanised: "<name> को ... कि <body>"
    to = _HI_TO.search(clause)
    if to is not None:
        pre = clause[: to.start()]
        words = [w for w in pre.split() if w.lower() not in _HI_NAME_FILLER]
        name = " ".join(words) or None
        rest = clause[to.end() :]
        mark = _HI_MARK.search(rest)
        body = None
        if quoted:
            body = quoted.group(1)
        elif mark:
            body = rest[mark.end() :]
        return name, (_tidy(body) if body else None) or None
    return None, None


# ---- result type -----------------------------------------------------------------------------


@dataclass
class RulesResult:
    tasks: list[dict[str, object]] = field(default_factory=lambda: [])
    clarifications: list[str] = field(default_factory=lambda: [])


# ---- per-kind builders -----------------------------------------------------------------------

_PERIOD_HOURS = [
    (re.compile(_words(r"morning", r"subah", r"सुबह", r"ಬೆಳಿಗ್ಗೆ", r"ಬೆಳಗ್ಗೆ"), re.I), 9),
    (re.compile(_words(r"afternoon", r"dopahar", r"दोपहर", r"ಮಧ್ಯಾಹ್ನ"), re.I), 15),
    (re.compile(_words(r"evening", r"shaam", r"sham", r"शाम", r"ಸಂಜೆ"), re.I), 18),
]
_EXPLICIT_CLOCK = re.compile(
    rf"\d{{1,2}}:\d{{2}}|\d{{1,2}}\s*(?:am|pm|a\.m\.|p\.m\.|baje|बजे|ಗಂಟೆ)|{_B0}at\s+\d|{_B0}noon{_B1}|midnight",
    re.IGNORECASE,
)

_CREATE_VERB = re.compile(
    rf"{_B0}(?:schedule|set\s*up|arrange|book|create|add|fix|organi[sz]e|plan|put|rakho|rakh|banao|"
    rf"tay|रखो|रखें|रख|बुक|तय|बनाओ|बनाइए|शेड्यूल|सेट|ಹಾಕಿ|ಸೆಟ್|ನಿಗದಿ|ಇಡಿ|ಏರ್ಪಡಿಸಿ){_B1}",
    re.IGNORECASE,
)
_READ_VERB = re.compile(
    rf"{_B0}(?:check|show|see|view|read|open|what|whats|what's|list|any|do\s+i\s+have|upcoming|"
    rf"देख[\wऀ-ॿ\u0c80-\u0cff]*|दिखा[\wऀ-ॿ\u0c80-\u0cff]*|क्या|ಏನಿದೆ|ತೋರಿಸು|ನೋಡಿ){_B1}",
    re.IGNORECASE,
)
_AVAILABILITY = re.compile(
    rf"{_B0}(?:availability|available|am\s+i\s+free|are\s+we\s+free|free\s+(?:at|on|for)|"
    rf"फ्री|खाली|ಖಾಲಿ|ಲಭ್ಯ){_B1}",
    re.IGNORECASE,
)
_EVENT_NOUN = re.compile(
    rf"{_B0}(meetings?|appointments?|events?|calls?|catch-?ups?|syncs?|interviews?|stand-?ups?|"
    rf"मीटिंग|बैठक|अपॉइंटमेंट|इवेंट|ಮೀಟಿಂಗ್[\wऀ-ॿ\u0c80-\u0cff]*|ಸಭೆ[\wऀ-ॿ\u0c80-\u0cff]*|ಅಪಾಯಿಂಟ್‌?ಮೆಂಟ್){_B1}",
    re.IGNORECASE,
)
_NOUN_LABEL = {
    "meeting": "Meeting",
    "appointment": "Appointment",
    "event": "Event",
    "call": "Call",
    "catchup": "Catch-up",
    "catch-up": "Catch-up",
    "sync": "Sync",
    "interview": "Interview",
    "standup": "Standup",
    "stand-up": "Standup",
    "मीटिंग": "Meeting",
    "बैठक": "Meeting",
    "अपॉइंटमेंट": "Appointment",
    "इवेंट": "Event",
}
_STOP_AFTER_NAME = (
    r"tomorrow|today|tonight|on|at|for|in|next|this|from|by|to|and|then|about|regarding|morning|"
    r"afternoon|evening|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday|please"
)
_WITH_NAME = re.compile(
    rf"\bwith\s+((?!(?:{_STOP_AFTER_NAME})\b)[^\W\d_][\w.'\u2019-]*"
    rf"(?:\s+(?!(?:{_STOP_AFTER_NAME})\b)[^\W\d_][\w.'\u2019-]*){{0,2}})",
    re.IGNORECASE,
)
_HI_WITH = re.compile(
    rf"(\S+)\s+(?:के\s+साथ|ke\s+saath|ke\s+sath)(?!{_W})",
    re.IGNORECASE,
)
_KN_WITH = re.compile(r"(\S+)\s+ಜೊತೆ")
_TOPIC = re.compile(
    r"\b(?:about|regarding)\s+([^,;]+?)(?=\s+(?:tomorrow|today|on|at|for|with)\b|$)", re.I
)
_DURATION = re.compile(
    rf"(?:{_B0}for\s+(?P<n1>\d{{1,3}}|an?|one|two|three|half\s+an?)\s*(?P<u1>minutes?|mins?|hours?|hrs?)"
    rf"|(?P<n2>\d{{1,3}})\s*(?P<u2>मिनट|घंटे|घंटा|ghante|ghanta|minute|ನಿಮಿಷ|ಗಂಟೆ)\s*(?:के\s+लिए|ke\s+liye|ಗಾಗಿ)?)",
    re.IGNORECASE,
)
_DUR_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


def _minutes(clause: str) -> int:
    for match in _DURATION.finditer(clause):
        number = match.group("n1") or match.group("n2")
        unit = (match.group("u1") or match.group("u2")).lower()
        # Hindi/Kannada "7 बजे"-like numbers are not durations unless they carry "for": require it.
        if match.group("n2") and not re.search(
            r"(?:के\s+लिए|ke\s+liye|ಗಾಗಿ)", clause[match.start() : match.end() + 12]
        ):
            continue
        if number.lower().startswith("half"):
            return 30 if "hour" in unit or "hr" in unit else 15
        amount = _DUR_WORDS.get(number.lower()) or int(number)
        hours = unit.startswith(("hour", "hr", "घं", "ಗಂ", "ghan"))
        total = amount * 60 if hours else amount
        if 5 <= total <= 480:
            return total
    return 30


def _slot(clause: str, now: datetime) -> tuple[str, str] | None:
    """(YYYY-MM-DD, HH:MM) for the date and time named in the clause, else None."""
    parsed = parse_when(clause, now)
    if parsed is None:
        return None
    due = parsed.due
    if not _EXPLICIT_CLOCK.search(clause):
        for pattern, hour in _PERIOD_HOURS:
            if pattern.search(clause):
                due = due.replace(hour=hour, minute=0)
                break
    return due.date().isoformat(), due.strftime("%H:%M")


def _event_title(clause: str) -> str:
    noun = _EVENT_NOUN.search(clause)
    label = _NOUN_LABEL.get(noun.group(1).lower().rstrip("s"), "Meeting") if noun else "Meeting"
    if noun and noun.group(1).lower() in _NOUN_LABEL:
        label = _NOUN_LABEL[noun.group(1).lower()]
    who: str | None = None
    match = _WITH_NAME.search(clause)
    if match:
        who = match.group(1)
    else:
        for pattern in (_HI_WITH, _KN_WITH):
            other = pattern.search(clause)
            if other:
                who = other.group(1)
                break
    if who:
        who = _tidy(who)
        if who.lower() in {"me", "my", "the"} or "@" in who:
            who = None
    if who:
        return f"{label} with {_cap(who)}"[:200]
    topic = _TOPIC.search(clause)
    if topic and _tidy(topic.group(1)):
        return f"{label} about {_tidy(topic.group(1))}"[:200]
    return label


def _calendar(clause: str, now: datetime, out: RulesResult) -> None:
    creating = bool(_CREATE_VERB.search(clause)) and bool(_EVENT_NOUN.search(clause))
    noun_with = bool(_EVENT_NOUN.search(clause)) and bool(
        _WITH_NAME.search(clause) or _HI_WITH.search(clause) or _KN_WITH.search(clause)
    )
    slot = _slot(clause, now)
    if _AVAILABILITY.search(clause) and not creating:
        if slot is None:
            out.clarifications.append("Which day and time should I check?")
            return
        out.tasks.append(
            {
                "kind": "calendar.availability",
                "args": {"date": slot[0], "start": slot[1], "minutes": _minutes(clause)},
            }
        )
        return
    if creating or (noun_with and not _READ_VERB.search(clause)):
        if slot is None and out.tasks and out.tasks[-1]["kind"] == "calendar.availability":
            earlier = out.tasks[-1]["args"]
            if isinstance(earlier, dict):
                slot = (str(earlier["date"]), str(earlier["start"]))  # pyright: ignore[reportUnknownArgumentType]
        if slot is None:
            out.clarifications.append("When should I schedule it?")
            return
        out.tasks.append(
            {
                "kind": "calendar.create",
                "args": {
                    "title": _event_title(clause),
                    "date": slot[0],
                    "start": slot[1],
                    "minutes": _minutes(clause),
                    "attendees": [],
                },
            }
        )
        return
    if _READ_VERB.search(clause) or re.search(r"calendar|schedule|कैलेंडर|ಕ್ಯಾಲೆಂಡರ್", clause, re.I):
        wide = re.search(
            r"upcoming|this\s+week|next\s+(?:few\s+)?days|tomorrow|कल|ನಾಳೆ|इस\s+हफ्ते", clause, re.I
        )
        if wide is None:
            out.tasks.append({"kind": "calendar.today", "args": {}})
            return
        days = 7 if re.search(r"week|हफ्ते", clause, re.I) else 2
        number = re.search(r"next\s+(\d{1,2})\s+days", clause, re.I)
        if number:
            days = max(1, min(14, int(number.group(1))))
        out.tasks.append({"kind": "calendar.upcoming", "args": {"days": days}})


_IMPORTANT = re.compile(
    rf"{_B0}(?:important|urgent|priority|zaruri|zaroori|jaruri|जरूरी|ज़रूरी|महत्वपूर्ण|ಮುಖ್ಯ[\wऀ-ॿ\u0c80-\u0cff]*|ತುರ್ತು){_B1}",
    re.IGNORECASE,
)
_NEEDS_REPLY = re.compile(
    r"(?:need|needs|needing|waiting\s+for|awaiting|require|requires)\s+(?:a\s+|an\s+|my\s+)?"
    r"(?:reply|response|answer)|unanswered|not\s+replied|haven'?t\s+replied|reply\s+pending"
    r"|जवाब\s+(?:चाहिए|देना)|ಉತ್ತರ\s+ಬೇಕಾದ",
    re.IGNORECASE,
)
_COMPOSE = re.compile(
    rf"{_B0}(?:draft|write|compose|reply|respond|send|likh[\wऀ-ॿ\u0c80-\u0cff]*|लिख[\wऀ-ॿ\u0c80-\u0cff]*|ಬರೆ[\wऀ-ॿ\u0c80-\u0cff]*|ಕಳುಹಿಸಿ){_B1}",
    re.IGNORECASE,
)
_SEARCH_EN = re.compile(
    r"(?:search|find|look\s+for|look\s+up|show|any)\s+(?:for\s+)?(?:my\s+)?(?:e-?mails?|mails?)"
    r"\s+(?:from|about|regarding|with|for)\s+([^,;]+)",
    re.IGNORECASE,
)
_SEARCH_HI = re.compile(r"(\S+)\s+(?:का|के|की|से|ka|ke|ki|se)\s+(?:ईमेल|मेल|email|mail)", re.I)
_SEARCH_KN = re.compile(r"(\S+)\s+(?:ಅವರ|ಇಂದ)\s*(?:ಇಮೇಲ್|ಮೇಲ್)")
_DRAFT_EN = [
    re.compile(
        r"(?:draft|write|compose|send)\s+(?:an?\s+)?(?:e-?mail|mail)\s+to\s+(?P<n>.+?)"
        r"(?:\s+(?:saying|that|about|asking|regarding)\s+(?P<b>.+))?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:e-?mail|mail)\s+(?P<n>.+?)\s+(?:saying|that|about|asking|regarding)\s+(?P<b>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:reply|respond)\s+to\s+(?P<n>.+?)(?:['\u2019]s)?(?:\s+(?:e-?mail|mail))?"
        r"(?:\s+(?:saying|that|with|about)\s+(?P<b>.+))?$",
        re.IGNORECASE,
    ),
]


def _email(clause: str, out: RulesResult) -> None:
    if _NEEDS_REPLY.search(clause):
        out.tasks.append({"kind": "gmail.needs_reply", "args": {}})
        return
    if (_COMPOSE.search(clause) or _DRAFT_EN[1].search(clause)) and not re.search(
        r"(?:check|read|show|see|any)\b.*(?:e-?mails?|mails?|inbox)", clause, re.I
    ):
        name: str | None = None
        body: str | None = None
        for pattern in _DRAFT_EN:
            match = pattern.search(clause)
            if match:
                name, body = match.group("n"), match.groupdict().get("b")
                break
        if name is None and re.search(r"[ऀ-ॿಀ-೿]|\b(?:ko|ki)\b", clause):
            name, body = _message_parts(clause)
        good = _good_name(name or "", allow_phone=False)
        if good is None:
            out.clarifications.append("Who should I write the email to? Please say a contact name.")
            return
        if not body or not _tidy(body):
            out.clarifications.append(f"What should the email to {good} say?")
            return
        out.tasks.append(
            {"kind": "gmail.draft_reply", "args": {"to": good, "intent": _tidy(body)[:500]}}
        )
        return
    search = _SEARCH_EN.search(clause)
    query = search.group(1) if search else None
    if query is None:
        other = _SEARCH_HI.search(clause) or _SEARCH_KN.search(clause)
        query = other.group(1) if other else None
    if query is not None and _tidy(query):
        out.tasks.append({"kind": "gmail.search", "args": {"query": _tidy(query)[:200]}})
        return
    if _IMPORTANT.search(clause) or re.search(
        r"check|read|see|show|look|dekho|dekh|padho|देख|पढ़|दिखा|ನೋಡಿ|ತೋರಿಸು|ಓದಿ", clause, re.I
    ):
        out.tasks.append({"kind": "gmail.important", "args": {}})


def _whatsapp(clause: str, out: RulesResult) -> None:
    name, body = _message_parts(clause)
    good = _good_name(name or "", allow_phone=True)
    if good is None:
        out.clarifications.append("Who should I message?")
        return
    if not body:
        out.clarifications.append(f"What should I say to {good}?")
        return
    out.tasks.append({"kind": "whatsapp.send", "args": {"to": good, "body": body[:1000]}})


_REMIND_EN = re.compile(
    r"(?:remind\s+me|set\s+(?:a\s+|an\s+)?reminder|add\s+(?:a\s+|an\s+)?reminder|"
    r"create\s+(?:a\s+|an\s+)?reminder)\b\s*(?:to\b|that\b|about\b|for\b|:)?\s*",
    re.IGNORECASE,
)


def _reminder(clause: str, now: datetime, out: RulesResult) -> None:
    match = _REMIND_EN.search(clause)
    tail = clause[match.end() :] if match is not None else (reminder_request(clause) or "")
    tail = _tidy(tail)
    if not tail:
        out.clarifications.append("What should I remind you about?")
        return
    parsed = parse_when(tail, now)
    text, when_text = tail, None
    if parsed is not None and parsed.text:
        remaining = parsed.text.lower().split()
        diff: list[str] = []
        for word in tail.split():
            if word.lower() in remaining:
                remaining.remove(word.lower())
            else:
                diff.append(word)
        candidate = " ".join(diff)
        if candidate and parse_when(candidate, now) is not None:
            text, when_text = parsed.text, candidate
    out.tasks.append(
        {"kind": "reminder.create", "args": {"text": text[:500], "when_text": when_text}}
    )


def _first_category(clause: str) -> str | None:
    if reminder_request(clause) is not None:
        return "reminder"  # "<what and when> yaad dilao" names other things before the verb
    if _REPLY_TO.search(clause):
        return "email"  # "Reply to Rahul saying I'll attend the meeting" is mail, not calendar
    positions: list[tuple[int, str]] = []
    for name, pattern in (
        ("reminder", _REMINDER),
        ("whatsapp", _WHATSAPP),
        ("message", _MESSAGE),
        ("day", _DAY),
        ("email", _EMAIL),
        ("calendar", _CALENDAR),
    ):
        found = pattern.search(clause)
        if found:
            positions.append((found.start(), name))
    if not positions:
        return None
    positions.sort()
    return positions[0][1]


def plan_with_rules(text: str, now: datetime) -> RulesResult:
    """Tasks (raw dicts, validated later by the caller) and questions for what was unclear."""
    out = RulesResult()
    for clause in _split(text):
        category = _first_category(clause)
        if category is None:
            continue
        if category == "reminder":
            _reminder(clause, now, out)
        elif category in {"whatsapp", "message"}:
            _whatsapp(clause, out)
        elif category == "day":
            out.tasks.append({"kind": "day.plan", "args": {}})
        elif category == "email":
            _email(clause, out)
        elif category == "calendar":
            _calendar(clause, now, out)
    return out
