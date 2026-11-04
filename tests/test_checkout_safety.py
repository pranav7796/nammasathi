"""End-to-end safety of the journey through the runner, with live-shaped fake commerce.

Every test in the "checkout cannot execute twice" section reproduces a path that
placed a second order, or lost track of one, before this code existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.commerce.base import (
    Address,
    Cart,
    CartIssue,
    CartItem,
    CartLine,
    CommerceError,
    CommerceNotAttempted,
    CommerceRejected,
    CommerceWriteDisabled,
    OrderResult,
    OrderSummary,
    OrderSummaryItem,
    OrderTracking,
    PaymentMethod,
    PaymentOptions,
    Product,
    RejectionKind,
    SearchResult,
    Variant,
    same_address_id,
)
from app.commerce.order_journal import (
    AttemptStatus,
    FileOrderJournal,
    InMemoryOrderJournal,
    JournalError,
    new_attempt,
    unresolved_attempts,
)
from app.commerce.units import parse_pack_size
from app.orchestrator.checkout import CheckoutCoordinator, CheckoutPolicy
from app.orchestrator.state_machine import CallStarted, Effect, PlaceOrder, Say, State
from app.orchestrator.turn_runner import ExistingCartConflict, TextTurnRunner
from app.voice.presentation import response_text


def variant(spin: str, pack: str, price: int = 150) -> Variant:
    amount = parse_pack_size(pack)
    return Variant(
        spin_id=spin,
        sku_id=f"sku-{spin}",
        pack_size=pack,
        quantity_value=float(amount.value) if amount else None,
        unit=amount.measure if amount else None,
        price_inr=Decimal(price),
    )


MILK = Product(id="m", display_name="Amul Milk", brand="Amul", variants=[variant("m1", "1 ltr")])
RICE = Product(id="r", display_name="Dosa Rice", brand="", variants=[variant("r1", "1 kg")])
OTHER_MILK = Product(
    id="n", display_name="Nandini Milk", brand="", variants=[variant("n1", "1 ltr")]
)


@dataclass
class Shop:
    """A fake Instamart with Swiggy's observed behaviour and scripted failures."""

    catalogue: dict[str, list[Product]] = field(
        default_factory=lambda: {"milk": [MILK], "rice": [RICE]}
    )
    items: list[CartItem] = field(default_factory=lambda: list[CartItem]())
    cart_address: str | None = None
    orders: list[OrderSummary] = field(default_factory=lambda: list[OrderSummary]())
    checkout_calls: int = 0
    update_calls: int = 0
    checkout_script: list[Any] = field(default_factory=lambda: list[Any]())
    issues: list[CartIssue] = field(default_factory=lambda: list[CartIssue]())
    cod_available: bool = True
    orders_fail: bool = False
    payment_fail: bool = False
    update_error: Exception | None = None
    tracking_error: bool = False

    async def get_addresses(self) -> list[Address]:
        return [Address(id="home", label="Home", full_address="1 Road")]

    async def search_products(self, address_id: str, query: str, offset: int = 0) -> SearchResult:
        return SearchResult(products=self.catalogue.get(query, []))

    async def go_to_items(self, address_id: str) -> list[Product]:
        return [MILK, RICE]

    def _cart(self) -> Cart:
        total = sum((item.line_total_inr for item in self.items), Decimal(0))
        return Cart(
            address_id=self.cart_address,
            delivery_label="Home",
            delivery_address="1 Road",
            items=list(self.items),
            item_total_inr=total,
            delivery_fee_inr=Decimal(0),
            grand_total_inr=total,
            payment_methods=[],
            issues=list(self.issues),
            lat=12.9,
            lng=77.5,
        )

    async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
        self.update_calls += 1
        if self.update_error is not None:
            raise self.update_error
        known = {
            v.spin_id: (p, v) for ps in self.catalogue.values() for p in ps for v in p.variants
        }
        self.items = [
            CartItem(
                spin_id=line.spin_id,
                sku_id=line.sku_id,
                name=known[line.spin_id][0].display_name,
                pack_size=known[line.spin_id][1].pack_size,
                quantity=line.quantity,
                unit_price_inr=known[line.spin_id][1].price_inr,
            )
            for line in items
        ]
        self.cart_address = address_id
        return self._cart()

    async def get_cart(self) -> Cart:
        return self._cart()

    async def get_payment_options(self) -> PaymentOptions:
        if self.payment_fail:
            raise CommerceError("payment options unavailable")
        return PaymentOptions(cod_available=self.cod_available, cod_id="COD")

    def place(
        self, order_id: str = "NEW-1", *, items: list[CartItem] | None = None
    ) -> OrderSummary:
        lines = self.items if items is None else items
        order = OrderSummary(
            order_id=order_id,
            created_at=datetime.now(UTC),
            items=[OrderSummaryItem(name=i.name, quantity=i.quantity) for i in lines],
            total_inr=sum((i.line_total_inr for i in lines), Decimal(0)),
            status="PLACED",
        )
        self.orders.append(order)
        return order

    async def checkout(
        self,
        address_id: str,
        payment_method: PaymentMethod = PaymentMethod.COD,
        confirm_duplicate: bool = False,
    ) -> OrderResult:
        assert confirm_duplicate is False, "a duplicate must never be forced"
        self.checkout_calls += 1
        step = self.checkout_script.pop(0) if self.checkout_script else "ok"
        if callable(step):
            step = step(self)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, OrderResult):
            return step
        order = self.place()
        return OrderResult(success=True, order_ids=[order.order_id], status="PLACED")

    async def get_orders(self, count: int = 10) -> list[OrderSummary]:
        if self.orders_fail:
            raise CommerceError("history unavailable")
        return list(self.orders)

    async def track_order(self, order_id: str, lat: float, lng: float) -> OrderTracking:
        if self.tracking_error:
            raise CommerceError("tracking unavailable")
        return OrderTracking(order_id=order_id, status_message="On the way")


class Extractor:
    """Scripted language understanding; only the runner decides what it means."""

    def __init__(self, *intents: Any) -> None:
        self.intents = list(intents)
        self.contexts: list[object] = []

    def extract(self, text: str, *, language: str = "kn-IN", context: Any = None) -> Any:
        self.contexts.append(context)
        script: tuple[str, list[tuple[str, float, str | None]]] = (
            self.intents.pop(0) if self.intents else ("unclear", [])
        )
        return SimpleNamespace(
            intent=script[0],
            needs_clarification=False,
            clarification_question=None,
            option_number=None,
            items=[
                SimpleNamespace(name=n, quantity=q, unit=u, search_term=None)
                for n, q, u in script[1]
            ],
        )


ADD_MILK: Any = ("add_items", [("milk", 1.0, "litre")])


async def start(
    shop: Shop, *intents: Any, journal: Any = None, policy: Any = None
) -> TextTurnRunner:
    runner = await TextTurnRunner.create(
        shop,
        Extractor(*(intents or (ADD_MILK,))),
        address_id="home",
        journal=journal or InMemoryOrderJournal(),
        policy=policy,
    )
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))
    return runner


async def at_readback(shop: Shop, **kwargs: Any) -> TextTurnRunner:
    runner = await start(shop, **kwargs)
    await runner.dispatch_text("one litre milk")
    assert runner.session.state is State.READBACK
    assert runner.confirmable
    return runner


def says(effects: list[Effect]) -> list[str]:
    return [effect.template for effect in effects if isinstance(effect, Say)]


# ------------------------------------------------------------------- happy path
async def test_full_journey_verifies_the_order_before_announcing_it() -> None:
    shop = Shop()
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert any(isinstance(effect, PlaceOrder) for effect in effects)
    assert shop.checkout_calls == 1
    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("NEW-1",)
    assert runner.session.order_verified is True
    assert runner.session.order_status == "PLACED"
    assert says(effects)[-1] == "order_placed"


@pytest.mark.parametrize(("language", "yes"), [("kn-IN", "ಹೌದು"), ("hi-IN", "हाँ"), ("en-IN", "yes")])
async def test_spoken_yes_places_the_order_in_every_language(language: str, yes: str) -> None:
    shop = Shop()
    runner = await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="home")
    await runner.dispatch(CallStarted(user_known=True, language=language))
    await runner.dispatch_text("milk")

    await runner.dispatch_text(yes)

    assert shop.checkout_calls == 1
    assert runner.session.state is State.DONE


@pytest.mark.parametrize(("language", "no"), [("kn-IN", "ಬೇಡ"), ("hi-IN", "नहीं"), ("en-IN", "no")])
async def test_spoken_no_never_orders(language: str, no: str) -> None:
    shop = Shop()
    runner = await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="home")
    await runner.dispatch(CallStarted(user_known=True, language=language))
    await runner.dispatch_text("milk")

    effects = await runner.dispatch_text(no)

    assert shop.checkout_calls == 0
    assert runner.session.state is State.COLLECTING
    assert response_text(effects, language=language, commerce_mode="swiggy"), "never a silent turn"


# ---------------------------------------------------- approval bound to what was heard
async def test_yes_to_an_older_readback_never_orders_the_changed_cart() -> None:
    shop = Shop()
    runner = await start(shop, ADD_MILK, ("add_items", [("rice", 1.0, "kg")]))
    await runner.dispatch_text("milk")
    heard = (runner.session.readback_version, runner.session.readback_hash)
    await runner.dispatch_text("rice")  # the reply carrying this readback never reached the user

    effects = await runner.dispatch_text("yes", heard=heard)  # type: ignore[arg-type]

    assert shop.checkout_calls == 0
    assert runner.session.state is State.READBACK
    assert "readback" in says(effects), "the user hears the real cart instead"


async def test_failed_write_with_lost_reply_cannot_be_confirmed_blindly() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    heard = (runner.session.readback_version, runner.session.readback_hash)
    runner.extractor = Extractor(("add_items", [("rice", 1.0, "kg")]))
    shop.update_error = CommerceError("network")

    effects = await runner.dispatch_text("rice")

    assert "commerce_error" in says(effects), "the failure is spoken, not raised away"
    assert "readback" in says(effects), "and the real cart is read back in the same reply"
    assert [line.name for line in runner.session.cart] == ["Amul Milk"]
    # A yes tied to the earlier readback is refused; the failure produced a new one.
    await runner.dispatch_text("yes", heard=heard)  # type: ignore[arg-type]
    assert shop.checkout_calls == 0
    current = (runner.session.readback_version, runner.session.readback_hash)
    await runner.dispatch_text("yes", heard=current)  # type: ignore[arg-type]
    assert shop.checkout_calls == 1
    assert [line.name for line in shop.items] == ["Amul Milk"]


async def test_unknown_cart_after_failed_reconcile_blocks_confirmation() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("add_items", [("rice", 1.0, "kg")]))
    shop.update_error = CommerceError("network")
    real_get_cart = shop.get_cart
    calls = {"n": 0}

    async def fail_second_read() -> Cart:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CommerceError("cart read failed")
        return await real_get_cart()

    shop.get_cart = fail_second_read  # type: ignore[method-assign]
    await runner.dispatch_text("rice")

    assert runner.confirmable is False
    effects = await runner.dispatch_text("yes")
    assert shop.checkout_calls == 0
    assert "commerce_error" in says(effects)


# ------------------------------------------------------- checkout cannot execute twice
async def test_timeout_when_order_exists_is_found_and_not_repeated() -> None:
    shop = Shop()

    def placed_then_lost(s: Shop) -> Exception:
        s.place()
        return TimeoutError("response lost")

    shop.checkout_script = [placed_then_lost]
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("NEW-1",)
    assert shop.checkout_calls == 1
    assert "order_placed" in says(effects)


async def test_timeout_when_order_not_visible_is_unknown_and_second_yes_does_nothing() -> None:
    shop = Shop(checkout_script=[TimeoutError("response lost")])
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert runner.session.state is State.ORDER_UNKNOWN
    assert "order_unknown" in says(effects)
    assert runner.confirmable is False

    for reply in ("yes", "yes confirm", "place order"):
        await runner.dispatch_text(reply)
    assert shop.checkout_calls == 1, "the old code ordered again here"


async def test_unknown_outcome_blocks_a_brand_new_session(tmp_path: Path) -> None:
    journal = FileOrderJournal(tmp_path / "attempts.jsonl")
    shop = Shop(checkout_script=[TimeoutError("response lost")])
    first = await at_readback(shop, journal=journal)
    await first.dispatch_text("yes")

    second = await start(shop, journal=FileOrderJournal(tmp_path / "attempts.jsonl"))

    assert second.session.state is State.ORDER_UNKNOWN
    await second.dispatch_text("milk")
    await second.dispatch_text("yes")
    assert shop.checkout_calls == 1


async def test_attempt_survives_a_crash_and_recovers_when_the_order_appears(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    shop = Shop()
    # A crash right after journaling: PENDING on disk, the process is gone.
    FileOrderJournal(path).begin(new_attempt("h", "150", (), ("Amul Milk",)), lambda _: None)
    await shop.update_cart("home", [CartLine(spin_id="m1", sku_id="sku-m1", quantity=1)])
    shop.place("FROM-CRASH")

    runner = await TextTurnRunner.create(
        shop, Extractor(ADD_MILK), address_id="home", journal=FileOrderJournal(path)
    )
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))

    [attempt] = FileOrderJournal(path).attempts()
    assert attempt.status is AttemptStatus.PLACED
    assert attempt.order_ids == ("FROM-CRASH",)
    assert runner.session.state is not State.ORDER_UNKNOWN


async def test_crash_without_a_visible_order_blocks_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    shop = Shop()
    FileOrderJournal(path).begin(new_attempt("h", "150", (), ("Amul Milk",)), lambda _: None)

    runner = await start(shop, journal=FileOrderJournal(path))

    assert runner.session.state is State.ORDER_UNKNOWN
    assert unresolved_attempts(FileOrderJournal(path))


async def test_journal_is_written_before_checkout_is_called(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    seen: list[list[Any]] = []

    def inspect(s: Shop) -> OrderResult:
        seen.append(unresolved_attempts(FileOrderJournal(path)))
        return OrderResult(success=True, order_ids=[s.place().order_id])

    shop = Shop(checkout_script=[inspect])
    runner = await at_readback(shop, journal=FileOrderJournal(path))
    await runner.dispatch_text("yes")

    assert len(seen[0]) == 1, "the attempt was on disk when checkout ran"
    assert unresolved_attempts(FileOrderJournal(path)) == []


async def test_unusable_journal_means_no_checkout() -> None:
    class BrokenJournal(InMemoryOrderJournal):
        def begin(self, attempt: Any, guard: Any) -> None:
            raise JournalError("disk full")

    shop = Shop()
    runner = await at_readback(shop, journal=BrokenJournal())

    effects = await runner.dispatch_text("yes")

    assert shop.checkout_calls == 0
    assert "order_not_attempted" in says(effects)


async def test_unreadable_order_history_before_checkout_means_no_checkout() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    shop.orders_fail = True

    effects = await runner.dispatch_text("yes")

    assert shop.checkout_calls == 0
    assert "order_not_attempted" in says(effects)
    assert runner.session.state is State.READBACK


async def test_history_failure_during_reconcile_is_unknown_not_stuck() -> None:
    shop = Shop()

    def lost(s: Shop) -> Exception:
        s.orders_fail = True
        return TimeoutError("lost")

    shop.checkout_script = [lost]
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert runner.session.state is State.ORDER_UNKNOWN
    assert "order_unknown" in says(effects)
    follow_up = await runner.dispatch_text("hello?")
    assert says(follow_up) == ["order_unknown"], "the user is still answered"


async def test_multi_store_split_order_found_after_timeout_is_not_reordered() -> None:
    shop = Shop()

    def split(s: Shop) -> Exception:
        s.place("STORE-A", items=s.items[:1])  # only one line, a smaller total
        return TimeoutError("lost")

    shop.checkout_script = [split]
    runner = await start(shop, ("add_items", [("milk", 1.0, "litre"), ("rice", 1.0, "kg")]))
    await runner.dispatch_text("milk and rice")
    assert len(runner.session.cart) == 2

    await runner.dispatch_text("yes")

    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("STORE-A",)
    assert shop.checkout_calls == 1


async def test_partial_multi_store_keeps_placed_order_and_never_retries() -> None:
    shop = Shop()

    def partial(s: Shop) -> OrderResult:
        placed = s.place("STORE-A", items=s.items[:1])
        return OrderResult(success=False, partial=True, order_ids=[placed.order_id])

    shop.checkout_script = [partial]
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("STORE-A",)
    assert runner.session.order_partial is True
    assert says(effects)[-1] == "order_partial"
    await runner.dispatch_text("yes")
    assert shop.checkout_calls == 1


async def test_unrelated_order_placed_in_the_app_is_not_mistaken_for_ours() -> None:
    shop = Shop()

    def someone_else(s: Shop) -> Exception:
        s.orders.append(
            OrderSummary(
                order_id="APP-ORDER",
                items=[OrderSummaryItem(name="Chocolate", quantity=1)],
                total_inr=Decimal(999),
                status="PLACED",
            )
        )
        return TimeoutError("lost")

    shop.checkout_script = [someone_else]
    runner = await at_readback(shop)

    await runner.dispatch_text("yes")

    assert runner.session.state is State.ORDER_UNKNOWN
    assert runner.session.order_ids == ()


async def test_old_orders_are_not_mistaken_for_the_new_one() -> None:
    shop = Shop()
    shop.orders.append(
        OrderSummary(
            order_id="YESTERDAY",
            items=[OrderSummaryItem(name="Amul Milk", quantity=1)],
            total_inr=Decimal(150),
            status="DELIVERED",
        )
    )
    shop.checkout_script = [TimeoutError("lost")]
    runner = await at_readback(shop)

    await runner.dispatch_text("yes")

    assert runner.session.state is State.ORDER_UNKNOWN


async def test_reconciliation_waits_before_reading_and_keeps_polling() -> None:
    shop = Shop()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            shop.place()

    runner = await at_readback(shop)
    runner.coordinator = CheckoutCoordinator(
        shop,
        InMemoryOrderJournal(),
        CheckoutPolicy(reconcile_delays=(3.0, 7.0, 10.0)),
        sleep=sleep,
    )
    shop.checkout_script = [TimeoutError("lost")]

    await runner.dispatch_text("yes")

    assert slept[:2] == [3.0, 7.0]
    assert slept[0] > 0, "the docs ask to wait before checking"
    assert runner.session.state is State.DONE
    assert shop.checkout_calls == 1


async def test_placed_limit_stops_a_second_real_order(tmp_path: Path) -> None:
    journal = FileOrderJournal(tmp_path / "attempts.jsonl")
    policy = CheckoutPolicy(max_orders=1)
    shop = Shop()
    first = await at_readback(shop, journal=journal, policy=policy)
    await first.dispatch_text("yes")
    assert shop.checkout_calls == 1
    shop.items = []
    shop.cart_address = None

    second = await at_readback(shop, journal=journal, policy=policy)
    effects = await second.dispatch_text("yes")

    assert shop.checkout_calls == 1
    assert "order_limit" in says(effects)


async def test_order_above_the_money_limit_is_refused() -> None:
    shop = Shop()
    runner = await at_readback(shop, policy=CheckoutPolicy(max_total_inr=Decimal(100)))

    effects = await runner.dispatch_text("yes")

    assert shop.checkout_calls == 0
    assert "order_limit" in says(effects)


# ------------------------------------------------------- provider refusals
@pytest.mark.parametrize(
    "kind",
    [RejectionKind.MIN_ORDER, RejectionKind.OUT_OF_STOCK, RejectionKind.NOT_SERVICEABLE],
)
async def test_definitive_refusal_returns_to_a_fresh_readback(kind: RejectionKind) -> None:
    shop = Shop(checkout_script=[CommerceRejected("refused", kind=kind)])
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert "order_rejected" in says(effects)
    assert runner.session.state is State.READBACK
    assert says(effects)[-1] == "readback", "a fresh readback, then a fresh yes"
    assert runner.session.order_ids == ()
    await runner.dispatch_text("yes")
    assert shop.checkout_calls == 2
    assert runner.session.state is State.DONE


async def test_permission_refusal_places_nothing_and_keeps_swiggys_reason() -> None:
    refusal = CommerceRejected("refused", kind=RejectionKind.NOT_PERMITTED)
    refusal.provider_message = "Order not permitted for this account"
    journal = InMemoryOrderJournal()
    shop = Shop(checkout_script=[refusal])
    runner = await at_readback(shop, journal=journal)

    effects = await runner.dispatch_text("yes")

    assert "order_rejected" in says(effects)
    assert runner.session.order_ids == ()
    assert shop.checkout_calls == 1
    [attempt] = journal.attempts()
    assert attempt.status is AttemptStatus.NOT_PLACED
    assert "Order not permitted for this account" in attempt.note


@pytest.mark.parametrize("kind", [RejectionKind.DUPLICATE, RejectionKind.OTHER])
async def test_unclear_refusal_is_treated_as_unknown(kind: RejectionKind) -> None:
    shop = Shop(checkout_script=[CommerceRejected("refused", kind=kind)])
    runner = await at_readback(shop)

    await runner.dispatch_text("yes")
    await runner.dispatch_text("yes")

    assert runner.session.state is State.ORDER_UNKNOWN
    assert shop.checkout_calls == 1


async def test_refusal_with_a_new_order_in_history_counts_as_placed() -> None:
    shop = Shop()

    def refused_but_there(s: Shop) -> Exception:
        s.place()
        return CommerceRejected("refused", kind=RejectionKind.PAYMENT)

    shop.checkout_script = [refused_but_there]
    runner = await at_readback(shop)

    await runner.dispatch_text("yes")

    assert runner.session.state is State.DONE
    assert runner.session.order_ids == ("NEW-1",)


async def test_pending_payment_response_is_not_called_success() -> None:
    shop = Shop(
        checkout_script=[OrderResult(success=False, order_ids=["O1"], status="PENDING_PAYMENT")]
    )
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert "order_placed" not in says(effects)
    assert shop.checkout_calls == 1


# ------------------------------------------------------- checking before checkout
async def test_cart_changed_in_the_app_before_yes_is_read_back_not_ordered() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    shop.items[0] = shop.items[0].model_copy(update={"quantity": 5})

    effects = await runner.dispatch_text("yes")

    assert shop.checkout_calls == 0
    assert runner.session.state is State.READBACK
    assert runner.session.cart[0].quantity == 5
    assert "cart_changed" in says(effects)


async def test_new_cart_issue_before_yes_blocks_checkout() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    shop.issues = [CartIssue.NOT_SERVICEABLE]

    effects = await runner.dispatch_text("yes")

    assert shop.checkout_calls == 0
    assert "cart_blocked" in says(effects)


@pytest.mark.parametrize("issue", list(CartIssue))
async def test_every_cart_issue_blocks_the_readback(issue: CartIssue) -> None:
    shop = Shop(issues=[issue])
    runner = await start(shop)

    effects = await runner.dispatch_text("milk")

    assert "cart_blocked" in says(effects)
    assert "readback" not in says(effects)
    assert runner.confirmable is False
    text = response_text(effects, language="en-IN", commerce_mode="swiggy")
    assert "Shall I" not in text, "never asks for a yes it would refuse"


async def test_cod_unavailable_blocks_before_the_user_is_asked() -> None:
    shop = Shop(cod_available=False)
    runner = await start(shop)

    effects = await runner.dispatch_text("milk")

    assert "payment_unavailable" in runner.session.issues
    assert "cart_blocked" in says(effects)
    await runner.dispatch_text("yes")
    assert shop.checkout_calls == 0


async def test_unknown_payment_availability_is_treated_as_unavailable() -> None:
    shop = Shop(payment_fail=True)
    runner = await start(shop)

    await runner.dispatch_text("milk")

    assert "payment_unavailable" in runner.session.issues
    assert runner.confirmable is False


async def test_blank_address_details_from_the_cart_do_not_loop_forever() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    real_cart = shop._cart  # pyright: ignore[reportPrivateUsage]

    def blank_labels() -> Cart:
        return real_cart().model_copy(update={"delivery_label": "", "delivery_address": None})

    shop._cart = blank_labels  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]

    await runner.dispatch_text("yes")

    assert shop.checkout_calls == 1


async def test_checkout_switched_off_refuses_a_spoken_yes_and_records_no_attempt() -> None:
    journal = InMemoryOrderJournal()
    shop = Shop(checkout_script=[CommerceNotAttempted("disabled")])
    runner = await at_readback(shop, journal=journal)

    effects = await runner.dispatch_text("yes")

    assert "checkout_disabled" in says(effects)
    assert runner.session.state is State.READBACK
    assert unresolved_attempts(journal) == [], "nothing was sent, so nothing is left uncertain"


# ------------------------------------------------------- independent verification
async def test_order_missing_from_history_is_not_called_verified_without_tracking() -> None:
    shop = Shop(tracking_error=True)
    shop.checkout_script = [OrderResult(success=True, order_ids=["GHOST"], status="PLACED")]
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert runner.session.state is State.DONE
    assert runner.session.order_verified is False
    assert says(effects)[-1] == "order_placed_unverified"


async def test_tracking_is_the_second_source_for_verification() -> None:
    shop = Shop()
    shop.checkout_script = [OrderResult(success=True, order_ids=["TRACKED"], status="PLACED")]
    runner = await at_readback(shop)

    await runner.dispatch_text("yes")

    assert runner.session.order_verified is True
    assert runner.session.order_status == "On the way"


async def test_cancelled_status_is_reported_not_celebrated() -> None:
    shop = Shop()

    def cancelled(s: Shop) -> OrderResult:
        order = s.place()
        order.status = "CANCELLED"
        return OrderResult(success=True, order_ids=[order.order_id])

    shop.checkout_script = [cancelled]
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert says(effects)[-1] == "order_problem"
    assert runner.session.order_verified is False


async def test_total_mismatch_in_history_is_reported() -> None:
    shop = Shop()

    def wrong_total(s: Shop) -> OrderResult:
        order = s.place()
        order.total_inr = Decimal(500)
        return OrderResult(success=True, order_ids=[order.order_id])

    shop.checkout_script = [wrong_total]
    runner = await at_readback(shop)

    effects = await runner.dispatch_text("yes")

    assert says(effects)[-1] == "order_problem"


async def test_cancel_after_order_points_to_customer_care_and_claims_nothing() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    await runner.dispatch_text("yes")
    runner.extractor = Extractor(("cancel", []))

    effects = await runner.dispatch_text("please cancel it")

    assert says(effects) == ["cancel_via_support"]
    assert "080-67466729" in response_text(effects, language="en-IN", commerce_mode="swiggy")


# ------------------------------------------------------- existing cart protection
def _existing() -> CartItem:
    return CartItem(
        spin_id="r1",
        sku_id="sku-r1",
        name="Dosa Rice",
        pack_size="1 kg",
        quantity=2,
        unit_price_inr=Decimal(80),
    )


async def test_existing_cart_is_read_back_and_kept_when_adding() -> None:
    shop = Shop(items=[_existing()], cart_address="home")
    runner = await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="home")

    effects = await runner.dispatch(CallStarted(user_known=True, language="en-IN"))

    assert says(effects)[1:] == ["existing_cart", "readback"]
    assert shop.update_calls == 0, "starting a conversation changes nothing"
    await runner.dispatch_text("milk")
    assert sorted(item.name for item in shop.items) == ["Amul Milk", "Dosa Rice"]
    assert next(i for i in shop.items if i.name == "Dosa Rice").quantity == 2


async def test_cart_for_another_address_is_refused_without_touching_it() -> None:
    shop = Shop(items=[_existing()], cart_address="office")

    with pytest.raises(ExistingCartConflict):
        await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="home")

    assert shop.update_calls == 0
    assert len(shop.items) == 1


async def test_cancel_restores_the_cart_the_user_started_with() -> None:
    shop = Shop(items=[_existing()], cart_address="home")
    runner = await TextTurnRunner.create(
        shop, Extractor(ADD_MILK, ("cancel", [])), address_id="home"
    )
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))
    await runner.dispatch_text("milk")
    assert len(shop.items) == 2

    await runner.dispatch_text("cancel that")

    assert [item.name for item in shop.items] == ["Dosa Rice"]
    assert shop.items[0].quantity == 2
    assert [line.name for line in runner.session.cart] == ["Dosa Rice"]


async def test_brief_refusal_when_opening_the_basket_is_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seen live: get_cart refused as not serviceable, then fine moments later."""
    monkeypatch.setattr("app.orchestrator.turn_runner.START_CART_RETRY_SECONDS", 0)
    shop = Shop(items=[_existing()], cart_address="home")
    real_get_cart = shop.get_cart
    calls = {"n": 0}

    async def refuses_once() -> Cart:
        calls["n"] += 1
        if calls["n"] == 1:
            raise CommerceRejected("refused", kind=RejectionKind.NOT_SERVICEABLE)
        return await real_get_cart()

    shop.get_cart = refuses_once  # type: ignore[method-assign]

    runner = await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="home")

    assert calls["n"] == 2
    assert shop.update_calls == 0
    effects = await runner.dispatch(CallStarted(user_known=True, language="en-IN"))
    assert "existing_cart" in says(effects), "the real basket is still protected"


async def test_failed_cancel_never_claims_the_basket_was_cancelled() -> None:
    """Seen live: Ajji said "cancelled", then "could not reach the shop"."""
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("cancel", []))
    shop.update_error = CommerceError("refused")

    effects = await runner.dispatch_text("cancel that")

    assert "order_cancelled" not in says(effects)
    assert "commerce_error" in says(effects)
    assert "readback" in says(effects), "the user hears what is really still in the basket"
    assert [item.name for item in shop.items] == ["Amul Milk"]
    assert runner.session.cart != ()


async def test_successful_cancel_says_so_after_the_cart_is_empty() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("cancel", []))

    effects = await runner.dispatch_text("cancel that")

    assert says(effects) == ["order_cancelled", "ask_what_to_add"]
    assert shop.items == []
    assert runner.session.state is State.COLLECTING


async def test_cancel_does_not_overwrite_a_change_made_in_the_app() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("cancel", []))
    shop.items.append(_existing())
    writes = shop.update_calls

    effects = await runner.dispatch_text("cancel that")

    assert shop.update_calls == writes
    assert "order_cancelled" not in says(effects)
    assert "cart_changed" in says(effects)
    assert any(item.name == "Dosa Rice" for item in shop.items)


async def test_edit_made_in_the_app_between_turns_is_not_overwritten() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("add_items", [("rice", 1.0, "kg")]))
    shop.items.append(_existing())  # the user added this in the Swiggy app
    writes = shop.update_calls

    effects = await runner.dispatch_text("rice")

    assert shop.update_calls == writes
    assert "cart_changed" in says(effects)
    assert any(line.name == "Dosa Rice" for line in runner.session.cart)
    assert runner.session.state is State.READBACK


async def test_read_only_mode_leaves_the_real_cart_alone_and_says_so() -> None:
    shop = Shop(update_error=CommerceWriteDisabled("disabled"))
    runner = await start(shop)

    effects = await runner.dispatch_text("milk")

    assert "cart_writes_disabled" in says(effects)
    assert shop.items == []
    assert runner.session.cart == ()
    assert runner.confirmable is False


async def test_live_address_id_forms_are_treated_as_the_same_address() -> None:
    """Live: saved addresses are "<id>__<token>", the cart reports the bare "<id>"."""

    class LiveIds(Shop):
        async def get_addresses(self) -> list[Address]:
            return [Address(id="abc123__opaque", label="Home", full_address="1 Road")]

        async def update_cart(self, address_id: str, items: list[CartLine]) -> Cart:
            assert address_id == "abc123__opaque", "Swiggy gets the full ID back"
            cart = await super().update_cart(address_id, items)
            self.cart_address = "abc123"
            return cart.model_copy(update={"address_id": "abc123"})

    shop = LiveIds()
    runner = await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="abc123__opaque")
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))

    await runner.dispatch_text("milk")
    assert runner.session.issues == ()
    assert runner.confirmable

    await runner.dispatch_text("yes")
    assert shop.checkout_calls == 1


async def test_a_truly_different_address_is_still_refused() -> None:
    assert not same_address_id("abc__x", "abd__x")
    assert not same_address_id(None, "abc")
    assert same_address_id("abc__x", "abc")
    shop = Shop(items=[_existing()], cart_address="office__token")
    with pytest.raises(ExistingCartConflict):
        await TextTurnRunner.create(shop, Extractor(ADD_MILK), address_id="home")


# ------------------------------------------------------- clarification keeps context
async def test_options_are_spoken_and_answered_without_losing_the_rest_of_the_order() -> None:
    shop = Shop(catalogue={"milk": [MILK, OTHER_MILK], "rice": [RICE]})
    extractor = Extractor(
        ("add_items", [("rice", 1.0, "kg"), ("milk", 1.0, "litre"), ("rice", 2.0, "kg")])
    )
    runner = await TextTurnRunner.create(shop, extractor, address_id="home")
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))

    question = await runner.dispatch_text("rice and milk and rice")

    assert runner.session.state is State.CLARIFYING
    assert [line.name for line in runner.session.staged] == ["Dosa Rice"]
    assert shop.update_calls == 0
    spoken = response_text(question, language="en-IN", commerce_mode="swiggy")
    assert "Amul Milk" in spoken
    assert "Nandini Milk" in spoken
    assert "₹150" in spoken

    done = await runner.dispatch_text("second")

    assert runner.session.state is State.READBACK
    assert shop.update_calls == 1, "one turn's items are written once"
    assert {(i.name, i.quantity) for i in shop.items} == {("Dosa Rice", 3), ("Nandini Milk", 1)}
    assert "readback" in says(done)


async def test_llm_can_pick_an_option_only_within_the_real_list() -> None:
    shop = Shop(catalogue={"milk": [MILK, OTHER_MILK]})

    class Picks:
        def __init__(self, number: int) -> None:
            self.number = number
            self.first = True
            self.context: Any = None

        def extract(self, text: str, *, language: str = "kn-IN", context: Any = None) -> Any:
            if self.first:
                self.first = False
                return Extractor(ADD_MILK).extract(text, language=language)
            self.context = context
            return SimpleNamespace(
                intent="select_option",
                needs_clarification=False,
                clarification_question=None,
                option_number=self.number,
                items=[],
            )

    picks = Picks(9)
    runner = await TextTurnRunner.create(shop, picks, address_id="home")
    await runner.dispatch(CallStarted(user_known=True, language="en-IN"))
    await runner.dispatch_text("milk")

    effects = await runner.dispatch_text("the expensive imaginary one")
    assert picks.context is not None
    assert picks.context.options == ("Amul Milk 1 ltr", "Nandini Milk 1 ltr")
    assert runner.session.state is State.CLARIFYING, "an invented option adds nothing"
    assert shop.items == []
    assert says(effects) == ["clarification"]

    picks.number = 2
    await runner.dispatch_text("the nandini one")
    assert [item.name for item in shop.items] == ["Nandini Milk"]


async def test_usual_items_are_offered_as_choices() -> None:
    shop = Shop()
    runner = await start(shop, ("reorder_usual", []))

    effects = await runner.dispatch_text("the usual")

    assert runner.session.state is State.CLARIFYING
    assert runner.session.pending is not None
    assert runner.session.pending.kind == "usual_items"
    assert "Amul Milk" in response_text(effects, language="en-IN", commerce_mode="swiggy")
    assert shop.items == []


async def test_failed_extraction_changes_nothing_during_a_question() -> None:
    shop = Shop(catalogue={"milk": [MILK, OTHER_MILK]})
    runner = await start(shop)
    await runner.dispatch_text("milk")
    before = runner.session

    class Broken:
        def extract(self, text: str, *, language: str = "kn-IN", context: Any = None) -> Any:
            from app.llm.extractor import IntentExtractionError

            raise IntentExtractionError("bad output")

    runner.extractor = Broken()
    effects = await runner.dispatch_text("gibberish")

    assert runner.session == before
    assert says(effects) == ["intent_error"]


async def test_llm_confirm_is_never_a_yes_even_with_valid_context() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("confirm", []))

    await runner.dispatch_text("sounds good I suppose")

    assert shop.checkout_calls == 0
    assert runner.session.state is State.READBACK


async def test_removal_with_a_native_script_name_asks_rather_than_guessing() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    runner.extractor = Extractor(("remove_items", [("ಸಕ್ಕರೆ", 1.0, None)]))

    effects = await runner.dispatch_text("ಸಕ್ಕರೆ ತೆಗೆಯಿರಿ")

    assert runner.session.state is State.CLARIFYING
    assert [item.name for item in shop.items] == ["Amul Milk"]
    assert says(effects)[-1] == "clarification"


async def test_quantity_change_keeps_the_pack_and_invalidates_approval() -> None:
    shop = Shop()
    runner = await at_readback(shop)
    old = runner.session.readback_hash
    runner.extractor = Extractor(("change_quantity", [("milk", 3.0, "packet")]))

    await runner.dispatch_text("make it three")

    assert shop.items[0].quantity == 3
    assert runner.session.readback_hash != old
    await runner.dispatch_text("yes")
    assert shop.checkout_calls == 1
