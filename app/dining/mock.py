"""MockDineout: deterministic table booking for development, tests, and demos."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.commerce.base import CommerceRejected, RejectionKind
from app.dining.base import BookingResult, BookingStatus, DiningRestaurant, TableSlot

_RESTAURANTS = (
    DiningRestaurant("d1", "Toscano", "Indiranagar", "₹1800 for two"),
    DiningRestaurant("d2", "Olive Bistro", "Indiranagar", "₹2200 for two"),
    DiningRestaurant("d3", "Trattoria Roma", "Indiranagar", "₹3000 for two"),
)
_TIMES = (
    ("12:30", "12:30 PM"),
    ("19:00", "07:00 PM"),
    ("19:30", "07:30 PM"),
    ("20:00", "08:00 PM"),
)


class MockDineout:
    def __init__(self) -> None:
        self.restaurants = list(_RESTAURANTS)
        self.times = list(_TIMES)
        self.bookings: dict[str, BookingStatus] = {}
        self.book_calls = 0
        self.fail_next_book: Exception | None = None
        self.fail_status = False

    async def search(self, query: str, latitude: float, longitude: float) -> list[DiningRestaurant]:
        del query, latitude, longitude
        return list(self.restaurants)

    async def free_slots(
        self, restaurant_id: str, date: str, latitude: float, longitude: float
    ) -> list[TableSlot]:
        del latitude, longitude
        slots: list[TableSlot] = []
        for time, display in self.times:
            moment = datetime.fromisoformat(f"{date}T{time}").replace(
                tzinfo=ZoneInfo("Asia/Kolkata")
            )
            slots.append(
                TableSlot(date, time, display, int(moment.timestamp()), 1, f"{restaurant_id}-1")
            )
        return slots

    async def book(
        self, restaurant_id: str, slot: TableSlot, guests: int, latitude: float, longitude: float
    ) -> BookingResult:
        del restaurant_id, guests, latitude, longitude
        self.book_calls += 1
        if self.fail_next_book is not None:
            error, self.fail_next_book = self.fail_next_book, None
            raise error
        if (slot.time, slot.display_time) not in self.times:
            raise CommerceRejected("That slot is gone", kind=RejectionKind.OUT_OF_STOCK)
        order_id = f"DN{self.book_calls:06d}"
        self.bookings[order_id] = BookingStatus("CONFIRMED")
        return BookingResult(order_id, "CONFIRMED")

    async def status(self, order_id: str) -> BookingStatus:
        if self.fail_status:
            raise RuntimeError("status unavailable")
        return self.bookings[order_id]
