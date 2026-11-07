"""The reminder date parser: English, Hindi and Kannada phrases, in Indian Standard Time."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.personal.dates import IST, parse_when

# Saturday 7 November 2026, 16:00 IST.
NOW = datetime(2026, 11, 7, 16, 0, tzinfo=IST)


def at(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(
    ("phrase", "due", "text"),
    [
        # English
        ("Call mom tomorrow at 7 pm", at(8, 19), "Call mom"),
        ("call mom tomorrow at 7pm", at(8, 19), "call mom"),
        ("remind me to drink water in 2 hours", at(7, 18), "drink water"),
        ("stretch in two hours", at(7, 18), "stretch"),
        ("tea in 30 minutes", at(7, 16, 30), "tea"),
        ("tea in an hour", at(7, 17), "tea"),
        ("pay rent in 3 days", at(10, 16), "pay rent"),
        ("pay rent in 3 days at 10 am", at(10, 10), "pay rent"),
        ("renew in 2 weeks", at(21, 16), "renew"),
        ("standup next Monday 9am", at(9, 9), "standup"),
        ("gym on Sunday at 6:30 am", at(8, 6, 30), "gym"),
        ("dinner day after tomorrow evening", at(9, 18), "dinner"),
        ("walk tomorrow morning", at(8, 9), "walk"),
        ("walk this evening", at(7, 18), "walk"),
        ("tablets tonight", at(7, 21), "tablets"),
        ("tablets tonight at 10", at(7, 22), "tablets"),
        ("lunch tomorrow afternoon", at(8, 14), "lunch"),
        ("meeting tomorrow", at(8, 9), "meeting"),
        ("meeting tomorrow at 19:00", at(8, 19), "meeting"),
        ("meeting at 17:30", at(7, 17, 30), "meeting"),
        ("call at 7 in the evening", at(7, 19), "call"),
        # midnight and 12 am / pm
        ("flight tomorrow at 12 am", at(8, 0), "flight"),
        ("lunch tomorrow at 12 pm", at(8, 12), "lunch"),
        ("lunch tomorrow at 12:30 pm", at(8, 12, 30), "lunch"),
        ("sleep at midnight", at(8, 0), "sleep"),
        ("sleep tomorrow midnight", at(8, 0), "sleep"),
        ("sleep tonight at 12", at(8, 0), "sleep"),
        ("lunch at noon", at(8, 12), "lunch"),
        ("call tomorrow at 00:30", at(8, 0, 30), "call"),
        # past times roll to the next day only when no date was given
        ("pill at 3 pm", at(8, 15), "pill"),
        ("pill at 5 pm", at(7, 17), "pill"),
        ("pill this morning", None, None),
        ("pill today at 3 pm", None, None),
        ("pill morning", at(8, 9), "pill"),
        # Hindi, romanised
        ("kal subah 7 baje uthna", at(8, 7), "uthna"),
        ("aaj shaam 6 baje chai", at(7, 18), "chai"),
        ("aaj raat 9 baje dawai", at(7, 21), "dawai"),
        ("parso 10:30 am doctor", at(9, 10, 30), "doctor"),
        ("2 ghante baad paani", at(7, 18), "paani"),
        ("30 minute mein chai", at(7, 16, 30), "chai"),
        ("3 din baad bill", at(10, 16), "bill"),
        ("somvar subah 8 baje gym", at(9, 8), "gym"),
        ("kal dopahar", at(8, 14), ""),
        # Hindi, Devanagari
        ("कल शाम 6 बजे दवा", at(8, 18), "दवा"),
        ("आज रात 10 बजे सोना", at(7, 22), "सोना"),
        ("परसों सुबह 9 बजे मीटिंग", at(9, 9), "मीटिंग"),
        ("2 घंटे बाद पानी", at(7, 18), "पानी"),
        ("रविवार शाम 5 बजे पूजा", at(8, 17), "पूजा"),
        ("कल", at(8, 9), ""),
        # Kannada
        ("ನಾಳೆ ಸಂಜೆ 7 ಗಂಟೆಗೆ ಅಮ್ಮನಿಗೆ ಕರೆ", at(8, 19), "ಅಮ್ಮನಿಗೆ ಕರೆ"),
        ("ಇಂದು ರಾತ್ರಿ 9 ಗಂಟೆಗೆ ಔಷಧ", at(7, 21), "ಔಷಧ"),
        ("ನಾಡಿದ್ದು ಬೆಳಿಗ್ಗೆ 8 ಗಂಟೆಗೆ ಪೂಜೆ", at(9, 8), "ಪೂಜೆ"),
        ("2 ಗಂಟೆಯಲ್ಲಿ ನೀರು", at(7, 18), "ನೀರು"),
        ("30 ನಿಮಿಷದಲ್ಲಿ ಚಹಾ", at(7, 16, 30), "ಚಹಾ"),
        ("ಭಾನುವಾರ ಸಂಜೆ 5 ಗಂಟೆಗೆ ದೇವಸ್ಥಾನ", at(8, 17), "ದೇವಸ್ಥಾನ"),
        ("ನಾಳೆ ಮಧ್ಯಾಹ್ನ", at(8, 14), ""),
    ],
)
def test_phrases(phrase: str, due: datetime | None, text: str | None) -> None:
    parsed = parse_when(phrase, NOW)
    if due is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert parsed.due == due
        assert parsed.text == text


@pytest.mark.parametrize(
    "phrase",
    [
        "buy milk",
        "",
        "call mom at 7",  # am or pm?
        "call mom at 7:30",
        "call mom tomorrow at 7",
        "7 baje",
        "tomorrow today",
        "tomorrow and monday",
        "morning and evening",
        "in 2 hours tomorrow",
        "in 2 hours at 5 pm",
        "in 0 minutes",
        "in 5000 days",
        "at 25:00",
        "tomorrow at 13 pm",
        "tomorrow at 0 pm",
        "tomorrow at 7:75 pm",
        "two apples",
        "buy 2 apples",
        "tomorrow at 5 pm and 6 pm",
    ],
)
def test_unclear_or_missing_times_give_none(phrase: str) -> None:
    assert parse_when(phrase, NOW) is None


def test_weekday_names_pick_the_next_one() -> None:
    # NOW is a Saturday.
    saturday = parse_when("call on Saturday at 6 pm", NOW)
    assert saturday is not None and saturday.due == at(7, 18)
    later = parse_when("call on Saturday at 9 am", NOW)
    assert later is not None and later.due == at(14, 9)
    nxt = parse_when("call next Saturday at 6 pm", NOW)
    assert nxt is not None and nxt.due == at(14, 18)
    friday = parse_when("call friday morning", NOW)
    assert friday is not None and friday.due == at(13, 9)


def test_result_is_in_ist_whatever_zone_now_has() -> None:
    from zoneinfo import ZoneInfo

    utc_now = NOW.astimezone(ZoneInfo("UTC"))
    parsed = parse_when("tea tomorrow at 8 am", utc_now)
    assert parsed is not None
    assert parsed.due == at(8, 8)
    assert parsed.due.utcoffset() is not None
    assert str(parsed.due.utcoffset()) == "5:30:00"
    naive = parse_when("tea tomorrow at 8 am", NOW.replace(tzinfo=None))
    assert naive is not None and naive.due == at(8, 8)


def test_month_end_rolls_over() -> None:
    now = datetime(2026, 12, 31, 23, 0, tzinfo=IST)
    parsed = parse_when("party tomorrow at 1 am", now)
    assert parsed is not None
    assert parsed.due == datetime(2027, 1, 1, 1, 0, tzinfo=IST)


def test_text_is_not_mangled_inside_words() -> None:
    # "raat" contains "at"; "Saturnalia" is not a weekday.
    parsed = parse_when("raat 9 baje khaana", NOW)
    assert parsed is not None and parsed.text == "khaana"
    assert parse_when("read Saturnalia", NOW) is None
