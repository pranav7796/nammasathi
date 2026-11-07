"""Spoken reminders: "remind me to ..." in three languages, through the real web app."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.personal.voice import reminder_request
from tests.test_accounts import EMAIL, PASSWORD, register, site


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remind me to call Amma tomorrow at 7 pm", "Remind me to call Amma tomorrow at 7 pm"),
        ("please remind me to pay rent", "please remind me to pay rent"),
        ("Set a reminder to buy milk tonight", "Set a reminder to buy milk tonight"),
        ("कल शाम 7 बजे माँ को फोन करना याद दिलाओ", "कल शाम 7 बजे माँ को फोन करना"),
        ("मुझे दूध लाना याद दिला दो", "दूध लाना"),
        ("kal 8 baje doodh lana yaad dilao", "kal 8 baje doodh lana"),
        ("ನಾಳೆ ಸಂಜೆ 7 ಗಂಟೆಗೆ ಅಮ್ಮನಿಗೆ ಕರೆ ಮಾಡಲು ನೆನಪಿಸಿ", "ನಾಳೆ ಸಂಜೆ 7 ಗಂಟೆಗೆ ಅಮ್ಮನಿಗೆ ಕರೆ ಮಾಡಲು"),
    ],
)
def test_clear_reminder_requests_are_recognised(text: str, expected: str) -> None:
    assert reminder_request(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Two kilos of rice and one litre of milk",
        "Yes",
        "I want something for dinner",
        "Book a table for two in Indiranagar tomorrow at 7 PM",
        "do not remind me about anything",  # not a request to save one
        "ಹೌದು",
        "हाँ",
        "",
    ],
)
def test_everything_else_is_left_to_the_assistant(text: str) -> None:
    assert reminder_request(text) is None


async def start(client: httpx.AsyncClient, language: str = "en-IN") -> dict[str, str]:
    response = await client.post("/api/sessions", json={"language": language})
    assert response.status_code == 200, response.text
    return {"X-Session-ID": response.json()["session_id"]}


async def say(client: httpx.AsyncClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    response = await client.post("/api/session/text", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_signed_in_person_saves_a_reminder_by_voice(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        assert (await register(client)).status_code == 200
        headers = await start(client)
        reply = await say(client, headers, "Remind me to call Amma tomorrow at 7 pm")
        assert "Saved a reminder" in reply["assistant_text"]
        assert "can't alert you yet" in reply["assistant_text"]  # never implies it will ring
        widget = reply["ui"][0]
        assert widget["type"] == "reminder_list"
        assert [item["text"] for item in widget["items"]] == ["call Amma"]
        assert widget["items"][0]["due"] is not None
        listed = (await client.get("/api/personal/reminders")).json()["reminders"]
        assert [item["text"] for item in listed] == ["call Amma"]


async def test_a_repeated_request_does_not_duplicate(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        for _ in range(2):
            await say(client, headers, "Remind me to pay rent tomorrow at 9 am")
        listed = (await client.get("/api/personal/reminders")).json()["reminders"]
        assert len(listed) == 1


async def test_a_reminder_without_a_time_says_so(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        reply = await say(client, headers, "Remind me to renew the passport")
        assert "didn't find a time" in reply["assistant_text"]
        assert reply["ui"][0]["items"][0].get("due") is None


async def test_hindi_and_kannada_get_replies_in_their_own_language(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        hindi = await say(
            client, await start(client, "hi-IN"), "कल शाम 7 बजे माँ को फोन करना याद दिलाओ"
        )
        assert "रिमाइंडर सेव" in hindi["assistant_text"]
        kannada = await say(
            client, await start(client, "kn-IN"), "ನಾಳೆ ಸಂಜೆ 7 ಗಂಟೆಗೆ ಅಮ್ಮನಿಗೆ ಕರೆ ಮಾಡಲು ನೆನಪಿಸಿ"
        )
        assert "ರಿಮೈಂಡರ್" in kannada["assistant_text"]
        assert len((await client.get("/api/personal/reminders")).json()["reminders"]) == 2


async def test_someone_who_is_not_signed_in_is_asked_to_sign_in(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        headers = await start(client)
        reply = await say(client, headers, "Remind me to call Amma tomorrow at 7 pm")
        assert "sign in" in reply["assistant_text"].lower()
        assert not reply.get("ui")


async def test_reminders_belong_to_one_person(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as asha, s.browser() as ravi:
        await register(asha)
        await register(ravi, "ravi@example.com")
        await say(asha, await start(asha), "Remind me to call Amma tomorrow at 7 pm")
        theirs = (await ravi.get("/api/personal/reminders")).json()["reminders"]
        assert theirs == []
        reply = await say(ravi, await start(ravi), "Remind me to buy milk tomorrow at 8 am")
        assert [item["text"] for item in reply["ui"][0]["items"]] == ["buy milk"]


async def test_ordering_still_works_beside_reminders(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        await say(client, headers, "Remind me to call Amma tomorrow at 7 pm")
        order = await say(client, headers, "Two kilos of rice and one litre of milk")
        assert order["state"] in {"READBACK", "CLARIFYING"}
        assert "ui" not in order or not order["ui"]


async def test_personal_routes_need_a_sign_in(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        assert (await client.get("/api/personal/reminders")).status_code == 401
        assert (await client.get("/api/personal/notes")).status_code == 401
        assert EMAIL and PASSWORD
