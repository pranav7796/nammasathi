"""Share and calendar buttons after a finished demo order or booking."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from tests.test_accounts import site


async def converse(client: httpx.AsyncClient, turns: tuple[str, ...]) -> dict[str, Any]:
    started = await client.post("/api/sessions", json={"language": "en-IN"})
    assert started.status_code == 200, started.text
    headers = {"X-Session-ID": started.json()["session_id"]}
    reply: dict[str, Any] = started.json()
    for text in turns:
        response = await client.post("/api/session/text", json={"text": text}, headers=headers)
        assert response.status_code == 200, response.text
        reply = response.json()
    return reply


async def test_a_finished_demo_booking_offers_share_and_calendar(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        reply = await converse(
            client,
            (
                "Book a table for two in Indiranagar tomorrow at 7 PM, Italian",
                "First",
                "Yes",
            ),
        )
        assert reply["state"] == "DONE"
        panel = reply["ui"][0]
        assert panel["type"] == "share_panel"
        assert "Demo booking" in panel["summary"]
        assert "not a real reservation" in panel["summary"]
        calendar = panel["calendar"]
        assert calendar["title"].startswith("Demo: ")
        assert calendar["start"].endswith("+05:30")
        assert calendar["start"][11:16] == "19:00"
        assert calendar["end"][11:16] == "20:30"


async def test_a_finished_demo_order_offers_a_labelled_summary(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        reply = await converse(client, ("Two kilos of rice and one litre of milk", "Yes"))
        assert reply["state"] == "DONE"
        panel = reply["ui"][0]
        assert panel["type"] == "share_panel"
        assert "not a real order" in panel["summary"]
        assert not panel.get("calendar")


async def test_nothing_is_offered_before_the_order_is_done(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as client:
        reply = await converse(client, ("Two kilos of rice and one litre of milk",))
        assert reply["state"] in {"READBACK", "CLARIFYING"}
        assert not reply.get("ui")
