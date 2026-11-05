"""Runs the table-booking dialog: executes effects and journals every booking attempt."""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from re import fullmatch

from app.assistant.view import WorkflowView
from app.commerce.base import (
    CommerceAuthRequired,
    CommerceError,
    CommerceNotAttempted,
    CommerceRejected,
)
from app.commerce.order_journal import (
    AttemptStatus,
    JournalError,
    OrderAttempt,
    OrderJournal,
    new_attempt,
    resolve_attempt,
)
from app.dining.base import DiningAdapter
from app.dining.state_machine import (
    AreaResolved,
    BookingBlocked,
    BookingPlaced,
    BookingRefused,
    BookingUncertain,
    BookingVerified,
    BookTable,
    Cancelled,
    Confirmed,
    DetailsGiven,
    DiningEffect,
    DiningEvent,
    DiningSession,
    DiningState,
    FetchSlots,
    OptionChosen,
    Rejected,
    ResolveArea,
    RestaurantsFound,
    Resumed,
    SearchRestaurants,
    SlotsFound,
    Unclear,
    VerifyBooking,
    booking_hash,
    can_confirm,
    restaurant_line,
    slot_line,
    transition,
)
from app.llm.extractor import ClarificationContext, GroceryIntent
from app.observability import log_event
from app.orchestrator.checkout import AttemptRefused, CheckoutPolicy, attempt_guard
from app.orchestrator.confirmation import explicit_confirmation, option_choice
from app.orchestrator.state_machine import CartEntry, Effect, Say

# Coordinates from https://mcp.swiggy.com/builders/docs/reference/dineout/search_restaurants_dineout.md.
# Search must use the place the user named, so anywhere else makes Saathi ask, not guess.
KNOWN_AREAS: dict[str, tuple[float, float]] = {
    "bangalore": (12.9716, 77.5946),
    "bengaluru": (12.9716, 77.5946),
    "ಬೆಂಗಳೂರು": (12.9716, 77.5946),
    "बेंगलुरु": (12.9716, 77.5946),
    "koramangala": (12.9352, 77.6245),
    "ಕೋರಮಂಗಲ": (12.9352, 77.6245),
    "indiranagar": (12.9784, 77.6408),
    "ಇಂದಿರಾನಗರ": (12.9784, 77.6408),
    "mumbai": (19.0760, 72.8777),
    "मुंबई": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "दिल्ली": (28.6139, 77.2090),
}
_PROBLEM_WORDS = ("cancel", "fail", "reject")


class DiningWorkflow:
    domain = "dining"

    def __init__(
        self,
        adapter: DiningAdapter,
        journal: OrderJournal,
        policy: CheckoutPolicy | None = None,
        *,
        today: Date | None = None,
    ) -> None:
        self.adapter = adapter
        self.journal = journal
        self.policy = policy or CheckoutPolicy()
        self.today = today
        self.session = DiningSession()
        self._attempt: OrderAttempt | None = None

    @property
    def finished(self) -> bool:
        return self.session.state is DiningState.DONE

    @property
    def outcome_unknown(self) -> bool:
        return self.session.state is DiningState.UNKNOWN

    @property
    def has_open_task(self) -> bool:
        return self.session.state is not DiningState.DONE and bool(
            self.session.query or self.session.restaurant
        )

    async def open(self) -> list[Effect]:
        return []

    def context(self) -> ClarificationContext | None:
        session = self.session
        if session.state is DiningState.CHOOSING_RESTAURANT:
            return ClarificationContext(
                item="restaurant", options=tuple(item.name for item in session.restaurants)
            )
        if session.state is DiningState.CHOOSING_SLOT:
            return ClarificationContext(
                item="time", options=tuple(item.display_time for item in session.slots)
            )
        return None

    async def _dispatch(self, event: DiningEvent) -> list[Effect]:
        spoken: list[Effect] = []
        pending: list[DiningEvent] = [event]
        while pending:
            self.session, effects = transition(self.session, pending.pop(0))
            for effect in effects:
                if isinstance(effect, Say):
                    spoken.append(effect)
                else:
                    follow_up = await self._execute(effect, spoken)
                    if follow_up is not None:
                        pending.append(follow_up)
        return spoken

    async def _execute(self, effect: DiningEffect, spoken: list[Effect]) -> DiningEvent | None:
        session = self.session
        try:
            if isinstance(effect, ResolveArea):
                location = KNOWN_AREAS.get(effect.area.casefold().strip())
                return AreaResolved(*location) if location else AreaResolved(None)
            if isinstance(effect, SearchRestaurants):
                found = await self.adapter.search(effect.query, session.latitude, session.longitude)
                return RestaurantsFound(tuple(found))
            if isinstance(effect, FetchSlots):
                slots = await self.adapter.free_slots(
                    effect.restaurant_id, effect.date, session.latitude, session.longitude
                )
                return SlotsFound(tuple(slots))
        except (CommerceError, TimeoutError):
            spoken.append(Say("commerce_error"))
            return None
        if isinstance(effect, BookTable):
            return await self._book()
        if isinstance(effect, VerifyBooking):
            return await self._verify(effect.order_id)
        return None

    async def _book(self) -> DiningEvent:
        """Refresh the slot, journal the attempt, then call book_table exactly once."""
        session = self.session
        restaurant, slot = session.restaurant, session.slot
        if restaurant is None or slot is None:
            return BookingBlocked("order_not_attempted")
        try:
            fresh = await self.adapter.free_slots(
                restaurant.id, slot.date, session.latitude, session.longitude
            )
        except (CommerceError, TimeoutError):
            return BookingBlocked("order_not_attempted")
        if slot not in fresh:
            return SlotsFound(tuple(fresh))
        attempt = new_attempt(booking_hash(session), "0", (), (restaurant.name,), domain="dining")
        try:
            self.journal.begin(attempt, attempt_guard(self.policy, Decimal(0)))
        except AttemptRefused as refused:
            if refused.reason == "unresolved_attempt":
                return BookingUncertain()
            return BookingBlocked("order_limit")
        except JournalError:
            return BookingBlocked("order_not_attempted")
        self._attempt = attempt
        log_event("booking_attempt", attempt_id=attempt.attempt_id)
        try:
            result = await self.adapter.book(
                restaurant.id, slot, session.guests, session.latitude, session.longitude
            )
        except CommerceNotAttempted:
            self._settle(AttemptStatus.NOT_PLACED, note="refused locally before sending")
            return BookingBlocked("checkout_disabled")
        except CommerceAuthRequired:
            self._settle(AttemptStatus.NOT_PLACED, note="login was rejected")
            return BookingBlocked("order_not_attempted")
        except CommerceRejected as exc:
            if exc.kind.is_definitive:
                self._settle(
                    AttemptStatus.NOT_PLACED, note=f"refused: {exc.provider_message[:200]}"
                )
                return BookingRefused()
            self._settle(AttemptStatus.UNKNOWN, note=f"unclear: {exc.provider_message[:200]}")
            return BookingUncertain()
        except Exception as exc:
            # Dineout has no history to check, so a lost reply stays unknown until a person looks.
            log_event("booking_ambiguous", kind=type(exc).__name__)
            self._settle(AttemptStatus.UNKNOWN, note="outcome could not be proven")
            return BookingUncertain()
        self._settle(AttemptStatus.PLACED, order_ids=(result.order_id,))
        return BookingPlaced(result.order_id, result.status)

    def _settle(
        self, status: AttemptStatus, *, order_ids: tuple[str, ...] = (), note: str = ""
    ) -> None:
        if self._attempt is None:
            return
        try:
            self._attempt = resolve_attempt(
                self.journal, self._attempt, status, order_ids=order_ids, note=note
            )
        except JournalError:
            log_event("journal_update_failed", attempt_id=self._attempt.attempt_id)

    async def _verify(self, order_id: str) -> BookingVerified:
        try:
            status = (await self.adapter.status(order_id)).status
        except Exception:
            return BookingVerified(verified=False)
        problem = any(word in status.casefold() for word in _PROBLEM_WORDS)
        return BookingVerified(verified=True, status=status, problem=problem)

    async def direct(self, text: str, heard: tuple[int, str] | None = None) -> list[Effect] | None:
        session = self.session
        if session.state is DiningState.READBACK:
            answer = explicit_confirmation(text)
            if answer is True:
                version, digest = heard or (
                    session.readback_version if session.readback_version is not None else -1,
                    session.readback_hash,
                )
                return await self._dispatch(Confirmed(version, digest))
            if answer is False:
                return await self._dispatch(Rejected())
        if session.state in (DiningState.CHOOSING_RESTAURANT, DiningState.CHOOSING_SLOT):
            count = len(
                session.restaurants
                if session.state is DiningState.CHOOSING_RESTAURANT
                else session.slots
            )
            choice = option_choice(text, count)
            if choice is not None:
                return await self._dispatch(OptionChosen(choice))
            if explicit_confirmation(text) is False:
                return await self._dispatch(Rejected())
        if session.state in (DiningState.BOOKING, DiningState.VERIFYING, DiningState.UNKNOWN):
            return await self._dispatch(Unclear())
        return None

    async def apply(self, text: str, intent: GroceryIntent) -> list[Effect]:
        del text
        return await self._dispatch(self._event(intent))

    def _event(self, intent: GroceryIntent) -> DiningEvent:
        if intent.intent == "select_option" and intent.option_number is not None:
            return OptionChosen(intent.option_number - 1)
        if intent.intent == "cancel":
            return Cancelled()
        if intent.intent == "reject":
            return Rejected()
        query = intent.query
        if query is None and intent.items:
            query = intent.items[0].search_term or intent.items[0].name
        details = DetailsGiven(
            query=query,
            area=intent.area,
            date=self._valid_date(intent.date),
            time=intent.time if intent.time and fullmatch(r"\d{2}:\d{2}", intent.time) else None,
            guests=intent.guests,
        )
        if any((details.query, details.area, details.date, details.time, details.guests)):
            return details
        return Unclear()

    def _valid_date(self, value: str | None) -> str | None:
        """Only a real date today or later; anything else is asked again."""
        if not value:
            return None
        try:
            parsed = Date.fromisoformat(value)
        except ValueError:
            return None
        if self.today is not None and parsed < self.today:
            return None
        return value

    async def confirm(self, version: int, digest: str) -> list[Effect]:
        return await self._dispatch(Confirmed(version, digest))

    async def choose(self, index: int) -> list[Effect]:
        return await self._dispatch(OptionChosen(index))

    async def cancel(self) -> list[Effect]:
        return await self._dispatch(Cancelled())

    async def resume(self) -> list[Effect]:
        return await self._dispatch(Resumed())

    def view(self) -> WorkflowView:
        session = self.session
        restaurant, slot = session.restaurant, session.slot
        lines: tuple[CartEntry, ...] = ()
        if restaurant is not None and slot is not None:
            label = f"{slot.date} at {slot.display_time}, table for {session.guests}"
            lines = (CartEntry(restaurant.id, slot.item_id, restaurant.name, label, 1, 0),)
        options: tuple[CartEntry, ...] = ()
        if session.state is DiningState.CHOOSING_RESTAURANT:
            options = tuple(restaurant_line(item) for item in session.restaurants)
        elif session.state is DiningState.CHOOSING_SLOT:
            options = tuple(slot_line(item) for item in session.slots)
        confirmable = can_confirm(session)
        return WorkflowView(
            domain=self.domain,
            state=session.state.value,
            lines=lines,
            cart_version=session.version,
            cart_hash=session.readback_hash,
            readback_version=session.readback_version,
            readback_hash=session.readback_hash,
            confirmable=confirmable,
            payment_method="NONE",
            address_label=restaurant.name if restaurant else "",
            delivery_address=restaurant.area if restaurant else "",
            options=options,
            order_ids=(session.order_id,) if session.order_id else (),
            order_status=session.order_status,
            order_verified=session.verified,
        )
