"""Dialog state machine — PURE (no I/O, no clock, no randomness). DESIGN.md §5.

The runtime feeds Events in and executes the returned Effects (speak, call the
commerce adapter, place the order). Because the function is pure, every rule
below is unit- and property-testable.

States:
    GREETING -> COLLECTING -> (CLARIFYING <-> COLLECTING) -> READBACK
    READBACK -> COLLECTING (edit) | CONFIRMED (explicit yes to the cart just read back)
    CONFIRMED -> PLACING -> VERIFYING -> DONE
    PLACING -> FAILED (checkout did not report success; orders are reconciled)
    FAILED -> VERIFYING (a new order exists)
    FAILED -> READBACK (the provider refused AND a read proved no new order exists)
    FAILED -> ORDER_UNKNOWN (anything else; terminal, checkout is never offered again)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal

Money = int | Decimal


class State(StrEnum):
    GREETING = "GREETING"
    COLLECTING = "COLLECTING"
    CLARIFYING = "CLARIFYING"
    READBACK = "READBACK"
    CONFIRMED = "CONFIRMED"
    PLACING = "PLACING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    FAILED = "FAILED"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"


Intent = Literal[
    "add",
    "remove",
    "change_qty",
    "reorder_usual",
    "done",
    "yes",
    "no",
    "cancel",
    "unclear",
]
CartEdit = Literal["add", "remove", "change_qty"]
ClarificationKind = Literal[
    "pack_size",
    "only_bigger_pack",
    "out_of_stock",
    "ambiguous_item",
    "min_order",
    "not_found",
    "which_line",
    "usual_items",
    "need_details",
]
CheckoutAbortReason = Literal["preflight", "unresolved_attempt", "order_limit", "disabled"]


# ---------------------------------------------------------------- cart model
@dataclass(frozen=True)
class RequestedItem:
    """What the LLM extracted, e.g. name="akki", qty=2, unit="kg", search_term="rice"."""

    name: str
    qty: float
    unit: str | None = None
    search_term: str | None = None


@dataclass(frozen=True)
class CartEntry:
    """A resolved cart line (a concrete commerce variant)."""

    spin_id: str
    sku_id: str
    name: str
    pack_size: str
    quantity: int
    unit_price_inr: Money


@dataclass(frozen=True)
class Clarification:
    """One pending question (DESIGN.md §5: ask exactly one at a time)."""

    kind: ClarificationKind
    question: str
    options: tuple[CartEntry, ...] = ()
    requested: RequestedItem | None = None
    operation: CartEdit = "add"


# ------------------------------------------------------------------ events
@dataclass(frozen=True)
class CallStarted:
    user_known: bool
    language: str = "kn-IN"


@dataclass(frozen=True)
class UserUtterance:
    text: str
    intent: Intent
    requested_items: tuple[RequestedItem, ...] = ()


@dataclass(frozen=True)
class ItemsExtracted:
    """Commerce lookup finished. `resolved` lines are ready; `clarification` set if needed.

    `auto_resolved` marks lines produced by pack-size arithmetic (2 kg -> 2 x 1 kg)
    that MUST be spoken aloud. `remaining` holds requested items not looked up yet
    because `clarification` must be answered first.
    """

    resolved: tuple[CartEntry, ...]
    auto_resolved: tuple[CartEntry, ...] = ()
    clarification: Clarification | None = None
    operation: CartEdit = "add"
    remaining: tuple[RequestedItem, ...] = ()


@dataclass(frozen=True)
class OptionSelected:
    """The user picked option `index` (0-based) from the pending clarification."""

    index: int


@dataclass(frozen=True)
class UserConfirmed:
    """Explicit yes (houdu / haan) to the readback of `cart_version` and `cart_hash`."""

    cart_version: int
    cart_hash: str = ""


@dataclass(frozen=True)
class UserRejected:
    """User said no / wants to change something."""


@dataclass(frozen=True)
class UserCancelled:
    """User cancelled the unsubmitted cart/order attempt."""


@dataclass(frozen=True)
class UserDone:
    """User says "that's all" / "ashte" / "bas"."""


@dataclass(frozen=True)
class CheckoutSucceeded:
    order_ids: tuple[str, ...]
    partial: bool = False  # multi-store: some placed, some failed. Never retried.


@dataclass(frozen=True)
class CheckoutFailed:
    """Checkout did not report success. `terminal` means the provider definitively refused."""

    reason: str
    terminal: bool = False


@dataclass(frozen=True)
class CheckoutAborted:
    """A guard stopped checkout before anything was sent to the provider."""

    reason: CheckoutAbortReason


@dataclass(frozen=True)
class ReconcileResult:
    """Order-history reconciliation after CheckoutFailed.

    `confirmed_absent` means a successful read showed no order newer than the
    pre-checkout baseline. Without it, "not found" proves nothing.
    """

    order_found: bool
    order_ids: tuple[str, ...] = ()
    cart_hash: str = ""
    confirmed_absent: bool = False


@dataclass(frozen=True)
class OrderVerified:
    """Independent check of the placed order against order history."""

    verified: bool
    status: str = ""
    problem: bool = False


@dataclass(frozen=True)
class PriorAttemptUnresolved:
    """A durable journal entry shows an earlier checkout whose outcome is unknown."""


@dataclass(frozen=True)
class CartWriteFailed:
    """A cart write failed or could not be checked: the cart is not known until re-read."""


@dataclass(frozen=True)
class CartSnapshotReceived:
    """Adapter-sourced cart state used for readback and checkout preflight."""

    cart: tuple[CartEntry, ...]
    total_inr: Money
    address_id: str | None
    address_label: str = ""
    delivery_address: str = ""
    issues: tuple[str, ...] = ()
    fees_inr: Money = 0
    savings_inr: Money = 0
    store_count: int = 1
    payment_method: str = "COD"
    notice: str = ""


Event = (
    CallStarted
    | UserUtterance
    | ItemsExtracted
    | OptionSelected
    | UserConfirmed
    | UserRejected
    | UserCancelled
    | UserDone
    | CheckoutSucceeded
    | CheckoutFailed
    | CheckoutAborted
    | ReconcileResult
    | OrderVerified
    | PriorAttemptUnresolved
    | CartWriteFailed
    | CartSnapshotReceived
)


# ----------------------------------------------------------------- effects
@dataclass(frozen=True)
class Say:
    template: str  # template key, e.g. "readback", "pack_confirm", "filler_search"
    data: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    # Adapter-sourced facts for the presentation layer: readback lines or spoken options.
    lines: tuple[CartEntry, ...] = ()


@dataclass(frozen=True)
class CallCommerce:
    """Non-ordering commerce call."""

    op: Literal[
        "search",
        "go_to_items",
        "update_cart",
        "restore_cart",
        "get_cart",
        "get_orders",
        "verify_order",
    ]
    query: str | None = None
    items: tuple[RequestedItem, ...] = ()
    edit: CartEdit = "add"


@dataclass(frozen=True)
class PlaceOrder:
    """The ONLY way an order is placed. Runtime journals the attempt before calling checkout."""

    cart_version: int
    attempt_no: int
    payment_method: Literal["COD"] = "COD"
    cart_hash: str = ""


Effect = Say | CallCommerce | PlaceOrder


# ----------------------------------------------------------------- session
@dataclass(frozen=True)
class Session:
    state: State = State.GREETING
    language: str = "kn-IN"
    cart: tuple[CartEntry, ...] = ()
    cart_hash: str = ""
    total_inr: Money = 0
    fees_inr: Money = 0
    savings_inr: Money = 0
    store_count: int = 1
    payment_method: str = "COD"
    address_label: str = ""
    delivery_address: str = ""
    address_id: str | None = None
    issues: tuple[str, ...] = ()  # live conditions that block confirmation
    cart_version: int = 0  # bump on EVERY cart change
    confirmed_version: int | None = None  # cart_version the user said yes to
    # The cart last read back. Only this exact version and hash can be confirmed.
    readback_version: int | None = None
    readback_hash: str = ""
    pending: Clarification | None = None
    queue: tuple[RequestedItem, ...] = ()  # requested items waiting for the pending question
    staged: tuple[CartEntry, ...] = ()  # resolved lines waiting for the queue to finish
    staged_edit: CartEdit = "add"
    reask_count: int = 0
    attempt_no: int = 0  # increments per PlaceOrder for the same session
    failure_terminal: bool = False  # the last CheckoutFailed was a definitive refusal
    failure_reason: str = ""
    order_ids: tuple[str, ...] = ()
    order_status: str = ""
    order_verified: bool = False
    order_partial: bool = False


def format_money(amount: Money) -> str:
    """Canonical rupee text: "52", "52.5"→"52.50"; used for hashing and for display."""
    value = Decimal(amount)
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value.quantize(Decimal('0.01'))}"


def cart_hash(
    cart: tuple[CartEntry, ...],
    total_inr: Money | None = None,
    address_id: str | None = None,
    payment_method: str = "COD",
) -> str:
    lines = sorted(
        f"{entry.sku_id}:{entry.quantity}:{entry.name}:{entry.pack_size}:"
        f"{format_money(entry.unit_price_inr)}"
        for entry in cart
    )
    total = _cart_total(cart) if total_inr is None else total_inr
    identity = f"{address_id or ''}|{format_money(total)}|{payment_method}|"
    return sha256((identity + "\n".join(lines)).encode()).hexdigest()


def _cart_total(cart: tuple[CartEntry, ...]) -> Money:
    return sum((entry.quantity * entry.unit_price_inr for entry in cart), 0)


def _session_cart_hash(session: Session) -> str:
    return cart_hash(
        session.cart, session.total_inr or None, session.address_id, session.payment_method
    )


def can_confirm(session: Session) -> bool:
    """Whether the cart just read back is what would be ordered, and nothing blocks it."""
    return (
        session.state is State.READBACK
        and bool(session.cart)
        and not session.issues
        and session.readback_version == session.cart_version
        and session.readback_hash == _session_cart_hash(session)
    )


def readback_say(session: Session) -> Say:
    lines = "; ".join(
        f"{entry.quantity} x {entry.pack_size} {entry.name}" for entry in session.cart
    )
    total = session.total_inr or _cart_total(session.cart)
    return Say(
        "readback",
        data={
            "lines": lines,
            "total": format_money(total),
            "fees": format_money(session.fees_inr),
            "savings": format_money(session.savings_inr),
            "stores": str(session.store_count),
            "payment": session.payment_method,
            "address_label": session.address_label,
            "address": session.delivery_address or session.address_label,
        },
        lines=session.cart,
    )


def _with_readback(session: Session) -> tuple[Session, Say]:
    """Speak the current cart, or say what blocks it. Only a clean readback binds confirmation."""
    if session.issues:
        blocked = replace(session, readback_version=None, readback_hash="")
        say = readback_say(blocked)
        data = {**say.data, "issue": session.issues[0]}
        return blocked, Say("cart_blocked", data=data, lines=session.cart)
    current = _session_cart_hash(session)
    bound = replace(session, readback_version=session.cart_version, readback_hash=current)
    return bound, readback_say(bound)


def _unbind(session: Session) -> Session:
    return replace(session, confirmed_version=None, readback_version=None, readback_hash="")


def _merge_cart(
    cart: tuple[CartEntry, ...], additions: tuple[CartEntry, ...]
) -> tuple[CartEntry, ...]:
    merged = list(cart)
    positions = {entry.sku_id: index for index, entry in enumerate(merged)}
    for addition in additions:
        position = positions.get(addition.sku_id)
        if position is None:
            positions[addition.sku_id] = len(merged)
            merged.append(addition)
        else:
            current = merged[position]
            merged[position] = replace(current, quantity=current.quantity + addition.quantity)
    return tuple(merged)


def _apply_cart_edit(
    cart: tuple[CartEntry, ...], operation: CartEdit, edits: tuple[CartEntry, ...]
) -> tuple[CartEntry, ...] | None:
    if operation == "add":
        return _merge_cart(cart, edits)
    if operation == "remove":
        removed_skus = {entry.sku_id for entry in edits}
        return tuple(entry for entry in cart if entry.sku_id not in removed_skus)

    updated = list(cart)
    for replacement in edits:
        matches = [
            index
            for index, entry in enumerate(updated)
            if entry.sku_id == replacement.sku_id
            or entry.name.casefold() == replacement.name.casefold()
        ]
        if len(matches) != 1:
            return None
        updated[matches[0]] = replacement
    return tuple(updated)


def _clear_unsubmitted_cart(session: Session) -> Session:
    changed = bool(session.cart)
    return replace(
        _unbind(session),
        state=State.COLLECTING,
        cart=(),
        cart_hash="",
        total_inr=0,
        fees_inr=0,
        issues=(),
        cart_version=session.cart_version + int(changed),
        pending=None,
        queue=(),
        staged=(),
        reask_count=0,
    )


def _cancel(session: Session) -> tuple[Session, list[Effect]]:
    # "Cancelled" is only said once the real cart is known to be restored.
    if session.cart:
        return _clear_unsubmitted_cart(session), [CallCommerce("restore_cart")]
    return _clear_unsubmitted_cart(session), [Say("order_cancelled")]


def _clarification_say(clarification: Clarification) -> Say:
    item = clarification.requested.name if clarification.requested else ""
    return Say(
        "clarification",
        data={"kind": clarification.kind, "item": item},
        lines=clarification.options,
    )


def _apply_staged(
    session: Session,
    additions: tuple[CartEntry, ...],
    operation: CartEdit,
    auto_resolved: tuple[CartEntry, ...] = (),
) -> tuple[Session, list[Effect]]:
    """Apply every resolved line in one cart change, so one turn makes one cart write."""
    new_cart = _apply_cart_edit(session.cart, operation, additions)
    if new_cart is None:
        return replace(session, staged=(), pending=None), [Say("cart_edit_unclear")]
    next_session = replace(
        _unbind(session),
        state=State.READBACK,
        cart=new_cart,
        total_inr=_cart_total(new_cart),
        fees_inr=0,
        cart_hash=cart_hash(new_cart, _cart_total(new_cart), session.address_id),
        cart_version=session.cart_version + 1,
        pending=None,
        staged=(),
        queue=(),
        reask_count=0,
    )
    effects: list[Effect] = []
    if auto_resolved:
        effects.append(Say("pack_confirm"))
    effects.append(CallCommerce("update_cart"))
    return next_session, effects


def _advance_queue(session: Session, effects: list[Effect]) -> tuple[Session, list[Effect]]:
    """After a question is settled: look up waiting items, or apply what was resolved."""
    cleared = replace(session, pending=None, reask_count=0)
    if cleared.queue:
        next_session = replace(cleared, state=State.COLLECTING, queue=())
        return next_session, [
            *effects,
            CallCommerce(
                "search",
                query=cleared.queue[0].name,
                items=cleared.queue,
                edit=cleared.staged_edit,
            ),
        ]
    if cleared.staged:
        next_session, applied = _apply_staged(cleared, cleared.staged, cleared.staged_edit)
        return next_session, [*effects, *applied]
    if cleared.cart:
        next_session = replace(_unbind(cleared), state=State.READBACK)
        return next_session, [*effects, CallCommerce("get_cart")]
    return replace(cleared, state=State.COLLECTING), [*effects, Say("ask_what_to_add")]


def _on_items_extracted(
    session: Session, event: ItemsExtracted, *, invalidate: bool = False
) -> tuple[Session, list[Effect]]:
    base = _unbind(session) if invalidate else session
    if event.clarification is not None:
        staged = (*base.staged, *event.resolved)
        return (
            replace(
                base,
                state=State.CLARIFYING,
                pending=event.clarification,
                queue=event.remaining,
                staged=staged,
                staged_edit=event.operation,
                reask_count=0,
            ),
            [_clarification_say(event.clarification)],
        )
    additions = (*base.staged, *event.resolved)
    if additions:
        return _apply_staged(base, additions, event.operation, event.auto_resolved)
    if base.state is State.CLARIFYING:
        return replace(base, state=State.COLLECTING, pending=None, reask_count=0), []
    return base, [Say("ask_what_to_add")] if base.state is State.COLLECTING else []


def _search_effects(event: UserUtterance, edit: CartEdit) -> list[Effect]:
    return [
        Say("filler_search"),
        CallCommerce("search", query=event.text, items=event.requested_items, edit=edit),
    ]


def transition(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    """Return the next session and the effects to run. Pure function.

    Rules (each has tests in tests/test_state_machine.py):

    1. GREETING + CallStarted(user_known=True) -> COLLECTING, Say("greeting").
       Unknown user -> DONE with Say("unknown_user").
    2. COLLECTING + UserUtterance(add/remove/change_qty) -> search effect. One turn's
       items are resolved together and written to the cart in one update.
    3. A question stops the queue: resolved lines are staged and later items wait in
       `queue`. Answering or skipping continues exactly where the user left off.
    4. READBACK + UserConfirmed is honoured only when version and hash equal the
       readback most recently spoken, the cart equals that readback, and no live
       issue blocks it. Otherwise the current cart is read back and nothing is placed.
    5. Any cart change clears the readback binding, so it needs a fresh readback and yes.
    6. PlaceOrder is emitted ONLY on the CONFIRMED -> PLACING step.
    7. PLACING + CheckoutSucceeded -> VERIFYING. PLACING + CheckoutFailed -> FAILED and
       orders are reconciled. Never re-emit PlaceOrder from PLACING/FAILED.
    8. FAILED + order found -> VERIFYING. FAILED + definitive refusal with proof of no
       new order -> READBACK with a fresh readback. Anything else -> ORDER_UNKNOWN.
    9. ORDER_UNKNOWN is terminal. DONE ignores everything except informing the user.
    """
    if isinstance(event, PriorAttemptUnresolved):
        return replace(_unbind(session), state=State.ORDER_UNKNOWN), [Say("order_unknown")]
    if isinstance(event, CartWriteFailed):
        return _on_cart_write_failed(session)
    if isinstance(event, CartSnapshotReceived):
        return _on_cart_snapshot(session, event)

    match session.state:
        case State.GREETING:
            return _on_greeting(session, event)
        case State.COLLECTING:
            return _on_collecting(session, event)
        case State.CLARIFYING:
            return _on_clarifying(session, event)
        case State.READBACK:
            return _on_readback(session, event)
        case State.CONFIRMED:
            return _on_confirmed(session, event)
        case State.PLACING:
            return _on_placing(session, event)
        case State.FAILED:
            return _on_failed(session, event)
        case State.VERIFYING:
            return _on_verifying(session, event)
        case State.ORDER_UNKNOWN:
            return _on_order_unknown(session, event)
        case State.DONE:
            return _on_done(session, event)
        case _:  # pyright: ignore[reportUnnecessaryComparison]
            return session, []


def _on_greeting(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if not isinstance(event, CallStarted):
        return session, []

    if event.user_known:
        return (
            replace(session, state=State.COLLECTING, language=event.language),
            [Say("greeting")],
        )

    return replace(session, state=State.DONE, language=event.language), [Say("unknown_user")]


def _on_collecting(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, UserUtterance):
        if event.intent in ("add", "remove", "change_qty"):
            return session, _search_effects(event, event.intent)
        if event.intent == "reorder_usual":
            return session, [Say("filler_search"), CallCommerce("go_to_items")]
        if event.intent == "unclear":
            return session, [Say("intent_error")]
        return session, [Say("ask_what_to_add")]

    if isinstance(event, UserCancelled):
        return _cancel(session)

    if isinstance(event, ItemsExtracted):
        return _on_items_extracted(session, event)

    if isinstance(event, UserDone):
        if session.cart:
            next_session = replace(_unbind(session), state=State.READBACK, reask_count=0)
            return next_session, [CallCommerce("get_cart")]
        return session, [Say("ask_what_to_add")]

    if isinstance(event, UserRejected):
        return session, [Say("ask_what_to_add")]

    return session, []


def _same_request(first: RequestedItem, second: RequestedItem) -> bool:
    def words(item: RequestedItem) -> set[str]:
        return set((item.search_term or item.name).casefold().split())

    return bool(words(first) & words(second))


def _reask(session: Session) -> tuple[Session, list[Effect]]:
    pending = session.pending
    if pending is None:
        return session, []
    if session.reask_count >= 2:
        return _advance_queue(session, [Say("item_skipped")])
    return replace(session, reask_count=session.reask_count + 1), [_clarification_say(pending)]


def _on_clarifying(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, ItemsExtracted):
        return _on_items_extracted(session, event)

    if isinstance(event, OptionSelected):
        pending = session.pending
        if pending is None or not 0 <= event.index < len(pending.options):
            return _reask(session)
        chosen = pending.options[event.index]
        updated = replace(
            session,
            staged=(*session.staged, chosen),
            staged_edit=pending.operation,
        )
        return _advance_queue(updated, [])

    if isinstance(event, UserRejected):
        return _advance_queue(session, [Say("item_skipped")])

    if isinstance(event, UserCancelled):
        return _cancel(session)

    if isinstance(event, UserUtterance):
        pending = session.pending
        # Lines staged for removal must never be swept into an add.
        if event.intent == "add" and event.requested_items and session.staged_edit == "add":
            kept: tuple[RequestedItem, ...] = ()
            if (
                pending is not None
                and pending.requested is not None
                and pending.options
                and not any(_same_request(pending.requested, new) for new in event.requested_items)
            ):
                kept = (pending.requested,)  # "also add sugar" must not lose the open question
            items = (*event.requested_items, *kept, *session.queue)
            next_session = replace(
                session,
                state=State.COLLECTING,
                pending=None,
                queue=(),
                staged_edit="add",
                reask_count=0,
            )
            return next_session, [
                Say("filler_search"),
                CallCommerce("search", query=items[0].name, items=items, edit="add"),
            ]
        return _reask(session)

    if isinstance(event, UserDone):
        return _reask(session)

    return session, []


def _on_readback(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, UserRejected):
        next_session = replace(_unbind(session), state=State.COLLECTING, reask_count=0)
        return next_session, [Say("what_to_change")]

    if isinstance(event, UserCancelled):
        return _cancel(session)

    if isinstance(event, UserDone):
        bound, say = _with_readback(session)
        return bound, [say]

    if isinstance(event, UserUtterance):
        if event.intent in ("add", "remove", "change_qty"):
            return replace(_unbind(session), state=State.COLLECTING), _search_effects(
                event, event.intent
            )
        if event.intent == "reorder_usual":
            return (
                replace(_unbind(session), state=State.COLLECTING, reask_count=0),
                [Say("filler_search"), CallCommerce("go_to_items")],
            )
        if event.intent == "unclear":
            if session.reask_count == 0:
                return replace(session, reask_count=1), [Say("confirm_reask")]
            return (
                replace(_unbind(session), state=State.COLLECTING, reask_count=0),
                [Say("ask_what_to_add")],
            )
        return session, []

    if isinstance(event, ItemsExtracted):
        if event.clarification is None and not event.resolved:
            return session, []
        return _on_items_extracted(session, event, invalidate=True)

    if isinstance(event, UserConfirmed):
        expected_hash = _session_cart_hash(session)
        if (
            not can_confirm(session)
            or event.cart_version != session.cart_version
            or event.cart_hash != expected_hash
        ):
            bound, say = _with_readback(session)
            return bound, [say]
        confirmed = replace(
            session,
            state=State.CONFIRMED,
            cart_hash=expected_hash,
            confirmed_version=session.cart_version,
        )
        return _on_confirmed(confirmed, event)

    return session, []


def _on_confirmed(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if not isinstance(event, UserConfirmed):
        return session, []
    if (
        not session.cart
        or session.confirmed_version != session.cart_version
        or event.cart_version != session.cart_version
        or event.cart_hash != _session_cart_hash(session)
    ):
        return session, []
    next_session = replace(session, state=State.PLACING, attempt_no=session.attempt_no + 1)
    return next_session, [
        PlaceOrder(
            session.cart_version,
            next_session.attempt_no,
            cart_hash=_session_cart_hash(session),
        )
    ]


def _on_placing(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, CheckoutSucceeded):
        return (
            replace(
                session,
                state=State.VERIFYING,
                order_ids=event.order_ids,
                order_partial=event.partial,
            ),
            [CallCommerce("verify_order")],
        )

    if isinstance(event, CheckoutFailed):
        return (
            replace(
                session,
                state=State.FAILED,
                failure_terminal=event.terminal,
                failure_reason=event.reason,
            ),
            [Say("checking_order"), CallCommerce("get_orders")],
        )

    if isinstance(event, CheckoutAborted):
        if event.reason == "unresolved_attempt":
            return replace(_unbind(session), state=State.ORDER_UNKNOWN), [Say("order_unknown")]
        unbound = replace(_unbind(session), state=State.READBACK)
        template = {
            "preflight": "order_not_attempted",
            "order_limit": "order_limit",
            "disabled": "checkout_disabled",
        }[event.reason]
        return unbound, [Say(template), CallCommerce("get_cart")]

    if isinstance(event, (UserCancelled, UserRejected, UserUtterance)):
        return session, [Say("order_in_progress")]

    return session, []


def _on_cart_write_failed(session: Session) -> tuple[Session, list[Effect]]:
    if session.state in (
        State.PLACING,
        State.FAILED,
        State.VERIFYING,
        State.DONE,
        State.ORDER_UNKNOWN,
    ):
        return session, []
    return (
        replace(
            _unbind(session),
            state=State.COLLECTING,
            cart_version=session.cart_version + 1,
            pending=None,
            queue=(),
            staged=(),
            reask_count=0,
        ),
        [],
    )


def _on_cart_snapshot(
    session: Session, event: CartSnapshotReceived
) -> tuple[Session, list[Effect]]:
    if session.state in (State.FAILED, State.VERIFYING, State.DONE, State.ORDER_UNKNOWN):
        return session, []  # an order outcome is being settled or is final; never reopen the cart
    address_id = event.address_id if event.cart or event.address_id else session.address_id
    next_hash = cart_hash(event.cart, event.total_inr, address_id, event.payment_method)
    current_hash = _session_cart_hash(session)
    changed = next_hash != current_hash
    placing = session.state is State.PLACING
    waiting = session.state is State.CLARIFYING and session.pending is not None
    next_state = State.READBACK if event.cart else State.COLLECTING
    next_state = State.CLARIFYING if waiting else next_state
    next_session = replace(
        session,
        state=next_state,
        cart=event.cart,
        cart_hash=next_hash,
        total_inr=event.total_inr,
        fees_inr=event.fees_inr,
        savings_inr=event.savings_inr,
        store_count=event.store_count,
        payment_method=event.payment_method,
        issues=event.issues,
        address_id=address_id,
        address_label=event.address_label or session.address_label,
        delivery_address=event.delivery_address or session.delivery_address,
        cart_version=session.cart_version + (1 if changed else 0),
        confirmed_version=None if changed or placing else session.confirmed_version,
        reask_count=0,
    )
    if changed or placing:
        next_session = _unbind(next_session)
    if waiting:
        return next_session, []
    effects: list[Effect] = []
    if event.notice:
        effects.append(Say(event.notice))
    if not event.cart:
        return next_session, [*effects, Say("ask_what_to_add")]
    if placing and changed:
        effects.append(Say("cart_changed"))
    bound, say = _with_readback(next_session)
    effects.append(say)
    return bound, effects


def _on_failed(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, ReconcileResult):
        if event.order_found and event.cart_hash == session.cart_hash:
            return (
                replace(session, state=State.VERIFYING, order_ids=event.order_ids),
                [CallCommerce("verify_order")],
            )
        if not event.order_found and event.confirmed_absent and session.failure_terminal:
            return (
                replace(_unbind(session), state=State.READBACK),
                [
                    Say("order_rejected", data={"reason": session.failure_reason}),
                    CallCommerce("get_cart"),
                ],
            )
        return replace(_unbind(session), state=State.ORDER_UNKNOWN), [Say("order_unknown")]
    if isinstance(event, (UserCancelled, UserRejected, UserUtterance, UserConfirmed, UserDone)):
        return session, [Say("checking_order")]
    return session, []


def _on_verifying(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, OrderVerified):
        verified = replace(
            session,
            state=State.DONE,
            order_verified=event.verified and not event.problem,
            order_status=event.status,
        )
        if event.problem:
            return verified, [Say("order_problem")]
        if session.order_partial:
            return verified, [Say("order_partial")]
        if not event.verified:
            return verified, [Say("order_placed_unverified")]
        return verified, [Say("order_placed")]
    if isinstance(event, (UserCancelled, UserRejected, UserUtterance, UserConfirmed, UserDone)):
        return session, [Say("order_in_progress")]
    return session, []


def _on_order_unknown(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(
        event,
        (
            UserCancelled,
            UserRejected,
            UserUtterance,
            UserConfirmed,
            UserDone,
            OptionSelected,
        ),
    ):
        return session, [Say("order_unknown")]
    return session, []


def _on_done(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if not session.order_ids:
        return session, []
    if isinstance(event, UserCancelled):
        return session, [Say("cancel_via_support")]
    if isinstance(event, UserRejected):
        return session, [Say("order_already_placed")]
    if isinstance(event, UserUtterance) and event.intent == "cancel":
        return session, [Say("cancel_via_support")]
    if isinstance(event, UserUtterance) and event.intent in (
        "add",
        "remove",
        "change_qty",
    ):
        return session, [Say("order_already_placed")]
    return session, []
