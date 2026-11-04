"""Table booking dialog — PURE, with the same approval rules as the order state machine.

COLLECTING -> CHOOSING_RESTAURANT -> CHOOSING_SLOT -> READBACK -> BOOKING -> VERIFYING -> DONE
BOOKING -> UNKNOWN (terminal; Dineout has no history to check, so it is settled by a person)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256

from app.dining.base import DiningRestaurant, TableSlot
from app.orchestrator.state_machine import CartEntry, Say

MAX_OPTIONS = 3


class DiningState(StrEnum):
    COLLECTING = "COLLECTING"
    CHOOSING_RESTAURANT = "CHOOSING_RESTAURANT"
    CHOOSING_SLOT = "CHOOSING_SLOT"
    READBACK = "READBACK"
    BOOKING = "BOOKING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    UNKNOWN = "ORDER_UNKNOWN"


@dataclass(frozen=True)
class DiningSession:
    state: DiningState = DiningState.COLLECTING
    query: str = ""
    area: str = ""
    date: str = ""
    time: str = ""
    guests: int = 0
    latitude: float = 0.0
    longitude: float = 0.0
    restaurants: tuple[DiningRestaurant, ...] = ()
    restaurant: DiningRestaurant | None = None
    slots: tuple[TableSlot, ...] = ()
    slot: TableSlot | None = None
    version: int = 0
    readback_version: int | None = None
    readback_hash: str = ""
    reasks: int = 0
    order_id: str = ""
    order_status: str = ""
    verified: bool = False


# ------------------------------------------------------------------ events
@dataclass(frozen=True)
class DetailsGiven:
    query: str | None = None
    area: str | None = None
    date: str | None = None
    time: str | None = None
    guests: int | None = None


@dataclass(frozen=True)
class AreaResolved:
    latitude: float | None  # None: we don't know this place
    longitude: float = 0.0


@dataclass(frozen=True)
class RestaurantsFound:
    restaurants: tuple[DiningRestaurant, ...]


@dataclass(frozen=True)
class SlotsFound:
    slots: tuple[TableSlot, ...]


@dataclass(frozen=True)
class OptionChosen:
    index: int


@dataclass(frozen=True)
class Unclear:
    pass


@dataclass(frozen=True)
class Confirmed:
    version: int
    digest: str


@dataclass(frozen=True)
class Rejected:
    pass


@dataclass(frozen=True)
class Cancelled:
    pass


@dataclass(frozen=True)
class BookingPlaced:
    order_id: str
    status: str = ""


@dataclass(frozen=True)
class BookingRefused:
    pass


@dataclass(frozen=True)
class BookingBlocked:
    reason: str  # order_not_attempted | order_limit | checkout_disabled


@dataclass(frozen=True)
class BookingUncertain:
    pass


@dataclass(frozen=True)
class BookingVerified:
    verified: bool
    status: str = ""
    problem: bool = False


DiningEvent = (
    DetailsGiven
    | AreaResolved
    | RestaurantsFound
    | SlotsFound
    | OptionChosen
    | Unclear
    | Confirmed
    | Rejected
    | Cancelled
    | BookingPlaced
    | BookingRefused
    | BookingBlocked
    | BookingUncertain
    | BookingVerified
)


# ----------------------------------------------------------------- effects
@dataclass(frozen=True)
class ResolveArea:
    area: str


@dataclass(frozen=True)
class SearchRestaurants:
    query: str


@dataclass(frozen=True)
class FetchSlots:
    restaurant_id: str
    date: str


@dataclass(frozen=True)
class BookTable:
    """The ONLY way a table is booked. Runtime journals the attempt first."""

    version: int
    digest: str


@dataclass(frozen=True)
class VerifyBooking:
    order_id: str


DiningEffect = Say | ResolveArea | SearchRestaurants | FetchSlots | BookTable | VerifyBooking


def booking_hash(session: DiningSession) -> str:
    slot = session.slot
    restaurant = session.restaurant
    identity = "|".join(
        [
            restaurant.id if restaurant else "",
            slot.item_id if slot else "",
            str(slot.reservation_time) if slot else "",
            str(session.guests),
        ]
    )
    return sha256(identity.encode()).hexdigest()


def can_confirm(session: DiningSession) -> bool:
    return (
        session.state is DiningState.READBACK
        and session.restaurant is not None
        and session.slot is not None
        and session.readback_version == session.version
        and session.readback_hash == booking_hash(session)
    )


def _unbound(session: DiningSession) -> DiningSession:
    return replace(session, readback_version=None, readback_hash="", version=session.version + 1)


def _readback(session: DiningSession) -> tuple[DiningSession, list[DiningEffect]]:
    restaurant = session.restaurant
    slot = session.slot
    if restaurant is None or slot is None:
        return session, []
    bound = replace(
        session,
        state=DiningState.READBACK,
        readback_version=session.version,
        readback_hash=booking_hash(session),
        reasks=0,
    )
    return bound, [
        Say(
            "dining_readback",
            data={
                "restaurant": restaurant.name,
                "area": restaurant.area,
                "date": slot.date,
                "time": slot.display_time,
                "guests": str(session.guests),
            },
        )
    ]


def _ask_next(session: DiningSession) -> tuple[DiningSession, list[DiningEffect]]:
    """One question at a time, then search once everything needed is known."""
    collecting = replace(session, state=DiningState.COLLECTING)
    if not session.query:
        return collecting, [Say("dining_ask_query")]
    if not session.area:
        return collecting, [Say("dining_ask_area")]
    if not session.date:
        return collecting, [Say("dining_ask_date")]
    if not session.time:
        return collecting, [Say("dining_ask_time")]
    if not session.guests:
        return collecting, [Say("dining_ask_guests")]
    return collecting, [Say("filler_search"), ResolveArea(session.area)]


def restaurant_line(restaurant: DiningRestaurant) -> CartEntry:
    return CartEntry(restaurant.id, restaurant.id, restaurant.name, restaurant.area, 1, 0)


def slot_line(slot: TableSlot) -> CartEntry:
    return CartEntry(slot.item_id, slot.item_id, slot.display_time, slot.date, 1, 0)


def _options_say(kind: str, choices: tuple[DiningRestaurant, ...] | tuple[TableSlot, ...]) -> Say:
    lines = tuple(
        restaurant_line(choice) if isinstance(choice, DiningRestaurant) else slot_line(choice)
        for choice in choices
    )
    return Say("dining_options", data={"kind": kind}, lines=lines)


def _reask(session: DiningSession) -> tuple[DiningSession, list[DiningEffect]]:
    if session.reasks >= 2:
        return _reset(session), [Say("item_skipped"), Say("dining_ask_query")]
    says = (
        _options_say("restaurant", session.restaurants)
        if session.state is DiningState.CHOOSING_RESTAURANT
        else _options_say("slot", session.slots)
    )
    return replace(session, reasks=session.reasks + 1), [says]


def _reset(session: DiningSession) -> DiningSession:
    return replace(
        _unbound(session),
        state=DiningState.COLLECTING,
        query="",
        area="",
        date="",
        time="",
        guests=0,
        restaurants=(),
        restaurant=None,
        slots=(),
        slot=None,
        reasks=0,
    )


def _details(
    session: DiningSession, event: DetailsGiven
) -> tuple[DiningSession, list[DiningEffect]]:
    changed = replace(
        _unbound(session),
        query=event.query or session.query,
        area=event.area or session.area,
        date=event.date or session.date,
        time=event.time or session.time,
        guests=event.guests or session.guests,
        restaurants=(),
        restaurant=None,
        slots=(),
        slot=None,
        reasks=0,
    )
    return _ask_next(changed)


def _closest(slots: tuple[TableSlot, ...], wanted: str) -> tuple[TableSlot, ...]:
    def distance(slot: TableSlot) -> int:
        hours, minutes = slot.time.split(":")
        want_hours, want_minutes = wanted.split(":")
        return abs(int(hours) * 60 + int(minutes) - int(want_hours) * 60 - int(want_minutes))

    return tuple(sorted(slots, key=distance)[:MAX_OPTIONS])


def transition(
    session: DiningSession, event: DiningEvent
) -> tuple[DiningSession, list[DiningEffect]]:
    state = session.state
    if state is DiningState.UNKNOWN:
        return session, [Say("order_unknown")]
    if state is DiningState.BOOKING:
        return _on_booking(session, event)
    if state is DiningState.VERIFYING:
        return _on_verifying(session, event)
    if state is DiningState.DONE:
        if isinstance(event, Cancelled):
            return session, [Say("dining_cancel_elsewhere")]
        return session, [Say("dining_already_booked")]
    if isinstance(event, Cancelled):
        return _reset(session), [Say("dining_cancelled")]
    if isinstance(event, DetailsGiven):
        return _details(session, event)
    if isinstance(event, AreaResolved):
        if event.latitude is None:
            return replace(session, area=""), [Say("dining_area_unknown")]
        located = replace(session, latitude=event.latitude, longitude=event.longitude)
        return located, [SearchRestaurants(session.query)]
    if isinstance(event, RestaurantsFound):
        if not event.restaurants:
            return replace(session, query=""), [Say("dining_none_found")]
        found = event.restaurants[:MAX_OPTIONS]
        return (
            replace(session, state=DiningState.CHOOSING_RESTAURANT, restaurants=found, reasks=0),
            [_options_say("restaurant", found)],
        )
    if state is DiningState.CHOOSING_RESTAURANT:
        return _choosing_restaurant(session, event)
    if state is DiningState.CHOOSING_SLOT:
        return _choosing_slot(session, event)
    if state is DiningState.READBACK:
        return _on_readback(session, event)
    if isinstance(event, Rejected):
        return session, [Say("ask_domain")]
    if isinstance(event, Unclear):
        return _ask_next(session)
    return session, []


def _choosing_restaurant(
    session: DiningSession, event: DiningEvent
) -> tuple[DiningSession, list[DiningEffect]]:
    if isinstance(event, OptionChosen):
        if not 0 <= event.index < len(session.restaurants):
            return _reask(session)
        chosen = session.restaurants[event.index]
        return (
            replace(session, restaurant=chosen, reasks=0),
            [Say("filler_search"), FetchSlots(chosen.id, session.date)],
        )
    if isinstance(event, SlotsFound):
        if not event.slots:
            return (
                replace(session, restaurant=None, reasks=0),
                [Say("dining_no_free_slots"), _options_say("restaurant", session.restaurants)],
            )
        exact = [slot for slot in event.slots if slot.time == session.time]
        if exact:
            return _readback(_unbound(replace(session, slot=exact[0], slots=())))
        nearby = _closest(event.slots, session.time)
        return (
            replace(session, state=DiningState.CHOOSING_SLOT, slots=nearby, reasks=0),
            [Say("dining_time_unavailable"), _options_say("slot", nearby)],
        )
    if isinstance(event, Rejected):
        return _reset(session), [Say("dining_ask_query")]
    if isinstance(event, Unclear):
        return _reask(session)
    return session, []


def _choosing_slot(
    session: DiningSession, event: DiningEvent
) -> tuple[DiningSession, list[DiningEffect]]:
    if isinstance(event, OptionChosen):
        if not 0 <= event.index < len(session.slots):
            return _reask(session)
        return _readback(_unbound(replace(session, slot=session.slots[event.index], slots=())))
    if isinstance(event, Rejected):
        return _reset(session), [Say("dining_ask_query")]
    if isinstance(event, Unclear):
        return _reask(session)
    return session, []


def _on_readback(
    session: DiningSession, event: DiningEvent
) -> tuple[DiningSession, list[DiningEffect]]:
    if isinstance(event, Confirmed):
        if (
            not can_confirm(session)
            or event.version != session.version
            or event.digest != session.readback_hash
        ):
            return _readback(session)
        return replace(session, state=DiningState.BOOKING), [
            BookTable(session.version, session.readback_hash)
        ]
    if isinstance(event, Rejected):
        return _reset(session), [Say("what_to_change")]
    if isinstance(event, Unclear):
        if session.reasks == 0:
            return replace(session, reasks=1), [Say("confirm_reask")]
        return _readback(session)
    return session, []


def _on_booking(
    session: DiningSession, event: DiningEvent
) -> tuple[DiningSession, list[DiningEffect]]:
    if isinstance(event, BookingPlaced):
        return (
            replace(session, state=DiningState.VERIFYING, order_id=event.order_id),
            [VerifyBooking(event.order_id)],
        )
    if isinstance(event, BookingUncertain):
        return replace(_unbound(session), state=DiningState.UNKNOWN), [Say("order_unknown")]
    if isinstance(event, BookingRefused):
        # Refused means no booking was made, so offer the times again from fresh data.
        restaurant = session.restaurant
        unbound = replace(_unbound(session), state=DiningState.CHOOSING_RESTAURANT, slot=None)
        effects: list[DiningEffect] = [Say("dining_booking_refused")]
        if restaurant is not None:
            effects.append(FetchSlots(restaurant.id, session.date))
        return unbound, effects
    if isinstance(event, BookingBlocked):
        return replace(_unbound(session), state=DiningState.READBACK), [Say(event.reason)]
    if isinstance(event, SlotsFound):  # the slot vanished between readback and booking
        return _choosing_restaurant(
            replace(_unbound(session), state=DiningState.CHOOSING_RESTAURANT, slot=None), event
        )
    return session, [Say("order_in_progress")]


def _on_verifying(
    session: DiningSession, event: DiningEvent
) -> tuple[DiningSession, list[DiningEffect]]:
    if not isinstance(event, BookingVerified):
        return session, [Say("order_in_progress")]
    done = replace(
        session,
        state=DiningState.DONE,
        verified=event.verified and not event.problem,
        order_status=event.status,
    )
    if event.problem:
        return done, [Say("dining_booking_problem")]
    if not event.verified:
        return done, [Say("dining_booking_unverified")]
    return done, [Say("dining_booked")]
