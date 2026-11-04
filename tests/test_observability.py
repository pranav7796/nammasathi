from __future__ import annotations

import logging

import pytest

from app.commerce.mock import MockInstamart
from app.observability import log_event
from app.orchestrator.state_machine import CallStarted
from app.orchestrator.turn_runner import TextTurnRunner


def test_unlisted_and_non_scalar_fields_are_dropped() -> None:
    record = log_event(
        "x",
        tool="get_cart",
        duration_ms=12,
        address="12 Home Road",
        phone="9999999999",
        transcript="ಹಾಲು",
        token="secret",
        status={"nested": "secret"},
    )
    assert set(record) == {"ts", "event", "tool", "duration_ms"}


async def test_runtime_events_hold_no_address_or_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ajji.events")
    runner = await TextTurnRunner.create(MockInstamart())
    await runner.dispatch(CallStarted(user_known=True))

    assert any("state_changed" in message for message in caplog.messages)
    assert not any("Jayanagar" in message for message in caplog.messages)
