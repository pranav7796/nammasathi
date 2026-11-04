"""The one code path allowed to call adapter.checkout. Runtime only; no dialog decisions.

Order of operations and why:
1. Record existing order IDs as a baseline. Without it no later check can tell a
   new order from an old one, so a failed read aborts before anything is sent.
2. Journal the attempt durably, atomically with the guards. A crash after this
   point is recovered from the journal on next start.
3. Call checkout exactly once.
4. On anything but a clear result, poll order history for an order not in the
   baseline. This is immune to clock skew, name differences, and multi-store splits.
5. Never retry. An outcome that cannot be proven stays UNKNOWN and blocks checkout
   until a person settles it (`python -m app.order_admin`).

Contracts: https://mcp.swiggy.com/builders/docs/build/ship-to-production.md
(check-then-retry, no idempotency) and .../reference/instamart/checkout.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from app.commerce.base import (
    CommerceAdapter,
    CommerceAuthRequired,
    CommerceNotAttempted,
    CommerceRejected,
    OrderSummary,
    PaymentMethod,
)
from app.commerce.order_journal import (
    UNRESOLVED,
    AttemptStatus,
    JournalError,
    OrderAttempt,
    OrderJournal,
    new_attempt,
    resolve_attempt,
    unresolved_attempts,
)
from app.observability import log_event
from app.orchestrator.state_machine import (
    CheckoutAborted,
    CheckoutFailed,
    CheckoutSucceeded,
    OrderVerified,
    ReconcileResult,
    Session,
    format_money,
)

ORDER_HISTORY_COUNT = 20
_PROBLEM_STATUS_WORDS = ("cancel", "fail", "reject", "refund")
_TOTAL_TOLERANCE_INR = Decimal(1)  # the bill rounds to whole rupees, order totals may not


@dataclass(frozen=True)
class CheckoutPolicy:
    max_total_inr: Decimal | None = None
    max_orders: int | None = None  # orders placed or unresolved, ever, per journal
    reconcile_delays: tuple[float, ...] = (0.0,)
    rejection_delays: tuple[float, ...] = (0.0,)
    verify_delays: tuple[float, ...] = (0.0,)


# The docs ask for a 2-5 s wait before checking and a 30 s user-facing budget.
REAL_RECONCILE_DELAYS = (3.0, 7.0, 10.0, 10.0)
REAL_REJECTION_DELAYS = (2.0,)
REAL_VERIFY_DELAYS = (0.0, 3.0, 7.0)


class _Guarded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CheckoutCoordinator:
    def __init__(
        self,
        adapter: CommerceAdapter,
        journal: OrderJournal,
        policy: CheckoutPolicy | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.adapter = adapter
        self.journal = journal
        self.policy = policy or CheckoutPolicy()
        self._sleep = sleep
        self._attempt: OrderAttempt | None = None

    def _guard(self, session_total: Decimal) -> Callable[[list[OrderAttempt]], None]:
        def check(existing: list[OrderAttempt]) -> None:
            if any(attempt.status in UNRESOLVED for attempt in existing):
                raise _Guarded("unresolved_attempt")
            limit = self.policy.max_orders
            used = sum(attempt.status is AttemptStatus.PLACED for attempt in existing)
            if limit is not None and used >= limit:
                raise _Guarded("order_limit")
            maximum = self.policy.max_total_inr
            if maximum is not None and session_total > maximum:
                raise _Guarded("order_limit")

        return check

    async def _order_ids(self) -> tuple[str, ...]:
        orders = await self.adapter.get_orders(count=ORDER_HISTORY_COUNT)
        return tuple(order.order_id for order in orders)

    async def place(
        self, session: Session, address_id: str
    ) -> CheckoutSucceeded | CheckoutFailed | CheckoutAborted:
        """Journal the attempt, then call checkout exactly once."""
        try:
            baseline = await self._order_ids()
        except Exception:
            log_event("checkout_aborted", kind="baseline_read_failed")
            return CheckoutAborted("preflight")
        attempt = new_attempt(
            session.cart_hash,
            format_money(session.total_inr),
            baseline,
            tuple(line.name for line in session.cart),
        )
        try:
            self.journal.begin(attempt, self._guard(Decimal(session.total_inr)))
        except _Guarded as guard:
            log_event("checkout_aborted", kind=guard.reason)
            return CheckoutAborted(
                "unresolved_attempt" if guard.reason == "unresolved_attempt" else "order_limit"
            )
        except JournalError:
            log_event("checkout_aborted", kind="journal_unavailable")
            return CheckoutAborted("preflight")
        self._attempt = attempt
        log_event("checkout_attempt", attempt_id=attempt.attempt_id)
        try:
            result = await self.adapter.checkout(address_id, payment_method=PaymentMethod.COD)
        except CommerceNotAttempted:
            self._settle(AttemptStatus.NOT_PLACED, note="refused locally before sending")
            return CheckoutAborted("disabled")
        except CommerceAuthRequired:
            self._settle(AttemptStatus.NOT_PLACED, note="login was rejected")
            return CheckoutAborted("preflight")
        except CommerceRejected as exc:
            log_event("checkout_rejected", kind=exc.kind.value)
            return CheckoutFailed(exc.kind.value, terminal=exc.kind.is_definitive)
        except Exception as exc:
            # Timeouts, lost connections, 5xx, unreadable replies: the order may exist.
            log_event("checkout_ambiguous", kind=type(exc).__name__)
            return CheckoutFailed("unknown")
        if result.order_ids and (result.success or result.partial):
            self._settle(AttemptStatus.PLACED, order_ids=tuple(result.order_ids))
            return CheckoutSucceeded(tuple(result.order_ids), partial=result.partial)
        log_event("checkout_unclear_result", status=result.status)
        return CheckoutFailed("duplicate" if result.probable_duplicate else "unknown")

    def _settle(
        self,
        status: AttemptStatus,
        *,
        order_ids: tuple[str, ...] = (),
        order_status: str = "",
        note: str = "",
    ) -> None:
        if self._attempt is None:
            return
        try:
            self._attempt = resolve_attempt(
                self.journal,
                self._attempt,
                status,
                order_ids=order_ids,
                order_status=order_status,
                note=note,
            )
        except JournalError:
            # The attempt stays PENDING on disk, which safely blocks further checkouts.
            log_event("journal_update_failed", attempt_id=self._attempt.attempt_id)

    def _new_orders(self, attempt: OrderAttempt, orders: list[OrderSummary]) -> list[OrderSummary]:
        baseline = set(attempt.baseline_order_ids)
        fresh = [order for order in orders if order.order_id not in baseline]
        names = {name.casefold() for name in attempt.item_names}
        total = Decimal(attempt.total_inr)
        # An unrelated order placed in the Swiggy app at the same moment must not count as ours.
        return [
            order
            for order in fresh
            if any(item.name.casefold() in names for item in order.items)
            or abs(order.total_inr - total) <= _TOTAL_TOLERANCE_INR
        ]

    async def reconcile(self, session: Session) -> ReconcileResult:
        """Look for a new order. Absence is only proven by a successful read of a clean history."""
        attempt = self._attempt
        if attempt is None:
            return ReconcileResult(order_found=False, cart_hash=session.cart_hash)
        delays = (
            self.policy.rejection_delays
            if session.failure_terminal
            else self.policy.reconcile_delays
        )
        found, absent = await self._poll_for_new_orders(attempt, delays)
        if found:
            ids = tuple(order.order_id for order in found)
            self._settle(AttemptStatus.PLACED, order_ids=ids, note="found by reconciliation")
            log_event("reconcile", outcome="found")
            return ReconcileResult(order_found=True, order_ids=ids, cart_hash=session.cart_hash)
        if absent and session.failure_terminal:
            self._settle(AttemptStatus.NOT_PLACED, note="refused; no new order in history")
            log_event("reconcile", outcome="refused_absent")
            return ReconcileResult(
                order_found=False, cart_hash=session.cart_hash, confirmed_absent=True
            )
        self._settle(AttemptStatus.UNKNOWN, note="outcome could not be proven")
        log_event("reconcile", outcome="unknown")
        return ReconcileResult(order_found=False, cart_hash=session.cart_hash)

    async def _poll_for_new_orders(
        self, attempt: OrderAttempt, delays: tuple[float, ...]
    ) -> tuple[list[OrderSummary], bool]:
        absent = False
        for delay in delays:
            if delay:
                await self._sleep(delay)
            try:
                orders = await self.adapter.get_orders(count=ORDER_HISTORY_COUNT)
            except Exception:
                absent = False
                continue
            found = self._new_orders(attempt, orders)
            if found:
                return found, False
            baseline = set(attempt.baseline_order_ids)
            absent = all(order.order_id in baseline for order in orders)
        return [], absent

    async def verify(
        self, session: Session, *, lat: float | None, lng: float | None
    ) -> OrderVerified:
        """Check the placed order in order history, independent of the checkout response."""
        expected = set(session.order_ids)
        total = Decimal(session.total_inr)
        for delay in self.policy.verify_delays:
            if delay:
                await self._sleep(delay)
            try:
                orders = await self.adapter.get_orders(count=ORDER_HISTORY_COUNT)
            except Exception:
                continue
            matched = [order for order in orders if order.order_id in expected]
            if not matched:
                continue
            status = matched[0].status
            problem = any(
                word in order.status.casefold()
                for order in matched
                for word in _PROBLEM_STATUS_WORDS
            )
            if not session.order_partial and len(matched) == len(expected):
                placed_total = sum((order.total_inr for order in matched), Decimal(0))
                problem = problem or abs(placed_total - total) > _TOTAL_TOLERANCE_INR
            self._settle(
                AttemptStatus.PLACED, order_status=status, note="verified in order history"
            )
            log_event("order_verified", verified=True, status=status, outcome=str(problem))
            return OrderVerified(verified=True, status=status, problem=problem)
        if lat is not None and lng is not None and session.order_ids:
            try:
                tracking = await self.adapter.track_order(session.order_ids[0], lat, lng)
            except Exception:
                tracking = None
            if tracking is not None:
                self._settle(AttemptStatus.PLACED, note="verified by tracking")
                log_event("order_verified", verified=True, outcome="tracking")
                return OrderVerified(verified=True, status=tracking.status_message)
        log_event("order_verified", verified=False)
        return OrderVerified(verified=False)

    async def recover_unresolved(self) -> list[OrderAttempt]:
        """Try to settle attempts left by a crash or restart. Returns what is still unresolved."""
        for attempt in unresolved_attempts(self.journal):
            try:
                orders = await self.adapter.get_orders(count=ORDER_HISTORY_COUNT)
            except Exception:
                continue
            found = self._new_orders(attempt, orders)
            if found:
                resolve_attempt(
                    self.journal,
                    attempt,
                    AttemptStatus.PLACED,
                    order_ids=tuple(order.order_id for order in found),
                    order_status=found[0].status,
                    note="found during recovery",
                )
                log_event("attempt_recovered", attempt_id=attempt.attempt_id, outcome="placed")
        return unresolved_attempts(self.journal)
