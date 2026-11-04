"""Table booking: the live slot format, the pure dialog, and the safety of the booking call."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from mcp.types import TextContent

from app.commerce.base import CommerceError, CommerceNotAttempted, CommerceRejected, RejectionKind
from app.commerce.order_journal import (
    AttemptStatus,
    FileOrderJournal,
    InMemoryOrderJournal,
    unresolved_attempts,
)
from app.commerce.swiggy import OAUTH_REDIRECT_URI, KeyringTokenStorage
from app.dining.base import DiningRestaurant, TableSlot
from app.dining.mock import MockDineout
from app.dining.state_machine import (
    BookingPlaced,
    BookTable,
    Confirmed,
    DetailsGiven,
    DiningSession,
    DiningState,
    OptionChosen,
    RestaurantsFound,
    SlotsFound,
    Unclear,
    booking_hash,
    can_confirm,
    transition,
)
from app.dining.swiggy import SWIGGY_DINEOUT_URL, SwiggyDineout, parse_free_slots
from app.dining.workflow import KNOWN_AREAS, DiningWorkflow
from app.llm.extractor import GroceryIntent
from app.orchestrator.checkout import CheckoutPolicy
from app.orchestrator.state_machine import Effect, Say
from tests.test_swiggy_adapter import (
    FakeKeyring,
    FakeMcpResult,
    _no_callback,
    _no_redirect,
    _patch_mcp,
    _scripted,
)  # pyright: ignore[reportPrivateUsage]

LIVE_SLOTS = (Path(__file__).parent / "fixtures" / "dineout_slots_live.txt").read_text()
TODAY = date(2026, 11, 5)
TOMORROW = "2026-11-06"


# ------------------------------------------------- the live text format
def test_live_slot_text_parses_free_slots_with_exact_booking_fields() -> None:
    slots = parse_free_slots(LIVE_SLOTS, TOMORROW)

    assert len(slots) == 43  # 20 lunch + 23 dinner, as the live summary says
    assert {slot.item_id for slot in slots} == {"176866-47409"}, "only the free deal"
    first = slots[0]
    assert (first.date, first.time, first.display_time) == (TOMORROW, "12:00", "12:00 PM")
    assert first.slot_id == 1
    assert first.reservation_time == 1793946600
    assert next(slot for slot in slots if slot.time == "19:00").display_time == "07:00 PM"


def test_a_date_without_slots_has_none() -> None:
    assert parse_free_slots(LIVE_SLOTS, "2030-01-01") == []


def test_paid_only_day_offers_nothing() -> None:
    paid_only = LIVE_SLOTS.replace(f"{TOMORROW} [FREE]", f"{TOMORROW} [PAID ₹25 Pre-Book]")

    assert parse_free_slots(paid_only, TOMORROW) == []


@pytest.mark.parametrize("text", ["", "Something changed completely", "2026-11-06 12:00 PM"])
def test_unrecognized_format_finds_nothing_rather_than_guessing(text: str) -> None:
    assert parse_free_slots(text, TOMORROW) == []


# ------------------------------------------------- the adapter
def _adapter(*, booking: bool = True) -> SwiggyDineout:
    return SwiggyDineout(
        endpoint=SWIGGY_DINEOUT_URL,
        redirect_uri=OAUTH_REDIRECT_URI,
        storage=KeyringTokenStorage(FakeKeyring()),
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
        allow_real_checkout=booking,
    )


def _text_result(text: str, structured: object | None = None) -> FakeMcpResult:
    result = FakeMcpResult(structured if structured is not None else {})
    result.content = [TextContent(type="text", text=text)]
    return result


SLOT = TableSlot(TOMORROW, "19:00", "07:00 PM", 1794000000, 1, "176866-47409")


def test_search_reads_ids_from_text_and_cards_from_render(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(
        monkeypatch,
        search_restaurants_dineout=_text_result("1. Bologna (ID: 176866)\n2. Other (ID: 2)"),
        render_restaurants_dineout=FakeMcpResult(
            {
                "restaurants": [
                    {
                        "id": "176866",
                        "name": "Bologna",
                        "locality": "Indiranagar",
                        "costForTwo": "₹1200",
                    }
                ]
            }
        ),
    )

    found = asyncio.run(_adapter().search("Italian", 12.9, 77.5))

    assert found == [DiningRestaurant("176866", "Bologna", "Indiranagar", "₹1200")]
    assert calls[1]["arguments"]["restaurantIds"] == ["176866", "2"]  # type: ignore[index]


def test_no_results_makes_no_render_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(monkeypatch, search_restaurants_dineout=_text_result("Found 0"))

    assert asyncio.run(_adapter().search("Italian", 12.9, 77.5)) == []
    assert len(calls) == 1


def test_booking_sends_exact_slot_fields_and_no_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scripted(
        monkeypatch,
        book_table=FakeMcpResult(
            {"success": True, "data": {"orderId": "D1", "status": "CONFIRMED"}}
        ),
    )

    result = asyncio.run(_adapter().book("176866", SLOT, 2, 12.9, 77.5))

    assert result.order_id == "D1"
    assert calls[0]["arguments"] == {
        "restaurantId": "176866",
        "slotId": 1,
        "itemId": "176866-47409",
        "reservationTime": 1794000000,
        "guestCount": 2,
        "latitude": 12.9,
        "longitude": 77.5,
    }


def test_booking_needs_its_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(_adapter(booking=False).book("1", SLOT, 2, 0.0, 0.0))
    assert calls == []


def test_booking_without_an_id_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(monkeypatch, book_table=_text_result("Done"))

    with pytest.raises(CommerceError):
        asyncio.run(_adapter().book("1", SLOT, 2, 0.0, 0.0))


def test_free_booking_that_wants_payment_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(
        monkeypatch,
        book_table=FakeMcpResult(
            {"success": True, "data": {"orderId": "D1", "status": "PENDING_PAYMENT"}}
        ),
    )

    with pytest.raises(CommerceError):
        asyncio.run(_adapter().book("1", SLOT, 2, 0.0, 0.0))


def test_status_reads_structured_or_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(monkeypatch, get_booking_status=_text_result("Status: confirmed"))
    assert asyncio.run(_adapter().status("D1")).status == "confirmed"

    _scripted(monkeypatch, get_booking_status=_text_result("?"))
    with pytest.raises(CommerceError):
        asyncio.run(_adapter().status("D1"))


@pytest.mark.parametrize(
    "tool", ["create_cart", "confirm_order", "checkout", "get_payment_options"]
)
def test_paid_flow_tools_cannot_be_called(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    calls = _patch_mcp(monkeypatch, FakeMcpResult({}))

    with pytest.raises(CommerceNotAttempted):
        asyncio.run(_adapter()._call(tool, {}))  # pyright: ignore[reportPrivateUsage]
    assert calls == []


# ------------------------------------------------- the pure dialog
RESTAURANT = DiningRestaurant("d1", "Toscano", "Indiranagar")


def _at_readback() -> DiningSession:
    session = DiningSession(
        query="italian", area="indiranagar", date=TOMORROW, time="19:00", guests=2
    )
    session, _ = transition(session, RestaurantsFound((RESTAURANT,)))
    session, _ = transition(session, OptionChosen(0))
    session, _ = transition(session, SlotsFound((SLOT,)))
    assert session.state is DiningState.READBACK
    return session


def test_questions_come_one_at_a_time_in_order() -> None:
    session = DiningSession()
    expected = [
        (DetailsGiven(), "dining_ask_query"),
        (DetailsGiven(query="italian"), "dining_ask_area"),
        (DetailsGiven(area="indiranagar"), "dining_ask_date"),
        (DetailsGiven(date=TOMORROW), "dining_ask_time"),
        (DetailsGiven(time="19:00"), "dining_ask_guests"),
    ]
    for event, template in expected:
        session, effects = transition(session, event)
        assert [e.template for e in effects if isinstance(e, Say)] == [template]


def test_exact_time_goes_to_readback_and_binds_it() -> None:
    session = _at_readback()

    assert can_confirm(session)
    assert session.readback_hash == booking_hash(session)


def test_other_times_are_offered_never_chosen() -> None:
    session = DiningSession(query="q", area="a", date=TOMORROW, time="18:00", guests=2)
    session, _ = transition(session, RestaurantsFound((RESTAURANT,)))
    session, _ = transition(session, OptionChosen(0))
    close = TableSlot(TOMORROW, "18:30", "06:30 PM", 2, 1, "i")
    far = TableSlot(TOMORROW, "12:00", "12:00 PM", 1, 1, "i")

    session, effects = transition(session, SlotsFound((far, close)))

    assert session.state is DiningState.CHOOSING_SLOT
    assert session.slot is None
    assert session.slots[0] == close
    assert "dining_time_unavailable" in [e.template for e in effects if isinstance(e, Say)]


def test_only_a_yes_to_this_exact_readback_books() -> None:
    session = _at_readback()

    for version, digest in (
        (session.version - 1, session.readback_hash),
        (session.version, "wrong"),
    ):
        again, effects = transition(session, Confirmed(version, digest))
        assert not any(isinstance(e, BookTable) for e in effects)
        assert again.state is DiningState.READBACK

    booked, effects = transition(session, Confirmed(session.version, session.readback_hash))
    assert booked.state is DiningState.BOOKING
    assert [e for e in effects if isinstance(e, BookTable)]


def test_changing_any_detail_clears_the_approval() -> None:
    session = _at_readback()

    changed, _ = transition(session, DetailsGiven(guests=4))

    assert changed.readback_version is None
    assert not can_confirm(changed)
    _, effects = transition(changed, Confirmed(session.version, session.readback_hash))
    assert not any(isinstance(e, BookTable) for e in effects)


def test_unclear_never_books() -> None:
    session = _at_readback()
    for _ in range(4):
        session, effects = transition(session, Unclear())
        assert not any(isinstance(e, BookTable) for e in effects)
    assert session.state is DiningState.READBACK


def test_finished_and_unknown_states_never_book_again() -> None:
    session = _at_readback()
    session, _ = transition(session, Confirmed(session.version, session.readback_hash))
    session, _ = transition(session, BookingPlaced("D1"))
    assert session.state is DiningState.VERIFYING
    _, effects = transition(session, Confirmed(session.version, session.readback_hash))
    assert not any(isinstance(e, BookTable) for e in effects)


# ------------------------------------------------- the workflow
def _intent(**fields: Any) -> GroceryIntent:
    return GroceryIntent(intent=fields.pop("intent", "book_table"), items=[], **fields)


async def _ready(
    shop: MockDineout | None = None, journal: Any = None, policy: Any = None
) -> tuple[DiningWorkflow, MockDineout]:
    shop = shop or MockDineout()
    workflow = DiningWorkflow(shop, journal or InMemoryOrderJournal(), policy, today=TODAY)
    await workflow.apply(
        "x",
        _intent(query="Italian", area="Bengaluru", date=TOMORROW, time="19:00", guests=2),
    )
    assert workflow.session.state is DiningState.CHOOSING_RESTAURANT
    assert await workflow.direct("first") is not None
    assert workflow.session.state is DiningState.READBACK
    return workflow, shop


def _says(effects: list[Effect] | None) -> list[str]:
    return [e.template for e in effects or [] if isinstance(e, Say)]


async def test_booking_journey_with_verification() -> None:
    workflow, shop = await _ready()

    effects = await workflow.direct("yes")

    assert _says(effects) == ["dining_booked"]
    assert workflow.session.state is DiningState.DONE
    assert workflow.session.verified is True
    assert shop.book_calls == 1
    assert workflow.view().order_ids == ("DN000001",)


@pytest.mark.parametrize("yes", ["ಹೌದು", "हाँ", "yes"])
async def test_spoken_yes_books_in_every_language(yes: str) -> None:
    workflow, shop = await _ready()

    await workflow.direct(yes)

    assert shop.book_calls == 1


async def test_no_returns_without_booking() -> None:
    workflow, shop = await _ready()

    effects = await workflow.direct("ಬೇಡ")

    assert _says(effects) == ["what_to_change"]
    assert shop.book_calls == 0


async def test_yes_tied_to_an_older_readback_does_not_book() -> None:
    workflow, shop = await _ready()

    await workflow.direct("yes", (workflow.session.version - 1, workflow.session.readback_hash))

    assert shop.book_calls == 0


async def test_unknown_place_is_asked_not_guessed() -> None:
    shop = MockDineout()
    workflow = DiningWorkflow(shop, InMemoryOrderJournal(), today=TODAY)

    effects = await workflow.apply(
        "x", _intent(query="Italian", area="Atlantis", date=TOMORROW, time="19:00", guests=2)
    )

    assert _says(effects) == ["filler_search", "dining_area_unknown"]
    assert workflow.session.area == ""


async def test_past_or_invalid_dates_are_asked_again() -> None:
    workflow = DiningWorkflow(MockDineout(), InMemoryOrderJournal(), today=TODAY)

    for bad in ("2020-01-01", "tomorrow"):
        await workflow.apply("x", _intent(query="q", area="Delhi", date=bad))
        assert workflow.session.date == ""


async def test_lost_reply_is_unknown_and_never_retried(tmp_path: Path) -> None:
    journal = FileOrderJournal(tmp_path / "attempts.jsonl")
    shop = MockDineout()
    workflow, _ = await _ready(shop, journal)
    shop.fail_next_book = TimeoutError("lost")

    effects = await workflow.direct("yes")

    assert _says(effects) == ["order_unknown"]
    assert workflow.outcome_unknown
    await workflow.direct("yes")
    await workflow.confirm(workflow.session.version, workflow.session.readback_hash)
    assert shop.book_calls == 1
    [attempt] = unresolved_attempts(FileOrderJournal(tmp_path / "attempts.jsonl"))
    assert attempt.domain == "dining"
    assert attempt.status is AttemptStatus.UNKNOWN


async def test_journal_is_written_before_the_booking_call(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    seen: list[int] = []

    class Checking(MockDineout):
        async def book(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(len(unresolved_attempts(FileOrderJournal(path))))
            return await super().book(*args, **kwargs)

    workflow, _ = await _ready(Checking(), FileOrderJournal(path))

    await workflow.direct("yes")

    assert seen == [1]
    assert unresolved_attempts(FileOrderJournal(path)) == []


async def test_definite_refusal_offers_times_again_without_booking() -> None:
    shop = MockDineout()
    workflow, _ = await _ready(shop)
    shop.fail_next_book = CommerceRejected("gone", kind=RejectionKind.OUT_OF_STOCK)

    effects = await workflow.direct("yes")

    assert "dining_booking_refused" in _says(effects)
    assert workflow.session.order_id == ""
    assert not workflow.outcome_unknown


async def test_unclear_refusal_is_unknown() -> None:
    shop = MockDineout()
    workflow, _ = await _ready(shop)
    shop.fail_next_book = CommerceRejected("hmm", kind=RejectionKind.OTHER)

    await workflow.direct("yes")

    assert workflow.outcome_unknown


async def test_slot_gone_before_booking_is_never_booked() -> None:
    shop = MockDineout()
    workflow, _ = await _ready(shop)
    shop.times = [("20:00", "08:00 PM")]

    effects = await workflow.direct("yes")

    assert shop.book_calls == 0
    assert "dining_time_unavailable" in _says(effects)


async def test_switched_off_booking_records_nothing_uncertain() -> None:
    shop = MockDineout()
    journal = InMemoryOrderJournal()
    workflow, _ = await _ready(shop, journal)
    shop.fail_next_book = CommerceNotAttempted("off")

    effects = await workflow.direct("yes")

    assert _says(effects) == ["checkout_disabled"]
    assert unresolved_attempts(journal) == []
    assert workflow.session.state is DiningState.READBACK
    assert not can_confirm(workflow.session), "a fresh readback is needed"


async def test_order_limit_stops_a_booking() -> None:
    journal = InMemoryOrderJournal()
    policy = CheckoutPolicy(max_orders=1)
    first, _ = await _ready(MockDineout(), journal, policy)
    await first.direct("yes")
    shop = MockDineout()
    second, _ = await _ready(shop, journal, policy)

    effects = await second.direct("yes")

    assert _says(effects) == ["order_limit"]
    assert shop.book_calls == 0


async def test_unverifiable_booking_says_so() -> None:
    shop = MockDineout()
    workflow, _ = await _ready(shop)
    shop.fail_status = True

    effects = await workflow.direct("yes")

    assert _says(effects) == ["dining_booking_unverified"]
    assert workflow.session.verified is False


async def test_cancelled_status_is_reported() -> None:
    class Cancelled(MockDineout):
        async def status(self, order_id: str) -> Any:
            from app.dining.base import BookingStatus

            return BookingStatus("CANCELLED")

    workflow, _ = await _ready(Cancelled())

    effects = await workflow.direct("yes")

    assert _says(effects) == ["dining_booking_problem"]


async def test_cancel_before_booking_drops_it() -> None:
    workflow, shop = await _ready()

    effects = await workflow.cancel()

    assert _says(effects) == ["dining_cancelled"]
    assert shop.book_calls == 0


async def test_cancel_after_booking_points_elsewhere() -> None:
    workflow, _ = await _ready()
    await workflow.direct("yes")

    assert _says(await workflow.cancel()) == ["dining_cancel_elsewhere"]


def test_known_areas_come_from_the_documented_coordinates() -> None:
    assert KNOWN_AREAS["bengaluru"] == (12.9716, 77.5946)
    assert KNOWN_AREAS["koramangala"] == (12.9352, 77.6245)
    assert KNOWN_AREAS["indiranagar"] == (12.9784, 77.6408)
