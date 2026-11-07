"""The WhatsApp/ordering routing bug, and the isolation between workflows.

The bug: "Send a WhatsApp message" -> "Who should I message?" was a one-off string. The answer
("Rahul") reached the grocery assistant, which asked about groceries, and every reply carried the
grocery view (empty basket, Total 0, Demo order), so the page showed a shopping panel for WhatsApp.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from tests.test_accounts import register, site


async def start(client: httpx.AsyncClient, language: str = "en-IN") -> dict[str, str]:
    response = await client.post("/api/sessions", json={"language": language})
    assert response.status_code == 200, response.text
    return {"X-Session-ID": response.json()["session_id"]}


async def say(client: httpx.AsyncClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    response = await client.post("/api/session/text", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def settled(client: httpx.AsyncClient) -> dict[str, Any]:
    """Poll the board the way the page does, until nothing is pending or running."""
    for _ in range(60):
        data = (await client.get("/api/tasks/current")).json()
        if data["plan"] and not data["plan"]["active"]:
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("the plan never settled")


def kinds(reply: dict[str, Any]) -> list[str]:
    return [w["type"] for w in reply.get("ui", [])]


def workflow(reply: dict[str, Any]) -> str:
    return str(reply["workflow"]["kind"])


# The bug itself ---------------------------------------------------------------------------


async def test_a_whatsapp_question_is_answered_inside_the_whatsapp_workflow(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        asked = await say(client, headers, "Send a WhatsApp message")
        assert "Who should I message" in asked["assistant_text"]
        assert workflow(asked) == "whatsapp" and asked["workflow"]["waiting_for"] == "to"
        then = await say(client, headers, "Rahul")
        # It asks what to say to Rahul. It does not ask about groceries.
        assert "What should I say to Rahul" in then["assistant_text"]
        assert "groceries" not in then["assistant_text"].lower()
        assert workflow(then) == "whatsapp" and then["workflow"]["waiting_for"] == "body"
        done = await say(client, headers, "I'll be late")
        assert workflow(done) == "whatsapp" and done["plan"]["id"]
        board = await settled(client)
        assert "composer" in kinds(board)
        draft = next(w for w in board["ui"] if w["type"] == "composer")
        assert draft["channel"] == "message" and draft["body"] == "I'll be late"
        assert draft["state"] == "draft" and draft["demo"] is True


async def test_naming_the_person_keeps_the_name(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        reply = await say(client, headers, "Send Rahul a WhatsApp message")
        assert (
            "What should I say to Rahul" in reply["assistant_text"]
        )  # not "Who should I message?"
        assert reply["workflow"]["waiting_for"] == "body"


async def test_a_typed_number_is_accepted_as_the_recipient(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        await say(client, headers, "Send a WhatsApp message")
        await say(client, headers, "+91 98765 43210")
        await say(client, headers, "Running late")
        draft = next(w for w in (await settled(client))["ui"] if w["type"] == "composer")
        assert draft["to"] == "+919876543210"


async def test_a_workflow_reply_never_carries_the_shopping_view(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        for text in ("Send a WhatsApp message", "Rahul", "I'll be late"):
            reply = await say(client, headers, text)
            assert workflow(reply) == "whatsapp"
            assert reply["show_readback"] is False and reply["can_confirm"] is False
            assert reply["options"] == []


async def test_cancelling_a_question_drops_it_and_returns_to_normal(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        await say(client, headers, "Send a WhatsApp message")
        dropped = await say(client, headers, "never mind")
        assert "dropped" in dropped["assistant_text"]
        order = await say(client, headers, "Two kilos of rice and one litre of milk")
        assert workflow(order) == "order" and order["state"] in {"READBACK", "CLARIFYING"}


async def test_a_new_request_is_not_swallowed_as_an_answer(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        await say(client, headers, "Send a WhatsApp message")
        moved = await say(client, headers, "Check my important emails")
        assert workflow(moved) == "email"
        assert "email_list" in kinds(await settled(client))
        assert "Who should I message" not in moved["assistant_text"]


async def test_an_answer_that_is_really_an_order_is_not_taken_as_a_name(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        await say(client, headers, "Send a WhatsApp message")
        order = await say(client, headers, "I want something for dinner")
        assert workflow(order) == "order"
        assert "who" not in order["assistant_text"].lower()


# Each workflow has its own panel -----------------------------------------------------------------


async def test_each_request_lands_in_its_own_workflow(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        cases = [
            ("Send Rahul a WhatsApp message saying I'll be late", "whatsapp"),
            ("Check my important emails", "email"),
            ("Schedule a meeting with Rahul tomorrow at 3 PM", "calendar"),
            ("Plan my day", "day"),
            ("Remind me to call Amma tomorrow at 7 pm", "reminders"),
            ("Two kilos of rice and one litre of milk", "order"),
            ("I want something for dinner", "order"),
        ]
        for text, expected in cases:
            # A fresh conversation each time, as a person would start one.
            reply = await say(client, await start(client), text)
            assert workflow(reply) == expected, (text, workflow(reply))


async def test_gmail_and_calendar_replies_show_no_ordering_information(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        for text in ("Check my important emails", "Schedule a meeting with Rahul tomorrow at 3 PM"):
            reply = await say(client, await start(client), text)
            assert reply["show_readback"] is False and reply["can_confirm"] is False
            assert reply["options"] == [] and not (reply["order_ids"])


async def test_calendar_requests_show_calendar_widgets_only(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        reply = await say(client, await start(client), "What's on my calendar today?")
        assert workflow(reply) == "calendar"
        text = repr(reply["ui"]).lower()
        assert (
            "whatsapp" not in text
            and "cart" not in text
            and "total" not in text.replace("tasks", "")
        )


# Switching does not leak or destroy -------------------------------------------------------------


async def test_switching_away_from_an_order_keeps_it_but_stops_showing_it(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        order = await say(client, headers, "Two kilos of rice and one litre of milk")
        assert workflow(order) == "order" and order["cart"]
        away = await say(client, headers, "Send Rahul a WhatsApp message saying I'll be late")
        assert workflow(away) == "whatsapp"
        assert away["show_readback"] is False and away["options"] == []  # not offered over WhatsApp
        assert away["cart"]  # but not destroyed: still held, so it can be resumed
        assert any(t["capability"] == "grocery" and t["open"] for t in away["tasks"])
        back = await say(client, headers, "back to my groceries")
        assert workflow(back) == "order" and back["cart"]
        assert back["show_readback"] is True


async def test_a_bare_yes_cannot_place_a_paused_order_from_another_workflow(
    tmp_path: Path,
) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        order = await say(client, headers, "Two kilos of rice and one litre of milk")
        assert order["state"] == "READBACK"
        await say(client, headers, "Send Rahul a WhatsApp message saying I'll be late")
        guarded = await say(client, headers, "yes")
        assert "still waiting" in guarded["assistant_text"]
        assert guarded["order_ids"] == [] and guarded["state"] == "READBACK"  # nothing was placed


async def test_switching_from_email_to_calendar_replaces_the_active_workflow(
    tmp_path: Path,
) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        assert workflow(await say(client, headers, "Check my important emails")) == "email"
        cal = await say(client, headers, "Schedule a meeting with Rahul tomorrow at 3 PM")
        assert workflow(cal) == "calendar"


# Nothing else broke -------------------------------------------------------------------------------


async def test_ordering_still_completes_start_to_finish(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        headers = await start(client)
        await say(client, headers, "Two kilos of rice and one litre of milk")
        done = await say(client, headers, "Yes")
        assert done["state"] == "DONE" and workflow(done) == "order"


async def test_the_opening_greeting_is_not_an_order(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        started = (await client.post("/api/sessions", json={"language": "en-IN"})).json()
        assert started["workflow"]["kind"] == "none"


async def test_hindi_and_kannada_questions_stay_in_workflow(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        for language, text, ask_word in (
            ("hi-IN", "अम्मा को व्हाट्सऐप भेजो", "क्या लिखूँ"),
            ("kn-IN", "ಅಮ್ಮನಿಗೆ ವಾಟ್ಸಾಪ್ ಮಾಡಿ", "ಏನು ಹೇಳಲಿ"),
        ):
            headers = await start(client, language)
            asked = await say(client, headers, text)
            assert workflow(asked) == "whatsapp", (language, asked["assistant_text"])
            assert ask_word in asked["assistant_text"]
            answered = await say(
                client, headers, "मैं देर से आऊँगा" if language == "hi-IN" else "ತಡವಾಗಿ ಬರುತ್ತೇನೆ"
            )
            assert answered["plan"]["id"] and workflow(answered) == "whatsapp"
