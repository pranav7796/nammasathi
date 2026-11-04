from datetime import UTC, datetime

import pytest

from app.commerce.mock import MockInstamart
from app.llm.extractor import GeminiIntentExtractor, GroceryIntent, GroceryItem
from app.orchestrator.state_machine import CallStarted, Say, State
from app.orchestrator.turn_runner import TextTurnRunner

from .test_gemini_extractor import FakeClient, FakeResponse


@pytest.mark.asyncio
async def test_text_intent_to_mock_order_flow() -> None:
    extractor = GeminiIntentExtractor(
        FakeClient(
            iter(
                [
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="add_items",
                            items=[
                                GroceryItem(name="rice", quantity=2, unit="kg"),
                                GroceryItem(name="milk", quantity=1, unit="litre"),
                            ],
                        )
                    ),
                    FakeResponse(parsed=GroceryIntent(intent="confirm", items=[])),
                ]
            )
        )
    )
    runner = await TextTurnRunner.create(
        MockInstamart(now=datetime(2026, 11, 3, 10, 0, tzinfo=UTC)), extractor=extractor
    )

    await runner.dispatch(CallStarted(user_known=True))
    effects = await runner.dispatch_text("2 kg rice and 1 litre milk")

    assert runner.session.state is State.READBACK
    readback = next(
        effect for effect in effects if isinstance(effect, Say) and effect.template == "readback"
    )
    assert readback.template == "readback"
    assert "rice" in readback.data["lines"].lower()
    assert "milk" in readback.data["lines"].lower()

    await runner.dispatch_text("yes")

    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("IM000001",)


@pytest.mark.asyncio
async def test_llm_confirm_classification_cannot_authorize_checkout() -> None:
    extractor = GeminiIntentExtractor(
        FakeClient(
            iter(
                [
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="add_items",
                            items=[GroceryItem(name="atta", quantity=5, unit="kg")],
                        )
                    ),
                    FakeResponse(parsed=GroceryIntent(intent="confirm", items=[])),
                ]
            )
        )
    )
    mock = MockInstamart()
    runner = await TextTurnRunner.create(mock, extractor=extractor)
    await runner.dispatch(CallStarted(user_known=True))
    await runner.dispatch_text("Add 5 kg atta")

    await runner.dispatch_text("maybe")

    assert runner.session.state is State.READBACK
    assert mock.checkout_calls == 0


@pytest.mark.asyncio
async def test_text_cart_edits_require_updated_confirmation() -> None:
    extractor = GeminiIntentExtractor(
        FakeClient(
            iter(
                [
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="add_items",
                            items=[GroceryItem(name="atta", quantity=5, unit="kg")],
                        )
                    ),
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="add_items",
                            items=[GroceryItem(name="milk", quantity=1, unit="litre")],
                        )
                    ),
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="remove_items",
                            items=[GroceryItem(name="milk", quantity=1, unit=None)],
                        )
                    ),
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="change_quantity",
                            items=[GroceryItem(name="atta", quantity=3, unit="kg")],
                        )
                    ),
                    FakeResponse(parsed=GroceryIntent(intent="confirm", items=[])),
                ]
            )
        )
    )
    mock = MockInstamart(now=datetime(2026, 11, 3, 10, 0, tzinfo=UTC))
    runner = await TextTurnRunner.create(mock, extractor=extractor)
    await runner.dispatch(CallStarted(user_known=True))

    await runner.dispatch_text("Add 5 kg atta")
    first_hash = runner.session.cart_hash
    await runner.dispatch_text("Add 1 litre milk")
    assert runner.session.cart_hash != first_hash
    assert runner.session.state is State.READBACK

    await runner.dispatch_text("Remove the milk")
    assert all("milk" not in item.name.casefold() for item in runner.session.cart)
    await runner.dispatch_text("Make the atta 3 kg instead")
    assert runner.session.cart[0].pack_size == "1 kg"
    assert runner.session.cart[0].quantity == 3
    assert runner.session.state is State.READBACK

    await runner.dispatch_text("Yes, confirm")
    assert runner.session.state is State.DONE
    assert mock.checkout_calls == 1


@pytest.mark.asyncio
async def test_text_cancel_clears_cart_without_checkout() -> None:
    extractor = GeminiIntentExtractor(
        FakeClient(
            iter(
                [
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="add_items",
                            items=[GroceryItem(name="atta", quantity=5, unit="kg")],
                        )
                    ),
                    FakeResponse(parsed=GroceryIntent(intent="cancel", items=[])),
                ]
            )
        )
    )
    mock = MockInstamart()
    runner = await TextTurnRunner.create(mock, extractor=extractor)
    await runner.dispatch(CallStarted(user_known=True))
    await runner.dispatch_text("Add 5 kg atta")

    await runner.dispatch_text("Cancel this order")

    assert runner.session.cart == ()
    assert runner.session.state is State.COLLECTING
    assert mock.checkout_calls == 0


@pytest.mark.asyncio
async def test_failed_extraction_does_not_change_cart() -> None:
    extractor = GeminiIntentExtractor(
        FakeClient(
            iter(
                [
                    FakeResponse(
                        parsed=GroceryIntent(
                            intent="add_items",
                            items=[GroceryItem(name="atta", quantity=5, unit="kg")],
                        )
                    ),
                    FakeResponse(text='{"intent":"add_items","items":[]}'),
                ]
            )
        )
    )
    runner = await TextTurnRunner.create(MockInstamart(), extractor=extractor)
    await runner.dispatch(CallStarted(user_known=True))
    await runner.dispatch_text("Add 5 kg atta")
    before = runner.session

    effects = await runner.dispatch_text("unparseable request")

    assert runner.session == before
    assert any(isinstance(effect, Say) and effect.template == "intent_error" for effect in effects)
