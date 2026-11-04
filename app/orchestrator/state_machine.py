"""Dialog state machine — PURE (no I/O, no clock, no randomness). DESIGN.md §4.

    Pranav implements `transition()`. Coding agents: do NOT implement it;
    only write tests / review (see AGENTS.md).

The runtime feeds Events in and executes the returned Effects (speak, call the
commerce adapter, place the order, notify family). Because the function is pure,
every rule below is unit- and property-testable.

States (mermaid in DESIGN.md §4):
    GREETING -> COLLECTING -> (CLARIFYING <-> COLLECTING) -> READBACK
    READBACK -> COLLECTING (edit) | CONFIRMED (explicit yes)
    CONFIRMED -> PLACING -> DONE | FAILED
    FAILED -> READBACK (reconcile: no order found) | DONE (reconcile: order found)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
from typing import Literal


class State(StrEnum):
    GREETING = "GREETING"
    COLLECTING = "COLLECTING"
    CLARIFYING = "CLARIFYING"
    READBACK = "READBACK"
    CONFIRMED = "CONFIRMED"
    PLACING = "PLACING"
    DONE = "DONE"
    FAILED = "FAILED"


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


# ---------------------------------------------------------------- cart model
@dataclass(frozen=True)
class RequestedItem:
    """What the LLM extracted, e.g. name="rice", qty=2, unit="kg"."""

    name: str
    qty: float
    unit: str | None = None


@dataclass(frozen=True)
class CartEntry:
    """A resolved cart line (a concrete Swiggy variant)."""

    spin_id: str
    sku_id: str
    name: str
    pack_size: str
    quantity: int
    unit_price_inr: int


@dataclass(frozen=True)
class Clarification:
    """One pending question (DESIGN.md §5: ask exactly one at a time)."""

    kind: Literal["pack_size", "only_bigger_pack", "out_of_stock", "ambiguous_item", "min_order"]
    question: str
    options: tuple[CartEntry, ...] = ()


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
    that MUST be spoken aloud.
    """

    resolved: tuple[CartEntry, ...]
    auto_resolved: tuple[CartEntry, ...] = ()
    clarification: Clarification | None = None
    operation: CartEdit = "add"


@dataclass(frozen=True)
class UserConfirmed:
    """Explicit yes (houdu / haan) to the readback of `cart_version`."""

    cart_version: int
    cart_hash: str = ""


@dataclass(frozen=True)
class UserRejected:
    """User said no / wants to change something at READBACK."""


@dataclass(frozen=True)
class UserCancelled:
    """User cancelled the unsubmitted cart/order attempt."""


@dataclass(frozen=True)
class UserDone:
    """User says "that's all" / "ashte" / "bas"."""


@dataclass(frozen=True)
class CheckoutSucceeded:
    order_ids: tuple[str, ...]


@dataclass(frozen=True)
class CheckoutFailed:
    reason: str
    terminal: bool = False  # domain failure (e.g. not serviceable) vs unknown (timeout/5xx)


@dataclass(frozen=True)
class ReconcileResult:
    """Result of get_orders reconciliation after CheckoutFailed."""

    order_found: bool
    order_ids: tuple[str, ...] = ()
    cart_hash: str = ""


@dataclass(frozen=True)
class CartSnapshotReceived:
    """Adapter-sourced cart state used for readback and checkout preflight."""

    cart: tuple[CartEntry, ...]
    total_inr: int
    address_id: str | None
    address_label: str = ""
    delivery_address: str = ""


Event = (
    CallStarted
    | UserUtterance
    | ItemsExtracted
    | UserConfirmed
    | UserRejected
    | UserCancelled
    | UserDone
    | CheckoutSucceeded
    | CheckoutFailed
    | ReconcileResult
    | CartSnapshotReceived
)


# ----------------------------------------------------------------- effects
@dataclass(frozen=True)
class Say:
    template: str  # template key, e.g. "readback", "pack_confirm", "filler_search"
    data: dict[str, str] = field(default_factory=lambda: dict[str, str]())


@dataclass(frozen=True)
class CallCommerce:
    """Non-ordering commerce call: search / go_to_items / update_cart / get_cart / get_orders."""

    op: Literal["search", "go_to_items", "update_cart", "get_cart", "get_orders"]
    query: str | None = None
    items: tuple[RequestedItem, ...] = ()
    edit: CartEdit = "add"


@dataclass(frozen=True)
class PlaceOrder:
    """The ONLY way an order is placed. Runtime writes order_attempts (PENDING) first."""

    cart_version: int
    attempt_no: int
    payment_method: Literal["COD"] = "COD"
    cart_hash: str = ""


@dataclass(frozen=True)
class NotifyFamily:
    reason: str


Effect = Say | CallCommerce | PlaceOrder | NotifyFamily


# ----------------------------------------------------------------- session
@dataclass(frozen=True)
class Session:
    state: State = State.GREETING
    language: str = "kn-IN"
    cart: tuple[CartEntry, ...] = ()
    cart_hash: str = ""
    total_inr: int = 0
    address_label: str = ""
    delivery_address: str = ""
    address_id: str | None = None
    cart_version: int = 0  # bump on EVERY cart change
    confirmed_version: int | None = None  # cart_version the user said yes to
    pending: Clarification | None = None
    reask_count: int = 0
    attempt_no: int = 0  # increments per PlaceOrder for the same session
    order_ids: tuple[str, ...] = ()


def cart_hash(
    cart: tuple[CartEntry, ...], total_inr: int | None = None, address_id: str | None = None
) -> str:
    lines = sorted(
        f"{entry.sku_id}:{entry.quantity}:{entry.name}:{entry.pack_size}:{entry.unit_price_inr}"
        for entry in cart
    )
    total = _cart_total(cart) if total_inr is None else total_inr
    return sha256((f"{address_id or ''}|{total}|" + "\n".join(lines)).encode()).hexdigest()


def _cart_total(cart: tuple[CartEntry, ...]) -> int:
    return sum(entry.quantity * entry.unit_price_inr for entry in cart)


def _session_cart_hash(session: Session) -> str:
    return cart_hash(session.cart, session.total_inr or None, session.address_id)


def _readback_say(session: Session) -> Say:
    lines = "; ".join(
        f"{entry.quantity} x {entry.pack_size} {entry.name}" for entry in session.cart
    )
    total = session.total_inr or _cart_total(session.cart)
    return Say(
        "readback",
        data={
            "lines": lines,
            "total": str(total),
            "payment": "COD",
            "address_label": session.address_label,
            "address": session.delivery_address or session.address_label,
        },
    )


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
        session,
        state=State.COLLECTING,
        cart=(),
        cart_hash="",
        total_inr=0,
        cart_version=session.cart_version + int(changed),
        confirmed_version=None,
        pending=None,
        reask_count=0,
    )


def _failure_kind(reason: str) -> str:
    normalized = reason.casefold().replace("_", " ")
    if "401" in normalized or "unauthorized" in normalized or "token expired" in normalized:
        return "auth"
    if "429" in normalized or "rate limit" in normalized:
        return "rate_limit"
    if "minimum order" in normalized or "min order" in normalized or "₹99" in normalized:
        return "min_order"
    if "cart expired" in normalized or "cart abandoned" in normalized:
        return "cart_expired"
    if "cart changed" in normalized:
        return "cart_changed"
    return "other"


def _map_failure(session: Session, event: Event) -> tuple[Session, list[Effect]] | None:
    if not isinstance(event, CheckoutFailed):
        return None

    kind = _failure_kind(event.reason)
    if kind == "auth":
        return (
            replace(session, state=State.FAILED),
            [Say("swiggy_login_expired"), NotifyFamily(reason=event.reason)],
        )
    if kind == "rate_limit":
        return session, [Say("filler_search")]
    if kind == "min_order" and session.state is State.READBACK:
        clarification = Clarification("min_order", event.reason)
        return replace(session, state=State.CLARIFYING, pending=clarification), [Say("min_order")]
    if kind == "cart_expired" and session.state in (State.READBACK, State.PLACING):
        return replace(session, state=State.COLLECTING), [
            Say("cart_expired"),
            CallCommerce("get_cart"),
        ]
    if kind == "cart_changed" and session.state is State.PLACING:
        return replace(session, state=State.READBACK, reask_count=0), [Say("cart_changed")]
    return None


def transition(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    """Return the next session and the effects to run. Pure function.

    Rules (each has tests in tests/test_state_machine.py):

    1. GREETING + CallStarted(user_known=True) -> COLLECTING, Say("greeting").
       Unknown user -> DONE with Say("unknown_user").
    2. COLLECTING + UserUtterance(intent in add/remove/change_qty) ->
       COLLECTING, CallCommerce("search", ...) and Say("filler_search").
       intent "reorder_usual" -> CallCommerce("go_to_items").
    3. COLLECTING + ItemsExtracted:
       - clarification set -> CLARIFYING, Say(question). One question at a time.
       - resolved lines -> merged into cart, cart_version += 1, stay COLLECTING.
       - auto_resolved lines (e.g. 2 kg -> 2 x 1 kg) are ALWAYS spoken aloud
         (Say("pack_confirm")) — never added silently.
    4. CLARIFYING + answer (UserUtterance / ItemsExtracted) -> COLLECTING once resolved.
    5. COLLECTING + UserDone (non-empty cart) -> READBACK, Say("readback") listing
       every line, pack size, total, COD, address. Empty cart -> stay, ask what to add.
    6. READBACK + UserRejected or any cart edit -> COLLECTING (edits bump
       cart_version, which invalidates any earlier confirmation; must re-readback).
    7. READBACK + UserConfirmed(v) where v == cart_version -> CONFIRMED, then
       immediately PLACING with exactly one PlaceOrder(cart_version, attempt_no+1).
       Stale confirmation (v != cart_version) -> re-readback, no PlaceOrder.
       Intent "unclear" never counts as yes.
    8. PlaceOrder is emitted ONLY on the CONFIRMED -> PLACING step, and at most
       once per (cart_version, attempt_no).
    9. PLACING + CheckoutSucceeded -> DONE, Say("order_placed").
       PLACING + CheckoutFailed -> FAILED, CallCommerce("get_orders") (reconcile),
       Say("checking_order"). Never re-emit PlaceOrder from PLACING/FAILED.
    10. FAILED + ReconcileResult(order_found=True) -> DONE, Say("order_placed").
        FAILED + ReconcileResult(order_found=False) -> READBACK, Say("order_failed_retry").
        A terminal CheckoutFailed (e.g. not serviceable) also NotifyFamily.
    11. DONE ignores everything except a deliberate re-order (handled by runtime).
    """
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
        case State.DONE:
            return _on_done(session, event)
        case _:
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
    mapped = _map_failure(session, event)
    if mapped is not None:
        return mapped

    if isinstance(event, UserUtterance):
        if event.intent in ("add", "remove", "change_qty"):
            return session, [
                Say("filler_search"),
                CallCommerce(
                    "search",
                    query=event.text,
                    items=event.requested_items,
                    edit=event.intent,
                ),
            ]
        if event.intent == "reorder_usual":
            return session, [Say("filler_search"), CallCommerce("go_to_items")]
        return session, []

    if isinstance(event, UserCancelled):
        return _clear_unsubmitted_cart(session), [Say("order_cancelled")]

    if isinstance(event, ItemsExtracted):
        if event.clarification is not None:
            return (
                replace(session, state=State.CLARIFYING, pending=event.clarification),
                [Say("clarification", data={"kind": event.clarification.kind})],
            )
        if event.resolved:
            new_cart = _apply_cart_edit(session.cart, event.operation, event.resolved)
            if new_cart is None:
                return session, [Say("cart_edit_unclear")]
            next_session = replace(
                session,
                state=State.READBACK,
                cart=new_cart,
                total_inr=_cart_total(new_cart),
                cart_hash=cart_hash(new_cart, _cart_total(new_cart), session.address_id),
                cart_version=session.cart_version + 1,
                confirmed_version=None,
                reask_count=0,
            )
        else:
            next_session = session
        effects: list[Effect] = []
        if event.auto_resolved:
            effects.append(Say("pack_confirm"))
        if event.resolved:
            effects.append(CallCommerce("update_cart"))
        return next_session, effects

    if isinstance(event, UserDone):
        if session.cart:
            return replace(session, state=State.READBACK, reask_count=0), [CallCommerce("get_cart")]
        return session, [Say("ask_what_to_add")]

    return session, []


def _on_clarifying(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    mapped = _map_failure(session, event)
    if mapped is not None:
        return mapped

    if isinstance(event, ItemsExtracted):
        if event.clarification is not None:
            return (
                replace(session, pending=event.clarification),
                [Say("clarification", data={"kind": event.clarification.kind})],
            )
        if event.resolved:
            new_cart = _apply_cart_edit(session.cart, event.operation, event.resolved)
            if new_cart is None:
                return session, [Say("cart_edit_unclear")]
            next_session = replace(
                session,
                state=State.READBACK,
                cart=new_cart,
                total_inr=_cart_total(new_cart),
                cart_hash=cart_hash(new_cart, _cart_total(new_cart), session.address_id),
                cart_version=session.cart_version + 1,
                confirmed_version=None,
                pending=None,
                reask_count=0,
            )
        else:
            next_session = replace(session, state=State.COLLECTING, pending=None)
        effects: list[Effect] = []
        if event.auto_resolved:
            effects.append(Say("pack_confirm"))
        if event.resolved:
            effects.append(CallCommerce("update_cart"))
        return next_session, effects

    if isinstance(event, UserUtterance) and event.intent != "unclear":
        next_session = replace(session, state=State.COLLECTING, pending=None)
        if event.intent in ("add", "remove", "change_qty"):
            return next_session, [
                Say("filler_search"),
                CallCommerce(
                    "search",
                    query=event.text,
                    items=event.requested_items,
                    edit=event.intent,
                ),
            ]
        if event.intent == "reorder_usual":
            return next_session, [Say("filler_search"), CallCommerce("go_to_items")]
        return next_session, []

    if isinstance(event, UserCancelled):
        return _clear_unsubmitted_cart(session), [Say("order_cancelled")]

    return session, []


def _on_readback(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    mapped = _map_failure(session, event)
    if mapped is not None:
        return mapped

    if isinstance(event, UserRejected):
        return replace(session, state=State.COLLECTING, confirmed_version=None, reask_count=0), []

    if isinstance(event, UserCancelled):
        return _clear_unsubmitted_cart(session), [Say("order_cancelled")]

    if isinstance(event, UserUtterance):
        if event.intent in ("add", "remove", "change_qty"):
            return (
                replace(session, state=State.COLLECTING, confirmed_version=None),
                [
                    Say("filler_search"),
                    CallCommerce(
                        "search",
                        query=event.text,
                        items=event.requested_items,
                        edit=event.intent,
                    ),
                ],
            )
        if event.intent == "reorder_usual":
            return (
                replace(session, state=State.COLLECTING, confirmed_version=None, reask_count=0),
                [Say("filler_search"), CallCommerce("go_to_items")],
            )
        if event.intent == "unclear":
            if session.reask_count == 0:
                return replace(session, reask_count=1), [Say("confirm_reask")]
            return (
                replace(session, state=State.COLLECTING, reask_count=0),
                [Say("ask_what_to_add")],
            )
        return session, []

    if isinstance(event, ItemsExtracted):
        if event.clarification is not None:
            return (
                replace(
                    session,
                    state=State.CLARIFYING,
                    pending=event.clarification,
                    confirmed_version=None,
                ),
                [Say("clarification", data={"kind": event.clarification.kind})],
            )
        if event.resolved:
            new_cart = _apply_cart_edit(session.cart, event.operation, event.resolved)
            if new_cart is None:
                return session, [Say("cart_edit_unclear")]
            next_session = replace(
                session,
                state=State.READBACK,
                cart=new_cart,
                total_inr=_cart_total(new_cart),
                cart_hash=cart_hash(new_cart, _cart_total(new_cart), session.address_id),
                cart_version=session.cart_version + 1,
                confirmed_version=None,
                reask_count=0,
            )
            effects: list[Effect] = []
            if event.auto_resolved:
                effects.append(Say("pack_confirm"))
            effects.append(CallCommerce("update_cart"))
            return next_session, effects
        return session, []

    if isinstance(event, UserConfirmed):
        expected_hash = _session_cart_hash(session)
        if (
            event.cart_version != session.cart_version
            or event.cart_hash != expected_hash
            or not session.cart
        ):
            return session, [_readback_say(session)]
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
    mapped = _map_failure(session, event)
    if mapped is not None:
        return mapped

    if isinstance(event, CheckoutSucceeded):
        return (
            replace(session, state=State.DONE, order_ids=event.order_ids),
            [Say("order_placed")],
        )

    if isinstance(event, CheckoutFailed):
        effects: list[Effect] = [Say("checking_order"), CallCommerce("get_orders")]
        if event.terminal:
            effects.append(NotifyFamily(reason=event.reason))
        return replace(session, state=State.FAILED), effects

    if isinstance(event, (UserCancelled, UserRejected, UserUtterance)):
        return session, [Say("order_already_placed")]

    return session, []


def _on_cart_snapshot(
    session: Session, event: CartSnapshotReceived
) -> tuple[Session, list[Effect]]:
    next_hash = cart_hash(event.cart, event.total_inr, event.address_id)
    current_hash = _session_cart_hash(session)
    changed = next_hash != current_hash
    placing = session.state is State.PLACING
    next_state = State.READBACK if event.cart else State.COLLECTING
    next_session = replace(
        session,
        state=next_state,
        cart=event.cart,
        cart_hash=next_hash,
        total_inr=event.total_inr,
        address_id=event.address_id,
        address_label=event.address_label or session.address_label,
        delivery_address=event.delivery_address or session.delivery_address,
        cart_version=session.cart_version + (1 if changed else 0),
        confirmed_version=None if changed or placing else session.confirmed_version,
        reask_count=0,
    )
    if not event.cart:
        return next_session, [Say("ask_what_to_add")]
    effects: list[Effect] = []
    if placing and changed:
        effects.append(Say("cart_changed"))
    effects.append(_readback_say(next_session))
    return next_session, effects


def _on_failed(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if not isinstance(event, ReconcileResult):
        return session, []
    if event.order_found and event.cart_hash == _session_cart_hash(session):
        return replace(session, state=State.DONE, order_ids=event.order_ids), [Say("order_placed")]
    return replace(session, state=State.READBACK), [Say("order_failed_retry")]


def _on_done(session: Session, event: Event) -> tuple[Session, list[Effect]]:
    if isinstance(event, (UserCancelled, UserRejected)):
        return session, [Say("order_already_placed")]
    if isinstance(event, UserUtterance) and event.intent in (
        "add",
        "remove",
        "change_qty",
        "cancel",
    ):
        return session, [Say("order_already_placed")]
    return session, []
