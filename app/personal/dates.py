"""Deterministic date and time phrases for reminders, in Indian Standard Time.

Pure: the caller supplies `now`; nothing here reads the clock. Supports English, Hindi
(Devanagari and romanised) and Kannada script. When a phrase is unclear (a bare "at 7" with no
am/pm or part of the day, two different days, a past date) the answer is None rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_DEFAULT_HOURS = {"morning": 9, "afternoon": 14, "evening": 18, "night": 21}
_DEFAULT_DATE_ONLY_HOUR = 9
_MAX_DAYS = 365
_MAX_MINUTES = 60 * 24 * 30

_W = r"[\wऀ-ॿಀ-೿]"
_B0 = rf"(?<!{_W})"
_B1 = rf"(?!{_W})"

_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUM = r"(\d{1,4}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"

_UNIT = {
    "min": "m",
    "mins": "m",
    "minute": "m",
    "minutes": "m",
    "मिनट": "m",
    "ನಿಮಿಷ": "m",
    "hour": "h",
    "hours": "h",
    "hr": "h",
    "hrs": "h",
    "ghante": "h",
    "ghanta": "h",
    "ghantey": "h",
    "घंटे": "h",
    "घंटा": "h",
    "घण्टे": "h",
    "ಗಂಟೆ": "h",
    "day": "d",
    "days": "d",
    "din": "d",
    "दिन": "d",
    "ದಿನ": "d",
    "week": "w",
    "weeks": "w",
    "hafte": "w",
    "हफ्ते": "w",
    "हफ़्ते": "w",
}
_UNITS = "|".join(sorted((re.escape(u) for u in _UNIT), key=len, reverse=True))

_RELATIVE = [
    re.compile(
        rf"{_B0}(?:in|after)\s+{_NUM}\s*({_UNITS}){_B1}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_B0}{_NUM}\s*({_UNITS})\s*(?:ke\s+)?(?:baad|bad|mein|me|के\s+बाद|बाद|में){_B1}",
        re.IGNORECASE,
    ),
    re.compile(rf"{_B0}{_NUM}\s*(ನಿಮಿಷ|ಗಂಟೆ|ದಿನ)(?:ಗಳ|ಯ|ದ)?\s*(?:ಲ್ಲಿ|ನಂತರ|ಆದ ಮೇಲೆ)"),
]

_DAY_WORDS: dict[str, int] = {
    "today": 0,
    "aaj": 0,
    "आज": 0,
    "ಇಂದು": 0,
    "tomorrow": 1,
    "tmrw": 1,
    "kal": 1,
    "कल": 1,
    "ನಾಳೆ": 1,
    "parso": 2,
    "parson": 2,
    "परसों": 2,
    "परसो": 2,
    "ನಾಡಿದ್ದು": 2,
}
_DAYS = re.compile(
    rf"{_B0}(day\s+after\s+(?:tomorrow|tmrw)|"
    + "|".join(re.escape(w) for w in sorted(_DAY_WORDS, key=len, reverse=True))
    + rf")(?:ಗೆ)?{_B1}",
    re.IGNORECASE,
)

_WEEKDAYS: dict[str, int] = {}
for _index, _names in enumerate(
    (
        ("monday", "somvar", "somwar", "सोमवार", "ಸೋಮವಾರ"),
        ("tuesday", "mangalvar", "mangalwar", "मंगलवार", "ಮಂಗಳವಾರ"),
        ("wednesday", "budhvar", "budhwar", "बुधवार", "ಬುಧವಾರ"),
        ("thursday", "guruvar", "guruwar", "brihaspativar", "गुरुवार", "बृहस्पतिवार", "ಗುರುವಾರ"),
        ("friday", "shukravar", "shukrawar", "शुक्रवार", "ಶುಕ್ರವಾರ"),
        ("saturday", "shanivar", "shaniwar", "शनिवार", "ಶನಿವಾರ"),
        ("sunday", "ravivar", "itwar", "itvar", "रविवार", "ಭಾನುವಾರ"),
    )
):
    for _name in _names:
        _WEEKDAYS[_name] = _index
_WEEKDAY_RE = re.compile(
    rf"{_B0}(?:(?:next|coming|this|agle|agla|अगले|अगला|ಮುಂದಿನ)\s+)?("
    + "|".join(re.escape(w) for w in sorted(_WEEKDAYS, key=len, reverse=True))
    + rf")(?:ದಂದು|ದ)?{_B1}",
    re.IGNORECASE,
)

_PERIODS: dict[str, str] = {
    "morning": "morning",
    "subah": "morning",
    "सुबह": "morning",
    "ಬೆಳಿಗ್ಗೆ": "morning",
    "ಬೆಳಗ್ಗೆ": "morning",
    "afternoon": "afternoon",
    "dopahar": "afternoon",
    "दोपहर": "afternoon",
    "ಮಧ್ಯಾಹ್ನ": "afternoon",
    "evening": "evening",
    "shaam": "evening",
    "sham": "evening",
    "शाम": "evening",
    "ಸಂಜೆ": "evening",
    "night": "night",
    "raat": "night",
    "रात": "night",
    "ರಾತ್ರಿ": "night",
    "tonight": "night",
}
_PERIOD_RE = re.compile(
    rf"{_B0}(?:(this|in\s+the|at)\s+)?("
    + "|".join(re.escape(w) for w in sorted(_PERIODS, key=len, reverse=True))
    + rf"){_B1}",
    re.IGNORECASE,
)

_SUFFIX = r"am|pm|a\.m\.|p\.m\.|baje|बजे|ಗಂಟೆಗೆ|ಗಂಟೆ|o'clock|oclock"
_CLOCKS = [
    # 7 pm, 7:30 am, 7 baje, 7 ಗಂಟೆಗೆ
    re.compile(rf"{_B0}(?:(?:at|@|by)\s*)?(\d{{1,2}})(?::(\d{{2}}))?\s*({_SUFFIX}){_B1}", re.I),
    # 19:00, 7:30
    re.compile(rf"{_B0}(?:(?:at|@|by)\s*)?(\d{{1,2}}):(\d{{2}})(){_B1}"),
    # at 7
    re.compile(rf"{_B0}(?:at|@)\s*(\d{{1,2}})()()(?![\d:]){_B1}", re.I),
]
_MIDNIGHT = re.compile(rf"{_B0}midnight{_B1}", re.I)
_NOON = re.compile(rf"{_B0}noon{_B1}", re.I)
_REMIND = re.compile(r"^\s*remind\s+me\s+(?:to|that|about)?\s*", re.I)
# Absolute dates. A bare "1/2" is not a date (it may be half a kilo): the numeric form needs "on".
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th)?"
_ABS_DMY = re.compile(
    rf"{_B0}(?:on\s+)?(\d{{1,2}}){_ORDINAL}\s+(?:of\s+)?({_MONTH_NAMES})\.?(?:,?\s+(\d{{4}}))?{_B1}",
    re.I,
)
_ABS_MDY = re.compile(
    rf"{_B0}(?:on\s+)?({_MONTH_NAMES})\.?\s+(\d{{1,2}}){_ORDINAL}(?:,?\s+(\d{{4}}))?{_B1}", re.I
)
_ABS_NUM = re.compile(rf"{_B0}on\s+(\d{{1,2}})[/-](\d{{1,2}})(?:[/-](\d{{4}}))?{_B1}", re.I)
_ABS_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_EDGE_WORDS = {"at", "on", "by", "for", "in", "to", "this", "next", "coming", "ko", "को", "mein"}


@dataclass(frozen=True)
class ParsedWhen:
    due: datetime
    text: str


class _Ambiguous(Exception):
    pass


def _number(token: str) -> int:
    lowered = token.lower()
    return _WORDS[lowered] if lowered in _WORDS else int(lowered)


def _year(token: str | None) -> int | None:
    return int(token) if token else None


def _tidy(text: str) -> str:
    text = _REMIND.sub("", re.sub(r"\s+", " ", text)).strip(" \t,.;:-")
    words = text.split(" ")
    while words and words[-1].lower() in _EDGE_WORDS:
        words.pop()
    while words and words[0].lower() in _EDGE_WORDS and len(words) > 1:
        words.pop(0)
    return " ".join(words).strip(" ,.;:-")


def _resolve_hour(hour: int, minute: int, suffix: str, period: str | None) -> tuple[int, bool]:
    """Hour of day, and whether it names the midnight that ends the evening ("night 12")."""
    if minute > 59 or hour > 23:
        raise _Ambiguous
    marker = suffix.lower().replace(".", "")
    if marker in {"am", "pm"}:
        if not 1 <= hour <= 12:
            raise _Ambiguous
        return (hour % 12) + (12 if marker == "pm" else 0), False
    if hour == 0 or hour >= 13:
        return hour, False
    if period is None:
        raise _Ambiguous
    if period == "morning":
        if hour == 12:
            raise _Ambiguous
        return hour, False
    if period == "afternoon":
        if hour == 12 or hour <= 6:
            return (hour % 12) + 12, False
        raise _Ambiguous
    if period == "evening":
        if hour == 12:
            raise _Ambiguous
        return hour + 12, False
    if hour == 12:
        return 0, True
    return (hour + 12, False) if hour >= 6 else (hour, False)


def _parse(text: str, now: datetime) -> ParsedWhen | None:
    now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    work = text

    # An absolute date: "10 November", "Nov 10th", "on 10/11", "2026-11-10".
    absolute: list[tuple[int, int, int | None]] = []  # (day, month, year or None)

    def take_dmy(match: re.Match[str]) -> str:
        absolute.append(
            (int(match.group(1)), _MONTHS[match.group(2).lower()], _year(match.group(3)))
        )
        return " "

    def take_mdy(match: re.Match[str]) -> str:
        absolute.append(
            (int(match.group(2)), _MONTHS[match.group(1).lower()], _year(match.group(3)))
        )
        return " "

    def take_num(match: re.Match[str]) -> str:
        absolute.append((int(match.group(1)), int(match.group(2)), _year(match.group(3))))
        return " "

    def take_iso(match: re.Match[str]) -> str:
        absolute.append((int(match.group(3)), int(match.group(2)), int(match.group(1))))
        return " "

    work = _ABS_ISO.sub(take_iso, work)
    work = _ABS_DMY.sub(take_dmy, work)
    work = _ABS_MDY.sub(take_mdy, work)
    work = _ABS_NUM.sub(take_num, work)
    if len(absolute) > 1:
        return None

    # In N minutes / hours / days.
    relative: list[tuple[int, str]] = []
    for pattern in _RELATIVE:

        def take(match: re.Match[str]) -> str:
            relative.append((_number(match.group(1)), _UNIT[match.group(2).lower()]))
            return " "

        work = pattern.sub(take, work)
    if len(relative) > 1:
        return None

    # Clock times.
    clocks: list[tuple[int, int, str]] = []
    for pattern in _CLOCKS:

        def take_clock(match: re.Match[str]) -> str:
            clocks.append((int(match.group(1)), int(match.group(2) or 0), match.group(3) or ""))
            return " "

        work = pattern.sub(take_clock, work)
    named: list[tuple[int, int, bool]] = []
    for pattern, hour in ((_MIDNIGHT, 0), (_NOON, 12)):

        def take_named(_match: re.Match[str], hour: int = hour) -> str:
            named.append((hour, 0, hour == 0))
            return " "

        work = pattern.sub(take_named, work)
    if len(clocks) + len(named) > 1:
        return None

    # Days.
    days: list[int] = []

    def take_day(match: re.Match[str]) -> str:
        word = match.group(1).lower()
        days.append(2 if word.startswith("day") else _DAY_WORDS[word])
        return " "

    work = _DAYS.sub(take_day, work)
    weekdays: list[tuple[int, bool]] = []

    def take_weekday(match: re.Match[str]) -> str:
        prefixed = match.group(0).lower() != match.group(1).lower()
        weekdays.append((_WEEKDAYS[match.group(1).lower()], prefixed))
        return " "

    work = _WEEKDAY_RE.sub(take_weekday, work)

    # Part of the day.
    periods: list[str] = []
    soft_today = False

    def take_period(match: re.Match[str]) -> str:
        nonlocal soft_today
        word = match.group(2).lower()
        periods.append(_PERIODS[word])
        if word == "tonight" or (match.group(1) or "").lower() == "this":
            soft_today = True
        return " "

    work = _PERIOD_RE.sub(take_period, work)
    if len(periods) > 1:
        return None
    period = periods[0] if periods else None

    if len(days) + len(weekdays) + len(absolute) > 1:
        return None
    if not (relative or clocks or named or days or weekdays or period or absolute):
        return None

    remainder = _tidy(work)

    if relative:
        amount, unit = relative[0]
        if amount < 1 or period or weekdays or days or absolute:
            return None
        if unit in {"m", "h"}:
            if clocks or named:
                return None
            minutes = amount * (60 if unit == "h" else 1)
            if minutes > _MAX_MINUTES:
                return None
            due = (now + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
            return ParsedWhen(due, remainder)
        offset = amount * (7 if unit == "w" else 1)
        if offset > _MAX_DAYS:
            return None
        days = [offset]

    explicit_date = bool(days or weekdays or soft_today or absolute)
    try:
        if clocks:
            hour, minute, suffix = clocks[0]
            hour, next_day = _resolve_hour(hour, minute, suffix, period)
        elif named:
            hour, minute, next_day = named[0]
        elif period:
            hour, minute, next_day = _DEFAULT_HOURS[period], 0, False
        elif relative:
            hour, minute, next_day = now.hour, now.minute, False
        elif explicit_date:
            hour, minute, next_day = _DEFAULT_DATE_ONLY_HOUR, 0, False
        else:
            return None
    except _Ambiguous:
        return None

    today = now.date()
    if absolute:
        day, month, year = absolute[0]
        try:
            target = date(year or today.year, month, day)
            if year is None and target < today:
                target = date(today.year + 1, month, day)  # a date already gone means next year
        except ValueError:
            return None  # such as 31 February
        if (target - today).days > _MAX_DAYS:
            return None
    elif days:
        target = today + timedelta(days=days[0])
    elif weekdays:
        weekday, prefixed = weekdays[0]
        gap = (weekday - today.weekday()) % 7
        if gap == 0:
            later = datetime(today.year, today.month, today.day, hour, minute, tzinfo=IST)
            gap = 0 if (clocks or named or period) and later > now and not prefixed else 7
        target = today + timedelta(days=gap)
    else:
        target = today
    if next_day and soft_today and not days and not weekdays:
        target += timedelta(days=1)  # "tonight at 12" is the midnight that ends tonight
    due = datetime(target.year, target.month, target.day, hour, minute, tzinfo=IST)
    if due <= now:
        if explicit_date:
            return None
        due += timedelta(days=1)
    return ParsedWhen(due, remainder)


def names_a_day(text: str) -> bool:
    """True if the text names a day (tomorrow, Friday, 10 November), not only a clock time."""
    return any(
        pattern.search(text)
        for pattern in (_DAYS, _WEEKDAY_RE, _ABS_DMY, _ABS_MDY, _ABS_NUM, _ABS_ISO)
    )


def parse_when(text: str, now: datetime) -> ParsedWhen | None:
    """The due time named in `text` and the text left over, or None if there is no clear one."""
    return _parse(text, now)
