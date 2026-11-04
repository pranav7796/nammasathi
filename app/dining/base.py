"""Table booking: domain models and the provider contract (Swiggy Dineout)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DiningRestaurant:
    id: str
    name: str
    area: str = ""
    cost_for_two: str = ""


@dataclass(frozen=True)
class TableSlot:
    """One free, bookable time. Paid prebook deals need UPI and are not offered."""

    date: str  # YYYY-MM-DD
    time: str  # HH:MM, 24-hour
    display_time: str  # as Swiggy shows it, e.g. "07:30 PM"
    reservation_time: int
    slot_id: int
    item_id: str


@dataclass(frozen=True)
class BookingResult:
    order_id: str
    status: str = ""


@dataclass(frozen=True)
class BookingStatus:
    status: str


class DiningAdapter(Protocol):
    async def search(
        self, query: str, latitude: float, longitude: float
    ) -> list[DiningRestaurant]: ...

    async def free_slots(
        self, restaurant_id: str, date: str, latitude: float, longitude: float
    ) -> list[TableSlot]: ...

    async def book(
        self, restaurant_id: str, slot: TableSlot, guests: int, latitude: float, longitude: float
    ) -> BookingResult:
        """NOT idempotent, and there is no order history to reconcile against. Never retry."""
        ...

    async def status(self, order_id: str) -> BookingStatus: ...
