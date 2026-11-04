"""Swiggy Dineout: free table reservations only.

Contracts: https://mcp.swiggy.com/builders/docs/build/recipes/book-a-table.md and
reference/dineout/{search_restaurants_dineout,get_available_slots,book_table,
get_booking_status}.md.

Live, search and slots answer in text with empty structured content, unlike the docs:
restaurant IDs are read from search text and the cards from render_restaurants_dineout,
and slots are parsed strictly from text. See tests/fixtures/dineout_slots_live.txt. If the
format changes the parser finds nothing, so nothing can be booked. Paid prebook deals need
UPI and are never offered.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from app.commerce.base import CommerceError, CommerceNotAttempted
from app.commerce.swiggy import (
    SWIGGY_MCP_URL,
    SWIGGY_STAGING_MCP_URL,
    SwiggyMcpClient,
    _list,  # pyright: ignore[reportPrivateUsage]
    _optional_str,  # pyright: ignore[reportPrivateUsage]
    food_or_dining_url,
)
from app.dining.base import BookingResult, BookingStatus, DiningRestaurant, TableSlot

SWIGGY_DINEOUT_URL = food_or_dining_url(SWIGGY_MCP_URL, "dineout")
SWIGGY_STAGING_DINEOUT_URL = food_or_dining_url(SWIGGY_STAGING_MCP_URL, "dineout")

_FREE_DEAL = re.compile(r'^\s*(\d{4}-\d{2}-\d{2}) \[FREE\]: slotId=(\d+), itemId="([^"]+)"', re.M)
_DAY_SECTION = re.compile(r"^Slots for (\d{4}-\d{2}-\d{2}):\n((?:[ \t]+[^\n]+\n?)+)", re.M)
_TIME_ENTRY = re.compile(r"(\d{1,2}:\d{2} [AP]M)→(\d+)")
_RESTAURANT_ID = re.compile(r"\(ID: (\d+)\)")
_ORDER_ID = re.compile(r"order\s*(?:id)?\s*[:=#]?\s*([A-Za-z0-9-]{5,})", re.I)


def parse_free_slots(text: str, date: str) -> list[TableSlot]:
    """Free slots for one date from live slot text; empty when no free deal is listed."""
    deals = {match[0]: (int(match[1]), match[2]) for match in _FREE_DEAL.findall(text)}
    deal = deals.get(date)
    if deal is None:
        return []
    slots: list[TableSlot] = []
    for day, body in _DAY_SECTION.findall(text):
        if day != date:
            continue
        for display, epoch in _TIME_ENTRY.findall(body):
            parsed = datetime.strptime(display, "%I:%M %p").strftime("%H:%M")
            slots.append(TableSlot(date, parsed, display, int(epoch), deal[0], deal[1]))
    return slots


class SwiggyDineout(SwiggyMcpClient):
    allowed_endpoints = frozenset({SWIGGY_DINEOUT_URL, SWIGGY_STAGING_DINEOUT_URL})
    read_tools = frozenset(
        {
            "search_restaurants_dineout",
            "render_restaurants_dineout",
            "get_available_slots",
            "get_booking_status",
        }
    )
    write_tools = frozenset({"book_table"})

    async def search(self, query: str, latitude: float, longitude: float) -> list[DiningRestaurant]:
        location = {"latitude": latitude, "longitude": longitude}
        _, _, text = await self._invoke(
            "search_restaurants_dineout", {"query": query, "limit": 5, **location}
        )
        ids = _RESTAURANT_ID.findall(text)
        if not ids:
            return []
        data, _, _ = await self._invoke(
            "render_restaurants_dineout",
            {"restaurantIds": ids, "searches": [{"query": query, **location}]},
        )
        return [
            DiningRestaurant(
                id=str(raw.get("id")),
                name=str(raw.get("name")),
                area=str(raw.get("locality") or raw.get("area") or ""),
                cost_for_two=str(raw.get("costForTwo") or ""),
            )
            for raw in _list(data.get("restaurants"), "restaurants", allow_missing=True)
            if raw.get("id") and raw.get("name")
        ]

    async def free_slots(
        self, restaurant_id: str, date: str, latitude: float, longitude: float
    ) -> list[TableSlot]:
        _, _, text = await self._invoke(
            "get_available_slots",
            {
                "restaurantId": restaurant_id,
                "date": date,
                "latitude": latitude,
                "longitude": longitude,
            },
        )
        return parse_free_slots(text, date)

    async def book(
        self, restaurant_id: str, slot: TableSlot, guests: int, latitude: float, longitude: float
    ) -> BookingResult:
        if not self.allow_real_checkout:
            raise CommerceNotAttempted("Real table booking is disabled by configuration")
        data, _, text = await self._invoke(
            "book_table",
            {
                "restaurantId": restaurant_id,
                "slotId": slot.slot_id,
                "itemId": slot.item_id,
                "reservationTime": slot.reservation_time,
                "guestCount": guests,
                "latitude": latitude,
                "longitude": longitude,
            },
        )
        order_id = _optional_str(data.get("orderId"))
        if order_id is None:
            match = _ORDER_ID.search(text)
            order_id = match.group(1) if match else None
        if order_id is None:
            raise CommerceError("Swiggy did not return a booking ID")
        status = _optional_str(data.get("status")) or ""
        if status.upper() == "PENDING_PAYMENT":
            raise CommerceError("A free reservation unexpectedly needs payment")
        return BookingResult(order_id, status)

    async def status(self, order_id: str) -> BookingStatus:
        data, _, text = await self._invoke("get_booking_status", {"orderId": order_id})
        nested = data.get("data")
        source = cast(Mapping[str, object], nested) if isinstance(nested, Mapping) else data
        status = _optional_str(source.get("status"))
        if status is None:
            match = re.search(r"status[:\s]+([A-Za-z_ ]+)", text, re.I)
            status = match.group(1).strip() if match else None
        if status is None:
            raise CommerceError("Swiggy did not return a booking status")
        return BookingStatus(status)
