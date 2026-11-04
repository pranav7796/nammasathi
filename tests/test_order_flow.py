from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.commerce.mock import MockInstamart
from app.orchestrator.state_machine import (
    CallStarted,
    CartEntry,
    ItemsExtracted,
    PlaceOrder,
    Say,
    State,
    UserConfirmed,
)
from app.orchestrator.turn_runner import TextTurnRunner


def atta() -> CartEntry:
    return CartEntry("atta_5kg", "sku_atta_5kg", "Aashirvaad Whole Wheat Atta", "5 kg", 1, 299)


@pytest.mark.asyncio
async def test_mock_text_order_journey_requires_exact_confirmation() -> None:
    runner = await TextTurnRunner.create(
        MockInstamart(now=datetime(2026, 11, 3, 10, 0, tzinfo=UTC))
    )

    await runner.dispatch(CallStarted(user_known=True))
    readback = await runner.dispatch(ItemsExtracted(resolved=(atta(),)))
    readback_say = next(e for e in readback if isinstance(e, Say) and e.template == "readback")
    assert readback_say.data["lines"] == "1 x 5 kg Aashirvaad Whole Wheat Atta"
    assert readback_say.data["total"] == "324"
    assert readback_say.data["payment"] == "COD"
    assert readback_say.data["address_label"] == "Home"

    confirmation = UserConfirmed(runner.session.cart_version, runner.session.cart_hash)
    placed = await runner.dispatch(confirmation)

    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("IM000001",)
    assert any(isinstance(effect, PlaceOrder) for effect in placed)
    assert any(isinstance(effect, Say) and effect.template == "order_placed" for effect in placed)


@pytest.mark.asyncio
async def test_mock_text_order_does_not_place_on_unclear_confirmation() -> None:
    runner = await TextTurnRunner.create(MockInstamart())
    await runner.dispatch(CallStarted(user_known=True))
    await runner.dispatch(ItemsExtracted(resolved=(atta(),)))

    await runner.dispatch(UserConfirmed(runner.session.cart_version, "wrong-hash"))

    assert runner.session.state is State.READBACK
    assert runner.session.order_ids == ()
