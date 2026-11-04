"""State machine tests. These FAIL with NotImplementedError until Pranav implements
`transition()` — that is expected. Agents: extend tests, don't implement the FSM."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.orchestrator.state_machine import (
    CallCommerce,
    CallStarted,
    CartEntry,
    CartSnapshotReceived,
    CheckoutFailed,
    CheckoutSucceeded,
    Clarification,
    Effect,
    Event,
    ItemsExtracted,
    NotifyFamily,
    PlaceOrder,
    ReconcileResult,
    Say,
    Session,
    State,
    UserConfirmed,
    UserDone,
    UserRejected,
    UserUtterance,
    cart_hash,
    transition,
)

MILK = CartEntry("milk_500ml", "sku_milk_500ml", "Nandini Toned Milk", "500 ml", 2, 26)
RICE_1KG = CartEntry("rice_1kg", "sku_rice_1kg", "Sona Masoori Rice", "1 kg", 2, 78)
ATTA_5KG = CartEntry("atta_5kg", "sku_atta_5kg", "Aashirvaad Atta", "5 kg", 1, 299)


def s(state: State, **kw: object) -> Session:
    return replace(Session(state=state), **kw)  # type: ignore[arg-type]


def place_orders(effects: list[Effect]) -> list[PlaceOrder]:
    return [e for e in effects if isinstance(e, PlaceOrder)]


def says(effects: list[Effect]) -> list[str]:
    return [e.template for e in effects if isinstance(e, Say)]


def hash_for_cart(cart: tuple[CartEntry, ...]) -> str:
    return cart_hash(cart)


# ------------------------------------------------------------ diagram edges
def test_greeting_known_user_to_collecting() -> None:
    nxt, eff = transition(Session(), CallStarted(user_known=True))
    assert nxt.state is State.COLLECTING
    assert says(eff)


def test_greeting_unknown_user_ends() -> None:
    nxt, eff = transition(Session(), CallStarted(user_known=False))
    assert nxt.state is State.DONE
    assert not place_orders(eff)


def test_collecting_add_triggers_search_and_filler() -> None:
    nxt, eff = transition(s(State.COLLECTING), UserUtterance("2 kg akki", "add"))
    assert nxt.state is State.COLLECTING
    assert any(isinstance(e, CallCommerce) and e.op == "search" for e in eff)


def test_search_filler_precedes_commerce_call() -> None:
    _, eff = transition(s(State.COLLECTING), UserUtterance("2 kg akki", "add"))
    assert isinstance(eff[0], Say) and eff[0].template == "filler_search"
    assert isinstance(eff[1], CallCommerce) and eff[1].op == "search"


def test_reorder_usual_uses_go_to_items() -> None:
    _, eff = transition(s(State.COLLECTING), UserUtterance("yaavaagalu tarodu", "reorder_usual"))
    assert any(isinstance(e, CallCommerce) and e.op == "go_to_items" for e in eff)


def test_reorder_filler_precedes_commerce_call() -> None:
    _, eff = transition(s(State.COLLECTING), UserUtterance("yaavaagalu tarodu", "reorder_usual"))
    assert isinstance(eff[0], Say) and eff[0].template == "filler_search"
    assert isinstance(eff[1], CallCommerce) and eff[1].op == "go_to_items"


def test_reorder_usual_results_go_to_readback() -> None:
    sess, _ = transition(s(State.COLLECTING), UserUtterance("yaavaagalu tarodu", "reorder_usual"))
    nxt, _ = transition(sess, ItemsExtracted(resolved=(MILK,)))
    assert nxt.state is State.READBACK


def test_items_extracted_adds_to_cart_and_bumps_version() -> None:
    nxt, _ = transition(s(State.COLLECTING), ItemsExtracted(resolved=(MILK,)))
    assert MILK in nxt.cart
    assert nxt.cart_version == 1


def test_cart_items_merge_by_sku_id() -> None:
    extra_milk = CartEntry("milk_500ml", "sku_milk_500ml", "Nandini Toned Milk", "500 ml", 3, 26)
    nxt, _ = transition(
        s(State.COLLECTING, cart=(MILK,), cart_version=1),
        ItemsExtracted(resolved=(extra_milk,)),
    )
    assert len(nxt.cart) == 1
    assert nxt.cart[0].sku_id == MILK.sku_id
    assert nxt.cart[0].quantity == 5


def test_collecting_to_clarifying_on_ambiguity() -> None:
    q = Clarification("ambiguous_item", "Which dal?")
    nxt, eff = transition(s(State.COLLECTING), ItemsExtracted(resolved=(), clarification=q))
    assert nxt.state is State.CLARIFYING
    assert nxt.pending == q
    assert says(eff)


def test_clarifying_back_to_readback_when_resolved() -> None:
    q = Clarification("ambiguous_item", "Which dal?")
    nxt, _ = transition(s(State.CLARIFYING, pending=q), ItemsExtracted(resolved=(MILK,)))
    assert nxt.state is State.READBACK
    assert nxt.pending is None


def test_collecting_done_to_readback() -> None:
    nxt, eff = transition(s(State.COLLECTING, cart=(MILK,), cart_version=1), UserDone())
    assert nxt.state is State.READBACK
    assert any(isinstance(effect, CallCommerce) and effect.op == "get_cart" for effect in eff)


def test_readback_say_carries_lines_total_payment_and_address() -> None:
    sess = s(
        State.COLLECTING,
        cart=(MILK,),
        cart_version=1,
        total_inr=52,
        address_label="Home",
    )
    readback_state, effects = transition(sess, UserDone())
    assert any(isinstance(effect, CallCommerce) and effect.op == "get_cart" for effect in effects)
    _, eff = transition(
        readback_state,
        CartSnapshotReceived(
            cart=(MILK,),
            total_inr=52,
            address_id=None,
            address_label="Home",
            delivery_address="123 Home Road",
        ),
    )
    readback = next(e for e in eff if isinstance(e, Say) and e.template == "readback")
    assert readback.data == {
        "lines": "2 x 500 ml Nandini Toned Milk",
        "total": "52",
        "payment": "COD",
        "address_label": "Home",
        "address": "123 Home Road",
    }


def test_snapshot_change_invalidates_confirmed_cart_before_checkout() -> None:
    current = s(State.PLACING, cart=(MILK,), cart_version=1, total_inr=52, address_id="home")
    updated, effects = transition(
        current,
        CartSnapshotReceived(
            cart=(CartEntry("milk", "sku-milk", "Milk", "1 L", 1, 60),),
            total_inr=60,
            address_id="home",
            address_label="Home",
            delivery_address="123 Home Road",
        ),
    )
    assert updated.state is State.READBACK
    assert updated.confirmed_version is None
    assert [effect.template for effect in effects if isinstance(effect, Say)] == [
        "cart_changed",
        "readback",
    ]
    assert not place_orders(effects)


def test_done_with_empty_cart_stays_collecting() -> None:
    nxt, eff = transition(s(State.COLLECTING), UserDone())
    assert nxt.state is State.COLLECTING
    assert not place_orders(eff)


def test_readback_reject_to_collecting() -> None:
    nxt, eff = transition(s(State.READBACK, cart=(MILK,), cart_version=1), UserRejected())
    assert nxt.state is State.COLLECTING
    assert not place_orders(eff)


def test_readback_confirm_places_exactly_one_order() -> None:
    nxt, eff = transition(
        s(State.READBACK, cart=(MILK,), cart_version=1), UserConfirmed(1, hash_for_cart((MILK,)))
    )
    assert nxt.state is State.PLACING
    orders = place_orders(eff)
    assert len(orders) == 1
    assert orders[0].cart_version == 1
    assert orders[0].payment_method == "COD"
    assert nxt.confirmed_version == 1


def test_confirmation_and_place_order_carry_exact_cart_hash() -> None:
    cart_hash = hash_for_cart((MILK,))
    nxt, eff = transition(
        s(State.READBACK, cart=(MILK,), cart_version=1), UserConfirmed(1, cart_hash)
    )
    orders = place_orders(eff)
    assert nxt.state is State.PLACING
    assert len(orders) == 1
    assert orders[0].cart_hash == cart_hash


def test_unclear_answer_at_readback_is_not_yes() -> None:
    nxt, eff = transition(
        s(State.READBACK, cart=(MILK,), cart_version=1), UserUtterance("hmm", "unclear")
    )
    assert nxt.state is State.READBACK
    assert not place_orders(eff)


def test_second_unclear_answer_returns_to_collecting() -> None:
    sess = s(State.READBACK, cart=(MILK,), cart_version=1, reask_count=1)
    nxt, eff = transition(sess, UserUtterance("still not sure", "unclear"))
    assert nxt.state is State.COLLECTING
    assert nxt.reask_count == 0
    assert not place_orders(eff)


def test_placing_success_to_done() -> None:
    nxt, eff = transition(
        s(State.PLACING, cart=(MILK,), cart_version=1, confirmed_version=1, attempt_no=1),
        CheckoutSucceeded(("IM000001",)),
    )
    assert nxt.state is State.DONE
    assert nxt.order_ids == ("IM000001",)
    assert not place_orders(eff)


def test_placing_failure_to_failed_and_reconciles() -> None:
    nxt, eff = transition(
        s(State.PLACING, cart=(MILK,), cart_version=1, attempt_no=1), CheckoutFailed("timeout")
    )
    assert nxt.state is State.FAILED
    assert any(isinstance(e, CallCommerce) and e.op == "get_orders" for e in eff)
    assert not place_orders(eff), "never blind-retry checkout"


def test_failed_reconcile_found_to_done() -> None:
    nxt, eff = transition(
        s(State.FAILED, cart=(MILK,), cart_version=1, attempt_no=1),
        ReconcileResult(
            order_found=True,
            order_ids=("IM000001",),
            cart_hash=hash_for_cart((MILK,)),
        ),
    )
    assert nxt.state is State.DONE
    assert not place_orders(eff)


def test_failed_reconcile_requires_matching_cart_hash() -> None:
    nxt, eff = transition(
        s(State.FAILED, cart=(MILK,), cart_version=1, attempt_no=1),
        ReconcileResult(order_found=True, order_ids=("IM000001",), cart_hash="wrong"),
    )
    assert nxt.state is State.READBACK
    assert not place_orders(eff)


def test_failed_reconcile_not_found_to_readback() -> None:
    nxt, eff = transition(
        s(State.FAILED, cart=(MILK,), cart_version=1, attempt_no=1),
        ReconcileResult(order_found=False),
    )
    assert nxt.state is State.READBACK
    assert not place_orders(eff)


def test_retry_after_failed_uses_next_attempt_no() -> None:
    sess = s(State.READBACK, cart=(MILK,), cart_version=1, attempt_no=1)
    _, eff = transition(sess, UserConfirmed(1, hash_for_cart((MILK,))))
    assert [o.attempt_no for o in place_orders(eff)] == [2]


def test_terminal_failure_notifies_family() -> None:
    _, eff = transition(
        s(State.PLACING, cart=(MILK,), cart_version=1, attempt_no=1),
        CheckoutFailed("Address not serviceable", terminal=True),
    )
    assert any(isinstance(e, NotifyFamily) for e in eff)


def test_min_order_failure_enters_clarifying() -> None:
    nxt, _ = transition(
        s(State.READBACK, cart=(MILK,), cart_version=1),
        CheckoutFailed("Minimum order not met: ₹99"),
    )
    assert nxt.state is State.CLARIFYING
    assert nxt.pending is not None
    assert nxt.pending.kind == "min_order"


def test_cart_expired_rebuilds_from_adapter() -> None:
    nxt, eff = transition(
        s(State.READBACK, cart=(MILK,), cart_version=1), CheckoutFailed("cart expired")
    )
    assert nxt.state is State.COLLECTING
    assert any(isinstance(e, CallCommerce) and e.op == "get_cart" for e in eff)


def test_cart_changed_during_placing_returns_to_readback() -> None:
    nxt, eff = transition(
        s(State.PLACING, cart=(MILK,), cart_version=1), CheckoutFailed("cart_changed")
    )
    assert nxt.state is State.READBACK
    assert "cart_changed" in says(eff)
    assert not place_orders(eff)


def test_expired_authentication_fails_without_state_machine_retry() -> None:
    nxt, eff = transition(s(State.COLLECTING), CheckoutFailed("401 token expired", terminal=True))
    assert nxt.state is State.FAILED
    assert any(isinstance(e, NotifyFamily) for e in eff)


def test_rate_limit_emits_filler_without_retrying_in_state_machine() -> None:
    nxt, eff = transition(s(State.COLLECTING), CheckoutFailed("429 Retry-After: 1"))
    assert nxt == s(State.COLLECTING)
    assert says(eff) == ["filler_search"]
    assert not any(isinstance(e, CallCommerce) for e in eff)


# ------------------------------------------------------------ readback edits
def test_edit_after_readback_invalidates_confirmation() -> None:
    sess = s(State.READBACK, cart=(MILK,), cart_version=1)
    sess, _ = transition(sess, UserRejected())
    sess, _ = transition(sess, ItemsExtracted(resolved=(ATTA_5KG,)))
    assert sess.cart_version == 2
    sess, _ = transition(sess, UserDone())
    assert sess.state is State.READBACK
    nxt, eff = transition(sess, UserConfirmed(1, hash_for_cart((MILK,))))  # stale yes for old cart
    assert not place_orders(eff)
    assert nxt.state is State.READBACK


def test_stale_confirmation_is_ignored() -> None:
    _, eff = transition(
        s(State.READBACK, cart=(MILK,), cart_version=3), UserConfirmed(2, hash_for_cart((MILK,)))
    )
    assert not place_orders(eff)


# ------------------------------------------------------------ pack sizes
def test_auto_resolved_pack_is_always_spoken() -> None:
    _, eff = transition(
        s(State.COLLECTING), ItemsExtracted(resolved=(RICE_1KG,), auto_resolved=(RICE_1KG,))
    )
    assert "pack_confirm" in says(eff)


def test_only_bigger_pack_asks_instead_of_rounding_up() -> None:
    q = Clarification("only_bigger_pack", "Only 1 kg available. OK?", options=(RICE_1KG,))
    nxt, eff = transition(s(State.COLLECTING), ItemsExtracted(resolved=(), clarification=q))
    assert nxt.state is State.CLARIFYING
    assert RICE_1KG not in nxt.cart
    assert not place_orders(eff)


def test_out_of_stock_suggests_substitute_not_silent_add() -> None:
    q = Clarification("out_of_stock", "Coriander is out. Try X?", options=(MILK,))
    nxt, _ = transition(s(State.COLLECTING), ItemsExtracted(resolved=(), clarification=q))
    assert nxt.state is State.CLARIFYING
    assert nxt.cart == ()


# ------------------------------------------------------------ property tests
entries = st.sampled_from([MILK, RICE_1KG, ATTA_5KG])
events: st.SearchStrategy[Event] = st.one_of(
    st.builds(CallStarted, user_known=st.booleans()),
    st.builds(
        UserUtterance,
        text=st.just("x"),
        intent=st.sampled_from(
            ["add", "remove", "change_qty", "reorder_usual", "done", "yes", "no", "unclear"]
        ),
    ),
    st.builds(ItemsExtracted, resolved=st.tuples(entries)),
    st.builds(
        ItemsExtracted,
        resolved=st.just(()),
        auto_resolved=st.just(()),
        clarification=st.just(Clarification("ambiguous_item", "?")),
    ),
    st.builds(UserConfirmed, cart_version=st.integers(0, 6)),
    st.just(UserRejected()),
    st.just(UserDone()),
    st.builds(CheckoutSucceeded, order_ids=st.just(("IM1",))),
    st.builds(CheckoutFailed, reason=st.just("timeout"), terminal=st.booleans()),
    st.builds(ReconcileResult, order_found=st.booleans()),
)


@settings(max_examples=300, deadline=None)
@given(st.lists(events, max_size=40))
def test_property_place_order_only_after_confirming_latest_cart(seq: list[Event]) -> None:
    sess = Session()
    seen: set[tuple[int, int]] = set()
    for ev in seq:
        prev = sess
        sess, eff = transition(sess, ev)
        for po in place_orders(eff):
            # must come from a READBACK/CONFIRMED step with a yes to the CURRENT cart
            assert prev.state in (State.READBACK, State.CONFIRMED)
            assert isinstance(ev, UserConfirmed)
            assert ev.cart_version == prev.cart_version == po.cart_version
            assert sess.confirmed_version == sess.cart_version
            assert sess.cart, "never order an empty cart"
            key = (po.cart_version, po.attempt_no)
            assert key not in seen, "PlaceOrder at most once per cart version/attempt"
            seen.add(key)
        assert len(place_orders(eff)) <= 1


@settings(max_examples=200, deadline=None)
@given(st.lists(events, max_size=40))
def test_property_transition_is_pure(seq: list[Event]) -> None:
    sess = Session()
    for ev in seq:
        a = transition(sess, ev)
        b = transition(sess, ev)
        assert a == b
        sess = a[0]


@pytest.mark.parametrize("state", list(State))
def test_every_state_handles_every_event_type_without_crashing(state: State) -> None:
    sample: list[Event] = [
        CallStarted(True),
        UserUtterance("x", "unclear"),
        ItemsExtracted(resolved=()),
        UserConfirmed(0),
        UserRejected(),
        UserDone(),
        CheckoutSucceeded(("IM1",)),
        CheckoutFailed("x"),
        ReconcileResult(order_found=False),
    ]
    for ev in sample:
        transition(s(state), ev)
