"""Generative UI contract (docs/genui-spec.md). No network."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.assistant.view import WorkflowView
from app.dining.base import DiningRestaurant, TableSlot
from app.dining.state_machine import (
    DiningSession,
    OptionChosen,
    RestaurantsFound,
    SlotsFound,
)
from app.dining.state_machine import (
    transition as dining_transition,
)
from app.genui.actions import ACTION_IDS, is_known_action
from app.genui.builders import (
    CalendarInput,
    NoteInput,
    ReminderInput,
    TaskInput,
    booking_confirmation_widget,
    cart_widget,
    confirmation_widget,
    new_note_form,
    new_reminder_form,
    note_list_widget,
    reminder_list_widget,
    share_panel_widget,
    status_widget,
    task_board_widget,
)
from app.genui.sanitize import clean_text
from app.genui.validate import GenUIError, to_wire, validate_envelope, validate_widget
from app.orchestrator.state_machine import CartEntry, Session

ACT = {"label": "Go", "action_id": "order.confirm"}
NOW = "2026-11-09T10:00:00+05:30"

EXAMPLES: dict[str, dict[str, Any]] = {
    "heading": {"type": "heading", "id": "h", "text": "Hi", "level": 2},
    "text": {"type": "text", "id": "t", "text": "Hello", "tone": "muted"},
    "card": {
        "type": "card",
        "id": "c",
        "title": "T",
        "body": "b",
        "children": [{"type": "text", "id": "t1", "text": "x", "tone": "normal"}],
        "actions": [ACT],
    },
    "list": {
        "type": "list",
        "id": "l",
        "title": "L",
        "items": [{"id": "i1", "title": "A", "detail": "d", "badge": "b"}],
        "empty_text": "none",
    },
    "action_panel": {"type": "action_panel", "id": "ap", "title": "T", "actions": [ACT]},
    "form": {
        "type": "form",
        "id": "f",
        "title": "F",
        "fields": [
            {"name": "a", "label": "A", "kind": "text", "required": True, "value": "v"},
            {
                "name": "s",
                "label": "S",
                "kind": "select",
                "options": [{"value": "1", "label": "One"}],
            },
            {"name": "n", "label": "N", "kind": "number", "value": 3, "min": 0, "max": 9},
            {"name": "c", "label": "C", "kind": "checkbox", "value": True},
        ],
        "submit": {"label": "Save", "action_id": "note.create", "style": "primary"},
    },
    "task_board": {
        "type": "task_board",
        "id": "tb",
        "title": "T",
        "tasks": [
            {
                "id": "k1",
                "title": "Job",
                "status": "failed",
                "detail": "d",
                "retry_action_id": "task.retry:k1",
                "cancel_action_id": "task.cancel:k1",
            }
        ],
    },
    "progress": {"type": "progress", "id": "p", "label": "L", "value": 40.5},
    "cart": {
        "type": "cart",
        "id": "cart",
        "title": "Cart",
        "lines": [
            {"id": "line0", "name": "Rice", "quantity": 2, "unit_price_inr": "78.00"},
        ],
        "total_inr": "156.00",
        "fees_inr": None,
        "editable": True,
        "demo": False,
    },
    "table": {
        "type": "table",
        "id": "tbl",
        "title": "T",
        "columns": ["a", "b"],
        "rows": [["1", "2"]],
    },
    "confirmation": {
        "type": "confirmation",
        "id": "cf",
        "title": "T",
        "details": [{"label": "a", "value": "b"}],
        "consequence": "c",
        "confirm": {"label": "Yes", "action_id": "order.confirm"},
        "reject": {"label": "No", "action_id": "order.reject"},
        "demo": True,
    },
    "composer": {
        "type": "composer",
        "id": "cp",
        "channel": "email",
        "to": "a",
        "subject": "s",
        "body": "b",
        "send": ACT,
        "copy_enabled": True,
    },
    "calendar_view": {
        "type": "calendar_view",
        "id": "cv",
        "title": "T",
        "days": [
            {
                "date": "2026-11-09",
                "events": [{"id": "e1", "title": "E", "start": "09:00", "end": "10:00"}],
            }
        ],
    },
    "reminder_list": {
        "type": "reminder_list",
        "id": "rl",
        "title": "T",
        "items": [{"id": "r1", "text": "x", "due": NOW, "done": False}],
    },
    "note_list": {
        "type": "note_list",
        "id": "nl",
        "title": "T",
        "items": [{"id": "n1", "body": "x", "updated": NOW}],
    },
    "share_panel": {
        "type": "share_panel",
        "id": "sp",
        "title": "T",
        "summary": "s",
        "calendar": {"title": "m", "start": NOW, "end": NOW, "location": "l"},
    },
    "status": {"type": "status", "id": "st", "kind": "error", "message": "m", "retry": ACT},
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_component_accepts_example_and_round_trips(name: str) -> None:
    widget = validate_widget(EXAMPLES[name])
    wire = to_wire([widget])[0]
    assert wire["type"] == name
    assert validate_widget(wire) == widget
    assert to_wire([validate_widget(wire)]) == [wire]


def _bad(**over: Any) -> dict[str, Any]:
    return {**EXAMPLES["text"], **over}


def _cart(**over: Any) -> dict[str, Any]:
    data = deepcopy(EXAMPLES["cart"])
    data.update(over)
    return data


def _nested(depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "id": "leaf", "text": "x", "tone": "normal"}
    for i in range(depth - 1):
        node = {"type": "card", "id": f"c{i}", "title": "T", "children": [node]}
    return node


def test_limits() -> None:
    validate_widget(_nested(3))
    with pytest.raises(GenUIError):
        validate_widget(_nested(4))
    kids = [{"type": "text", "id": f"t{i}", "text": "x", "tone": "normal"} for i in range(41)]
    with pytest.raises(GenUIError):
        validate_widget({"type": "card", "id": "c", "title": "T", "children": kids})
    validate_widget({"type": "card", "id": "c", "title": "T", "children": kids[:40]})
    validate_widget(_bad(text="x" * 2000))
    with pytest.raises(GenUIError):
        validate_widget(_bad(text="x" * 2001))
    envelope = [_bad(id=f"t{i}") for i in range(13)]
    validate_envelope(envelope[:12])
    with pytest.raises(GenUIError):
        validate_envelope(envelope)


@pytest.mark.parametrize("bad_id", ["", "Upper", "has space", "a" * 65, "x/y"])
def test_bad_id_rejected(bad_id: str) -> None:
    with pytest.raises(GenUIError):
        validate_widget(_bad(id=bad_id))


@pytest.mark.parametrize("money", ["78", "7e1", "-1.00", "78.0", "1,000.00", 78.0, 78, None])
def test_bad_money_rejected(money: Any) -> None:
    with pytest.raises(GenUIError):
        validate_widget(_cart(total_inr=money))


def test_cart_quantity_and_iso() -> None:
    for q in (100, -1, 1.5, True):
        cart = _cart()
        cart["lines"][0]["quantity"] = q
        with pytest.raises(GenUIError):
            validate_widget(cart)
    cart = _cart()
    cart["lines"][0]["quantity"] = 0
    validate_widget(cart)
    bad_rem = deepcopy(EXAMPLES["reminder_list"])
    bad_rem["items"][0]["due"] = "tomorrow-ish"
    with pytest.raises(GenUIError):
        validate_widget(bad_rem)
    bad_share = deepcopy(EXAMPLES["share_panel"])
    bad_share["calendar"]["start"] = "2026-11-09T10:00:00"  # no offset
    with pytest.raises(GenUIError):
        validate_widget(bad_share)
    bad_day = deepcopy(EXAMPLES["calendar_view"])
    bad_day["days"][0]["date"] = "2026-13-45"
    with pytest.raises(GenUIError):
        validate_widget(bad_day)


def test_unknown_type_and_extra_property_rejected() -> None:
    with pytest.raises(GenUIError):
        validate_widget({"type": "iframe", "id": "x"})
    with pytest.raises(GenUIError):
        validate_widget(_bad(style="color:red"))
    with pytest.raises(GenUIError):
        validate_widget({**EXAMPLES["status"], "retry": {**ACT, "onclick": "x"}})
    with pytest.raises(GenUIError):
        validate_widget("nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "a <b>bold</b>",
        "x </div>",
        "JavaScript:alert(1)",
        "java\nscript:x",
    ],
)
def test_markup_rejected_but_plain_less_than_ok(text: str) -> None:
    with pytest.raises(GenUIError):
        validate_widget(_bad(text=text))
    validate_widget(_bad(text="2 < 3 and 5 > 4"))


def test_action_ids() -> None:
    def panel(action_id: str) -> dict[str, Any]:
        return {**EXAMPLES["action_panel"], "actions": [{"label": "x", "action_id": action_id}]}

    validate_widget(panel("reminder.done:ab12"))
    for bad in (
        "reminder.done:",
        "evil.do",
        "reminder.done:AB",
        "reminder.done:a:b",
        "order.confirm ",
    ):
        with pytest.raises(GenUIError):
            validate_widget(panel(bad))
    board = deepcopy(EXAMPLES["task_board"])
    board["tasks"][0]["retry_action_id"] = "nope"
    with pytest.raises(GenUIError):
        validate_widget(board)
    assert is_known_action("cart.set_quantity")
    assert not is_known_action("")
    assert len(ACTION_IDS) == 12


def test_duplicate_ids_in_envelope_rejected() -> None:
    with pytest.raises(GenUIError):
        validate_envelope([_bad(id="same"), _bad(id="same")])


def test_failed_widget_gives_no_partial_output() -> None:
    with pytest.raises(GenUIError):
        validate_envelope([_bad(id="ok"), _bad(id="bad", text="<img src=x>")])


def test_select_needs_options() -> None:
    form = deepcopy(EXAMPLES["form"])
    form["fields"][1].pop("options")
    with pytest.raises(GenUIError):
        validate_widget(form)


# ----------------------------------------------------------------- builders
def _session() -> Session:
    return Session(
        cart=(
            CartEntry("s1", "k1", "Sona Masoori Rice", "5 kg", 2, Decimal("78")),
            CartEntry("s2", "k2", "Milk <b>x</b>", "500 ml", 1, 31),
        ),
        total_inr=Decimal("187"),
        fees_inr=Decimal("15.50"),
        address_label="Home",
        delivery_address="12 MG Road",
    )


def test_cart_widget_uses_the_carts_own_amounts() -> None:
    session = _session()
    wire = to_wire([cart_widget(session, demo=True, editable=True)])[0]
    assert wire["total_inr"] == "187.00"
    assert wire["fees_inr"] == "15.50"
    assert [ln["unit_price_inr"] for ln in wire["lines"]] == ["78.00", "31.00"]
    assert "<" not in wire["lines"][1]["name"]
    assert wire["demo"] is True and wire["editable"] is True


def test_cart_widget_from_workflow_view() -> None:
    session = _session()
    view = WorkflowView(lines=session.cart, total_inr=session.total_inr)
    wire = to_wire([cart_widget(view, demo=False, editable=False)])[0]
    assert wire["total_inr"] == "187.00"
    assert "fees_inr" not in wire
    assert len(wire["lines"]) == 2


def test_confirmation_consequence_matches_demo_flag() -> None:
    demo = to_wire([confirmation_widget(_session(), demo=True)])[0]
    real = to_wire([confirmation_widget(_session(), demo=False)])[0]
    assert "demo" in demo["consequence"].lower() and "no real order" in demo["consequence"].lower()
    assert "real order will be placed" in real["consequence"].lower()
    assert "demo" not in real["consequence"].lower()
    assert (demo["confirm"]["action_id"], demo["reject"]["action_id"]) == (
        "order.confirm",
        "order.reject",
    )
    labels = {d["label"]: d["value"] for d in demo["details"]}
    assert labels["Total"] == "Rs. 187.00"
    assert labels["Deliver to"] == "Home 12 MG Road"


def _dining_readback() -> DiningSession:
    restaurant = DiningRestaurant("d1", "Toscano", "Indiranagar")
    slot = TableSlot("2026-11-10", "19:00", "07:00 PM", 1794000000, 1, "176866-47409")
    session = DiningSession(
        query="italian", area="indiranagar", date="2026-11-10", time="19:00", guests=2
    )
    session, _ = dining_transition(session, RestaurantsFound((restaurant,)))
    session, _ = dining_transition(session, OptionChosen(0))
    session, _ = dining_transition(session, SlotsFound((slot,)))
    return session


def test_booking_confirmation_widget() -> None:
    session = _dining_readback()
    demo = to_wire([booking_confirmation_widget(session, demo=True)])[0]
    real = to_wire([booking_confirmation_widget(session, demo=False)])[0]
    values = {d["label"]: d["value"] for d in demo["details"]}
    assert values["Restaurant"] == "Toscano"
    assert values["Time"] == "07:00 PM" and values["Guests"] == "2"
    assert "demo" in demo["consequence"].lower()
    assert "real" in real["consequence"].lower()
    assert real["confirm"]["action_id"] == "order.confirm"


def test_other_builders_produce_valid_widgets() -> None:
    ist = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    widgets = [
        share_panel_widget("Dinner at 7", CalendarInput("Dinner", ist, ist, location="Toscano")),
        share_panel_widget("no calendar"),
        task_board_widget(
            [TaskInput("ab12", "Send", "failed", "oops", retriable=True, cancellable=True)]
        ),
        reminder_list_widget([ReminderInput("ab12", "Call mom", ist), ReminderInput("c3", "x")]),
        note_list_widget([NoteInput("n1", "buy milk", ist)]),
        status_widget("error", "Something broke", "task.retry:ab12"),
        status_widget("empty", "Nothing here"),
        new_reminder_form(),
        new_note_form(),
    ]
    wires = to_wire(widgets[:1] + widgets[2:6] + widgets[7:])  # distinct ids
    assert validate_envelope(wires)
    board = wires[1]
    assert board["tasks"][0]["retry_action_id"] == "task.retry:ab12"
    form = next(w for w in wires if w["id"] == "form.reminder")
    assert form["submit"]["action_id"] == "reminder.create"
    text_field = form["fields"][0]
    assert (
        text_field["name"] == "text" and text_field["required"] and text_field["max_length"] == 500
    )
    note = next(w for w in wires if w["id"] == "form.note")
    assert note["fields"][0]["kind"] == "textarea" and note["submit"]["action_id"] == "note.create"
    with pytest.raises(GenUIError):  # an id that cannot be an action suffix fails closed
        task_board_widget([TaskInput("Bad Id", "x", "failed", retriable=True)])


def test_sanitize() -> None:
    assert clean_text("  a\x00b\t\n  c​ ", 50) == "ab c"
    assert clean_text("x" * 10, 5) == "xxxx…"
    assert len(clean_text("y" * 100, 20)) == 20
    assert "<s" not in clean_text("<<script>", 100)
    assert "javascript:" not in clean_text("javajavascript:script:x", 100).lower()
    assert clean_text("anything", 0) == ""
    validate_widget(_bad(text=clean_text("<b>hi</b> javascript:x", 100)))
