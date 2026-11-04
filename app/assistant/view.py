"""What any capability shows the user. Keeps the web layer free of workflow internals."""

from __future__ import annotations

from dataclasses import dataclass

from app.orchestrator.state_machine import CartEntry, Money


@dataclass(frozen=True)
class WorkflowView:
    domain: str = ""
    state: str = "COLLECTING"
    lines: tuple[CartEntry, ...] = ()
    cart_version: int = 0
    cart_hash: str = ""
    readback_version: int | None = None
    readback_hash: str = ""
    confirmable: bool = False
    total_inr: Money = 0
    fees_inr: Money = 0
    savings_inr: Money = 0
    store_count: int = 1
    issues: tuple[str, ...] = ()
    payment_method: str = "COD"
    address_label: str = ""
    delivery_address: str = ""
    options: tuple[CartEntry, ...] = ()
    order_ids: tuple[str, ...] = ()
    order_status: str = ""
    order_verified: bool = False
    order_partial: bool = False
