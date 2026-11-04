"""One assistant, three capabilities: routing, keeping tasks apart, and shared safety."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.assistant.orchestrator import Assistant, Reply
from app.assistant.router import Domain, keyword_domain, route
from app.assistant.workflows import OrderWorkflow
from app.commerce.mock import MockInstamart
from app.commerce.order_journal import (
    AttemptStatus,
    FileOrderJournal,
    InMemoryOrderJournal,
    new_attempt,
    resolve_attempt,
    unresolved_attempts,
)
from app.dining.mock import MockDineout
from app.dining.workflow import DiningWorkflow
from app.food.mock import MockFood
from app.llm.extractor import (
    ClarificationContext,
    GroceryIntent,
    GroceryItem,
    IntentExtractionError,
)
from app.orchestrator.checkout import CheckoutPolicy
from app.orchestrator.state_machine import Say
from app.voice.presentation import response_text

TOMORROW = "2026-11-06"


@pytest.mark.parametrize(
    ("suggested", "intent", "text", "active", "has_items", "expected"),
    [
        ("grocery", "add_items", "milk", None, True, "grocery"),
        ("food", "add_items", "biryani", None, True, "food"),
        ("dining", "add_items", "", None, False, "dining"),
        (None, "book_table", "table for two", None, False, "dining"),
        (None, "reorder_usual", "the usual", "food", False, "grocery"),
        (None, "add_items", "chicken biryani", "grocery", True, "food"),
        (None, "add_items", "milk", None, True, "grocery"),
        (None, "add_items", "more of that", "food", True, "food"),
        (None, "unclear", "hmm", None, False, None),
        (None, "unclear", "hmm", "food", False, "food"),
        ("nonsense", "add_items", "milk", None, True, "grocery"),
    ],
)
def test_routing(
    suggested: str | None,
    intent: str,
    text: str,
    active: Domain | None,
    has_items: bool,
    expected: str | None,
) -> None:
    assert (
        route(suggested=suggested, intent=intent, text=text, active=active, has_items=has_items)
        == expected
    )


@pytest.mark.parametrize("intent", ["confirm", "reject", "cancel", "done", "select_option"])
def test_replies_never_leave_the_current_task_even_if_the_model_says_so(intent: str) -> None:
    assert (
        route(suggested="dining", intent=intent, text="x", active="food", has_items=False) == "food"
    )
    assert route(suggested="dining", intent=intent, text="x", active=None, has_items=False) is None


def test_vague_meal_request_routes_to_food_when_asking() -> None:
    assert (
        route(
            suggested="food",
            intent="unclear",
            text="something for dinner",
            active=None,
            has_items=False,
            needs_clarification=True,
        )
        == "food"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("book a table for two", "dining"),
        ("ಟೇಬಲ್ ಬುಕ್ ಮಾಡಿ", "dining"),
        ("I want biryani", "food"),
        ("ಊಟ ಬೇಕು", "food"),
        ("खाना चाहिए", "food"),
        ("milk and bread", None),
    ],
)
def test_keywords_work_across_languages(text: str, expected: str | None) -> None:
    assert keyword_domain(text) == expected


class Script:
    """Scripted language understanding, recording the context it was given."""

    def __init__(self, *intents: GroceryIntent) -> None:
        self.intents = list(intents)
        self.contexts: list[ClarificationContext | None] = []

    def extract(
        self, text: str, *, language: str = "kn-IN", context: ClarificationContext | None = None
    ) -> GroceryIntent:
        del text, language
        self.contexts.append(context)
        if not self.intents:
            raise IntentExtractionError("nothing scripted")
        return self.intents.pop(0)


def groceries(*names: str, domain: str | None = "grocery") -> GroceryIntent:
    return GroceryIntent(
        intent="add_items",
        domain=domain,
        items=[GroceryItem(name=name, quantity=2, unit="kg") for name in names],  # above ₹99
    )


def meal(name: str, quantity: float = 1) -> GroceryIntent:
    return GroceryIntent(
        intent="add_items",
        domain="food",
        items=[GroceryItem(name=name, quantity=quantity, unit=None)],
    )


TABLE = GroceryIntent(
    intent="book_table",
    domain="dining",
    items=[],
    query="Italian",
    area="Bengaluru",
    date=TOMORROW,
    time="19:00",
    guests=2,
)


class Harness:
    def __init__(self, *intents: GroceryIntent, journal: Any = None, policy: Any = None) -> None:
        self.grocery_shop = MockInstamart()
        self.food_shop = MockFood()
        self.dining_shop = MockDineout()
        self.journal = journal or InMemoryOrderJournal()
        self.policy = policy or CheckoutPolicy()
        self.script = Script(*intents)
        self.assistant = Assistant(
            language="en-IN",
            extractor=self.script,
            factory=self.build,
            journal=self.journal,
            address_label="Home",
            delivery_address="1 Road",
            today=date(2026, 11, 5),
        )

    async def build(self, domain: Domain) -> Any:
        if domain == "dining":
            return DiningWorkflow(self.dining_shop, self.journal, self.policy)
        return OrderWorkflow(
            domain,
            self.food_shop if domain == "food" else self.grocery_shop,
            address_id="addr_home",
            language="en-IN",
            extractor=self.script,
            journal=self.journal,
            policy=self.policy,
        )


def says(reply: Reply) -> list[str]:
    return [effect.template for effect in reply.effects if isinstance(effect, Say)]


async def test_one_assistant_handles_all_three_in_one_conversation() -> None:
    h = Harness(groceries("rice"), meal("masala dosa"), TABLE)
    assert says(await h.assistant.start()) == ["greeting"]

    await h.assistant.turn("rice")
    assert h.assistant.active == "grocery"
    assert h.assistant.view().state == "READBACK"
    assert says(await h.assistant.turn("yes"))[-1] == "order_placed"
    assert h.grocery_shop.checkout_calls == 1

    await h.assistant.turn("masala dosa")
    assert h.assistant.active == "food"
    assert says(await h.assistant.turn("yes"))[-1] == "order_placed"
    assert h.food_shop.checkout_calls == 1

    await h.assistant.turn("table for two tomorrow")
    assert h.assistant.active == "dining"
    await h.assistant.turn("first")
    assert says(await h.assistant.turn("yes")) == ["dining_booked"]
    assert h.dining_shop.book_calls == 1


async def test_yes_only_ever_approves_the_current_task() -> None:
    h = Harness(groceries("rice"), TABLE)
    await h.assistant.start()
    await h.assistant.turn("rice")

    # Moving on leaves the grocery cart waiting, and it needs its own yes later.
    reply = await h.assistant.turn("book a table")
    assert "switched_domain" in says(reply)
    await h.assistant.turn("first")
    await h.assistant.turn("yes")

    assert h.dining_shop.book_calls == 1
    assert h.grocery_shop.checkout_calls == 0


async def test_switching_keeps_the_other_tasks_cart() -> None:
    h = Harness(groceries("rice"), meal("masala dosa"), groceries("sugar"))
    await h.assistant.start()
    await h.assistant.turn("rice")
    await h.assistant.turn("masala dosa")
    assert h.food_shop.checkout_calls == 0

    await h.assistant.turn("sugar")

    assert h.assistant.active == "grocery"
    names = {line.name for line in h.assistant.view().lines}
    assert names == {"Sona Masoori Rice", "Madhur Sugar"}


async def test_a_heard_readback_from_another_task_cannot_order() -> None:
    h = Harness(groceries("rice"), meal("masala dosa"))
    await h.assistant.start()
    await h.assistant.turn("rice")
    view = h.assistant.view()
    heard = (view.readback_version, view.readback_hash)
    await h.assistant.turn("masala dosa")

    await h.assistant.turn("yes", heard)  # type: ignore[arg-type]

    assert h.food_shop.checkout_calls == 0
    assert h.grocery_shop.checkout_calls == 0


async def test_llm_cannot_confirm_any_capability() -> None:
    llm_yes = GroceryIntent(intent="confirm", domain="grocery", items=[])
    h = Harness(groceries("rice"), llm_yes, TABLE, llm_yes, llm_yes)
    await h.assistant.start()
    await h.assistant.turn("rice")
    await h.assistant.turn("sounds fine I guess")
    assert h.grocery_shop.checkout_calls == 0

    await h.assistant.turn("a table please")
    await h.assistant.turn("first")
    await h.assistant.turn("sure why not")
    await h.assistant.turn("go on then")

    assert h.dining_shop.book_calls == 0


async def test_unknown_outcome_anywhere_blocks_everything(tmp_path: Path) -> None:
    journal = FileOrderJournal(tmp_path / "attempts.jsonl")
    h = Harness(TABLE, groceries("rice"), journal=journal)
    h.dining_shop.fail_next_book = TimeoutError("lost")
    await h.assistant.start()
    await h.assistant.turn("a table")
    await h.assistant.turn("first")

    assert says(await h.assistant.turn("yes")) == ["order_unknown"]
    assert h.assistant.blocked

    assert says(await h.assistant.turn("rice")) == ["order_unknown"]
    assert h.grocery_shop.checkout_calls == 0
    assert h.script.intents, "not even understood: nothing proceeds until a person settles it"


async def test_unresolved_attempt_from_another_capability_blocks_a_new_conversation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attempts.jsonl"
    FileOrderJournal(path).begin(
        new_attempt("h", "0", (), ("Toscano",), domain="dining"), lambda _: None
    )
    h = Harness(groceries("rice"), journal=FileOrderJournal(path))

    assert says(await h.assistant.start()) == ["order_unknown"]
    assert h.assistant.view().state == "ORDER_UNKNOWN"
    assert says(await h.assistant.turn("rice")) == ["order_unknown"]


async def test_settling_the_attempt_lets_a_new_conversation_continue(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    attempt = new_attempt("h", "0", (), ("Toscano",), domain="dining")
    FileOrderJournal(path).begin(attempt, lambda _: None)
    resolve_attempt(FileOrderJournal(path), attempt, AttemptStatus.NOT_PLACED)

    h = Harness(groceries("rice"), journal=FileOrderJournal(path))

    assert says(await h.assistant.start()) == ["greeting"]
    assert unresolved_attempts(FileOrderJournal(path)) == []


async def test_food_order_that_appears_after_a_crash_is_settled_by_food_history(
    tmp_path: Path,
) -> None:
    from decimal import Decimal

    from app.commerce.base import OrderSummary

    path = tmp_path / "attempts.jsonl"
    FileOrderJournal(path).begin(
        new_attempt("h", "120", (), ("Masala Dosa",), domain="food"), lambda _: None
    )
    h = Harness(journal=FileOrderJournal(path))
    h.food_shop.orders.append(
        OrderSummary(order_id="F9", items=[], total_inr=Decimal(120), status="PLACED")
    )

    assert says(await h.assistant.start()) == ["greeting"]
    [attempt] = FileOrderJournal(path).attempts()
    assert attempt.status is AttemptStatus.PLACED
    assert attempt.order_ids == ("F9",)


async def test_grocery_history_cannot_settle_a_food_attempt(tmp_path: Path) -> None:
    from decimal import Decimal

    from app.commerce.base import OrderSummary

    path = tmp_path / "attempts.jsonl"
    FileOrderJournal(path).begin(
        new_attempt("h", "120", (), ("Masala Dosa",), domain="food"), lambda _: None
    )
    h = Harness(journal=FileOrderJournal(path))
    h.grocery_shop.orders.append(
        OrderSummary(order_id="G1", items=[], total_inr=Decimal(120), status="PLACED")
    )

    assert says(await h.assistant.start()) == ["order_unknown"]


async def test_limit_is_shared_across_capabilities() -> None:
    h = Harness(groceries("rice"), TABLE, policy=CheckoutPolicy(max_orders=1))
    await h.assistant.start()
    await h.assistant.turn("rice")
    await h.assistant.turn("yes")
    assert h.grocery_shop.checkout_calls == 1

    await h.assistant.turn("a table")
    await h.assistant.turn("first")
    reply = await h.assistant.turn("yes")

    assert says(reply) == ["order_limit"]
    assert h.dining_shop.book_calls == 0


async def test_new_request_after_a_finished_order_starts_a_new_task() -> None:
    h = Harness(groceries("rice"), groceries("sugar"))
    await h.assistant.start()
    await h.assistant.turn("rice")
    await h.assistant.turn("yes")
    first_orders = h.assistant.view().order_ids

    await h.assistant.turn("sugar")

    assert h.assistant.view().state == "READBACK"
    assert h.assistant.view().order_ids == ()
    assert first_orders == ("IM000001",)
    assert h.grocery_shop.checkout_calls == 1


async def test_unclear_request_with_no_task_asks_rather_than_guessing() -> None:
    h = Harness(GroceryIntent(intent="unclear", items=[]))
    await h.assistant.start()

    assert says(await h.assistant.turn("hmm")) == ["ask_domain"]
    assert h.assistant.workflows == {}


async def test_failed_understanding_changes_nothing() -> None:
    h = Harness(groceries("rice"))
    await h.assistant.start()
    await h.assistant.turn("rice")
    before = h.assistant.view()

    assert says(await h.assistant.turn("???")) == ["intent_error"]
    assert h.assistant.view() == before


async def test_vague_dinner_request_asks_then_continues_in_food() -> None:
    vague = GroceryIntent(
        intent="add_items",
        domain="food",
        items=[],
        needs_clarification=True,
        clarification_question="What would you like?",
    )
    h = Harness(vague, meal("masala dosa"))
    await h.assistant.start()

    reply = await h.assistant.turn("I want something for dinner")
    assert h.assistant.active == "food"
    text = response_text(reply.effects, language="en-IN", commerce_mode="mock", domain="food")
    assert text == "Sure. What do you feel like eating?"

    await h.assistant.turn("masala dosa")
    assert h.assistant.view().state == "READBACK"


async def test_question_context_reaches_the_model_with_todays_date() -> None:
    h = Harness(meal("chicken biryani"), GroceryIntent(intent="unclear", items=[]))
    await h.assistant.start()
    await h.assistant.turn("biryani")

    await h.assistant.turn("the one nearby")

    context = h.script.contexts[-1]
    assert context is not None
    assert context.today == "2026-11-05"
    assert context.active_domain == "food"
    assert "Chicken Biryani from Paradise" in " ".join(context.options)


async def test_existing_food_cart_is_kept_and_spoken_when_opened() -> None:
    from app.commerce.base import CartLine

    h = Harness(meal("idli"))
    await h.food_shop.update_cart(
        "addr_home", [CartLine(spin_id="masala_dosa_0", sku_id="sku_masala_dosa_0", quantity=1)]
    )
    await h.assistant.start()

    reply = await h.assistant.turn("idli")

    assert says(reply)[0] == "existing_cart"
    assert {item.name for item in h.food_shop._cart} == {  # pyright: ignore[reportPrivateUsage]
        "Masala Dosa from Vidyarthi Bhavan",
        "Idli Vada from Vidyarthi Bhavan",
    }


async def test_phrases_are_rendered_for_each_capability() -> None:
    h = Harness(TABLE)
    await h.assistant.start()
    reply = await h.assistant.turn("a table")
    text = response_text(reply.effects, language="kn-IN", commerce_mode="mock", domain="dining")
    assert "Toscano" in text
    reply = await h.assistant.turn("ಮೊದಲನೆಯದು")
    text = response_text(reply.effects, language="kn-IN", commerce_mode="mock", domain="dining")
    assert "ಟೇಬಲ್" in text
    assert "07:00 PM" in text


async def test_every_language_reaches_a_booking() -> None:
    for yes, first in (("ಹೌದು", "ಮೊದಲನೆಯದು"), ("हाँ", "पहला"), ("yes", "first")):
        h = Harness(TABLE)
        await h.assistant.start()
        await h.assistant.turn("table")
        await h.assistant.turn(first)
        await h.assistant.turn(yes)
        assert h.dining_shop.book_calls == 1
