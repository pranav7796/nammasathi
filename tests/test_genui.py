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
    DayEvent,
    DayInput,
    EmailItem,
    EventConflict,
    EventFields,
    MemoryItem,
    NoteInput,
    PlannerItem,
    ReminderInput,
    TaskInput,
    booking_confirmation_widget,
    calendar_view_widget,
    cart_widget,
    composer_widget,
    confirmation_widget,
    day_planner_widget,
    email_list_widget,
    event_preview_widget,
    memory_list_widget,
    new_note_form,
    new_reminder_form,
    note_list_widget,
    reminder_list_widget,
    share_panel_widget,
    status_widget,
    task_board_widget,
)
from app.genui.sanitize import clean_multiline, clean_text
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
        "state": "draft",
        "demo": False,
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
    "email_list": {
        "type": "email_list",
        "id": "em",
        "title": "Emails",
        "items": [
            {
                "id": "18c3F_a-1",
                "sender": "Asha",
                "subject": "Dinner?",
                "snippet": "Are you free",
                "received": NOW,
                "unread": True,
                "important": False,
                "needs_reply": True,
            }
        ],
        "empty_text": "none",
        "demo": True,
    },
    "event_preview": {
        "type": "event_preview",
        "id": "ev",
        "title": "New event",
        "fields": {
            "title": "Dinner",
            "date": "2026-11-09",
            "start": "19:30",
            "minutes": 90,
            "attendees": ["asha@example.com"],
        },
        "conflicts": [{"title": "Standup", "start": "19:00", "end": "20:00"}],
        "state": "draft",
        "detail": "d",
        "approve": {"label": "Approve", "action_id": "task.approve:t1", "style": "primary"},
        "cancel": {"label": "Cancel", "action_id": "task.cancel:t1"},
        "demo": False,
        "editable": True,
    },
    "day_planner": {
        "type": "day_planner",
        "id": "dp",
        "title": "Your day",
        "date": "2026-11-09",
        "items": [
            {
                "id": "p1",
                "kind": "event",
                "title": "Standup",
                "time": "10:00",
                "done": False,
                "priority": "high",
                "detail": "d",
            },
            {"id": "p2", "kind": "task", "title": "Pay", "done": True, "priority": "low"},
        ],
        "demo": False,
        "editable": True,
    },
    "memory_list": {
        "type": "memory_list",
        "id": "mem",
        "title": "What I remember",
        "items": [{"key": "diet", "label": "Diet", "value": "vegetarian"}],
        "empty_text": "none",
        "consent_text": "Kept to personalise answers.",
    },
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
    assert len(ACTION_IDS) == 24


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
            [TaskInput("ab12", "Send", "failed", "oops", retriable=True, cancellable=True)],
            demo=False,
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
        task_board_widget([TaskInput("Bad Id", "x", "failed", retriable=True)], demo=False)


def test_sanitize() -> None:
    assert clean_text("  a\x00b\t\n  c​ ", 50) == "ab c"
    assert clean_text("x" * 10, 5) == "xxxx…"
    assert len(clean_text("y" * 100, 20)) == 20
    assert "<s" not in clean_text("<<script>", 100)
    assert "javascript:" not in clean_text("javajavascript:script:x", 100).lower()
    assert clean_text("anything", 0) == ""
    assert "javascript" not in clean_text("java\nscript:x j a v a s c r i p t :y", 100).lower()
    validate_widget(_bad(text=clean_text("<b>hi</b> javascript:x", 100)))


# ------------------------------------------- addendum: Gmail, Calendar, planner, memory
def _ex(name: str, **over: Any) -> dict[str, Any]:
    data = deepcopy(EXAMPLES[name])
    data.update(over)
    return data


def _rejects(data: dict[str, Any]) -> None:
    with pytest.raises(GenUIError):
        validate_widget(data)


@pytest.mark.parametrize("name", ["email_list", "event_preview", "day_planner", "composer"])
def test_demo_is_required_on_the_addendum_widgets(name: str) -> None:
    data = _ex(name)
    data.pop("demo")
    _rejects(data)
    _rejects(_ex(name, demo="yes"))
    _rejects(_ex(name, demo=1))


def test_old_task_board_and_calendar_still_valid_without_new_fields() -> None:
    board = validate_widget(
        {
            "type": "task_board",
            "id": "tb",
            "title": "T",
            "tasks": [{"id": "a", "title": "x", "status": "pending"}],
        }
    )
    assert to_wire([board])[0]["demo"] is False
    cal = deepcopy(EXAMPLES["calendar_view"])
    assert "demo" not in cal
    validate_widget(cal)
    validate_widget({**cal, "demo": True})
    _rejects({**cal, "demo": "no"})


def test_task_board_extensions() -> None:
    board = deepcopy(EXAMPLES["task_board"])
    board["demo"] = True
    board["summary"] = "2 of 3 done"
    board["tasks"][0].update(
        approve_action_id="task.approve:k1",
        view_action_id="task.view:k1",
        edit_action_id="task.edit:k1",
    )
    wire = to_wire([validate_widget(board)])[0]
    assert wire["tasks"][0]["view_action_id"] == "task.view:k1" and wire["summary"] == "2 of 3 done"
    for key in ("approve_action_id", "view_action_id", "edit_action_id"):
        bad = deepcopy(board)
        bad["tasks"][0][key] = "evil.go:k1"
        _rejects(bad)
    _rejects({**board, "demo": "x"})
    _rejects({**board, "summary": "<b>x</b>"})


def test_composer_states_and_fields() -> None:
    for state in ("draft", "sent", "copied", "failed"):
        validate_widget(_ex("composer", state=state, detail="delivered", editable=False))
    _rejects(_ex("composer", state="queued"))
    bad = _ex("composer")
    bad.pop("state")
    _rejects(bad)
    _rejects(_ex("composer", detail="<script>x</script>"))
    _rejects(_ex("composer", editable="yes"))


def test_email_list_limits_and_ids() -> None:
    row = EXAMPLES["email_list"]["items"][0]

    def rows(n: int) -> list[dict[str, Any]]:
        return [{**row, "id": f"m{i}"} for i in range(n)]

    validate_widget(_ex("email_list", items=rows(10)))
    _rejects(_ex("email_list", items=rows(11)))
    validate_widget(_ex("email_list", items=[]))
    for bad_id in ("", "a b", "a:b", "a/b", "x" * 101):
        _rejects(_ex("email_list", items=[{**row, "id": bad_id}]))
    validate_widget(_ex("email_list", items=[{**row, "id": "X" * 100}]))
    _rejects(_ex("email_list", items=[row, row]))  # duplicate id
    _rejects(_ex("email_list", items=[{**row, "received": "yesterday"}]))
    _rejects(_ex("email_list", items=[{**row, "unread": "yes"}]))
    missing = {k: v for k, v in row.items() if k != "needs_reply"}
    _rejects(_ex("email_list", items=[missing]))
    _rejects(_ex("email_list", items=[{**row, "subject": "<script>alert(1)</script>"}]))
    _rejects(_ex("email_list", items=[{**row, "snippet": "javascript:alert(1)"}]))
    _rejects(_ex("email_list", items=[{**row, "extra": 1}]))


def test_event_preview_validation() -> None:
    def fields(**over: Any) -> dict[str, Any]:
        return {**EXAMPLES["event_preview"]["fields"], **over}

    validate_widget(
        _ex("event_preview", fields=fields(attendees=[f"a{i}@x.io" for i in range(10)]))
    )
    _rejects(_ex("event_preview", fields=fields(attendees=[f"a{i}@x.io" for i in range(11)])))
    conflict = {"title": "Busy", "start": "10:00", "end": "11:00"}
    validate_widget(_ex("event_preview", conflicts=[conflict] * 10))
    _rejects(_ex("event_preview", conflicts=[conflict] * 11))
    for bad in (
        fields(date="2026-13-45"),
        fields(date="09-11-2026"),
        fields(start="25:00"),
        fields(start="9:30"),
        fields(minutes=0),
        fields(minutes=1441),
        fields(minutes=1.5),
        fields(minutes=True),
        fields(title=""),
        fields(title="<b>x</b>"),
        fields(attendees=["javascript:alert(1)"]),
        {**fields(), "location": "x"},
    ):
        _rejects(_ex("event_preview", fields=bad))
    _rejects(_ex("event_preview", conflicts=[{**conflict, "end": "noon"}]))
    for state in ("draft", "created", "failed", "cancelled"):
        validate_widget(_ex("event_preview", state=state, editable=False))
    _rejects(_ex("event_preview", state="sent"))
    _rejects(_ex("event_preview", approve={"label": "Go", "action_id": "evil.go"}))
    _rejects(_ex("event_preview", editable="yes"))


def test_day_planner_validation() -> None:
    item = {"id": "p", "kind": "task", "title": "x", "done": False, "priority": "normal"}

    def items(n: int) -> list[dict[str, Any]]:
        return [{**item, "id": f"p{i}"} for i in range(n)]

    validate_widget(_ex("day_planner", items=items(40)))
    _rejects(_ex("day_planner", items=items(41)))
    for bad in (
        {**item, "kind": "meeting"},
        {**item, "priority": "urgent"},
        {**item, "time": "7pm"},
        {**item, "time": "24:00"},
        {**item, "id": "Has Space"},
        {**item, "id": "a:b"},
        {**item, "id": "x" * 41},
        {**item, "title": "<img src=x>"},
        {**item, "done": "no"},
    ):
        _rejects(_ex("day_planner", items=[bad]))
    _rejects(_ex("day_planner", items=[item, item]))
    _rejects(_ex("day_planner", date="2026-02-30"))
    _rejects(_ex("day_planner", date="tomorrow"))
    _rejects(_ex("day_planner", editable="yes"))
    validate_widget(_ex("day_planner", items=[{**item, "time": "00:00"}]))


def test_memory_list_validation() -> None:
    row = {"key": "diet", "label": "Diet", "value": "veg"}

    def rows(n: int) -> list[dict[str, Any]]:
        return [{**row, "key": f"k{i}"} for i in range(n)]

    validate_widget(_ex("memory_list", items=rows(30)))
    _rejects(_ex("memory_list", items=rows(31)))
    _rejects(_ex("memory_list", items=[row, row]))
    for bad in (
        {**row, "key": "Diet"},
        {**row, "key": "a:b"},
        {**row, "key": ""},
        {**row, "value": "<b>"},
    ):
        _rejects(_ex("memory_list", items=[bad]))
    bad_consent = _ex("memory_list")
    bad_consent.pop("consent_text")
    _rejects(bad_consent)
    _rejects(_ex("memory_list", consent_text=""))
    _rejects(_ex("memory_list", demo=True))  # memory_list has no demo property


def test_addendum_action_ids() -> None:
    for base in (
        "task.approve",
        "task.edit",
        "task.view",
        "email.open",
        "email.reply",
        "plan.toggle",
        "plan.priority",
        "plan.move",
        "plan.add",
        "memory.forget",
        "memory.save",
        "demo.next",
    ):
        assert base in ACTION_IDS and is_known_action(base)
    assert is_known_action("task.approve:t1")
    assert is_known_action("plan.move:p-1")
    assert is_known_action("email.open:18c3F_a-1")  # mail ids keep their case
    assert is_known_action("email.reply:" + "A" * 100)
    assert not is_known_action("email.open:" + "A" * 101)
    assert not is_known_action("email.open:a:b")
    assert not is_known_action("task.approve:T1")  # other ids stay lowercase
    assert not is_known_action("plan.toggle:" + "a" * 41)
    assert not is_known_action("memory.forget:")


# ------------------------------------------------------------ addendum builders
HOSTILE = "<script>alert(1)</script>"
JS = "javascript:alert(1)"
RECEIVED = datetime(2026, 11, 9, 8, 30, tzinfo=UTC)


def test_email_list_widget_sanitises_hostile_strings() -> None:
    items = [
        EmailItem(
            "18c3F",
            f"Mallory {HOSTILE}",
            f"Win {JS} now",
            f"<b>click</b> {JS}\nsecond line",
            RECEIVED,
            unread=True,
            needs_reply=True,
        ),
        EmailItem("m2", "Asha", "Dinner", "Free tonight?", RECEIVED, important=True),
    ]
    wire = to_wire([email_list_widget(items, demo=True)])[0]
    assert wire["id"] == "emails" and wire["demo"] is True
    first = wire["items"][0]
    for key in ("sender", "subject", "snippet"):
        assert "<script" not in first[key] and "javascript:" not in first[key].lower()
        assert "<b" not in first[key]
    assert first["unread"] is True and first["needs_reply"] is True and first["important"] is False
    assert first["received"] == RECEIVED.isoformat()
    assert wire["items"][1]["important"] is True


def test_email_list_widget_fails_closed() -> None:
    with pytest.raises(GenUIError):
        email_list_widget([EmailItem("bad id", "a", "b", "c", RECEIVED)], demo=False)
    with pytest.raises(GenUIError):
        email_list_widget([EmailItem("x" * 101, "a", "b", "c", RECEIVED)], demo=False)
    many = [EmailItem(f"m{i}", "a", "b", "c", RECEIVED) for i in range(11)]
    with pytest.raises(GenUIError):
        email_list_widget(many, demo=False)
    assert to_wire([email_list_widget([], demo=False)])[0]["items"] == []
    with pytest.raises(TypeError):  # demo cannot be omitted
        email_list_widget([])  # type: ignore[call-arg]


def test_event_preview_widget() -> None:
    fields = EventFields(
        f"Dinner {HOSTILE}", "2026-11-09", "19:30", 90, ("asha@example.com", JS, "")
    )
    conflicts = [EventConflict(f"Standup {JS}", "19:00", "20:00")]
    wire = to_wire(
        [
            event_preview_widget(
                "t1",
                fields=fields,
                conflicts=conflicts,
                state="draft",
                detail=None,
                demo=True,
                editable=True,
            )
        ]
    )[0]
    assert wire["id"] == "event.t1"
    assert wire["approve"]["action_id"] == "task.approve:t1"
    assert wire["cancel"]["action_id"] == "task.cancel:t1"
    assert "<script" not in wire["fields"]["title"]
    assert all("javascript:" not in a.lower() for a in wire["fields"]["attendees"])
    assert "" not in wire["fields"]["attendees"]
    assert wire["conflicts"][0]["start"] == "19:00"
    assert wire["demo"] is True and wire["editable"] is True and "detail" not in wire
    done = to_wire(
        [
            event_preview_widget(
                "t1", fields=fields, state="created", detail="Added to Google Calendar", demo=False
            )
        ]
    )[0]
    assert done["state"] == "created" and done["detail"] == "Added to Google Calendar"
    assert done["editable"] is False and done["conflicts"] == []
    with pytest.raises(GenUIError):
        event_preview_widget("Bad Id", fields=fields, state="draft", demo=True)
    with pytest.raises(GenUIError):
        event_preview_widget(
            "t1", fields=EventFields("x", "2026-13-01", "19:30", 30), state="draft", demo=True
        )
    with pytest.raises(GenUIError):
        event_preview_widget(
            "t1",
            fields=EventFields("x", "2026-11-09", "19:30", 30, ("a",) * 11),
            state="draft",
            demo=True,
        )


def test_day_planner_widget() -> None:
    items = [
        PlannerItem("p1", "event", f"Standup {HOSTILE}", "10:00", priority="high", detail=JS),
        PlannerItem("p2", "reminder", "Call mom", done=True),
    ]
    wire = to_wire([day_planner_widget(date="2026-11-09", items=items, demo=False, editable=True)])[
        0
    ]
    assert wire["id"] == "planner" and wire["date"] == "2026-11-09" and wire["editable"] is True
    assert "<script" not in wire["items"][0]["title"]
    assert "javascript:" not in wire["items"][0].get("detail", "").lower()
    assert wire["items"][0]["priority"] == "high" and "time" not in wire["items"][1]
    with pytest.raises(GenUIError):
        day_planner_widget(date="2026-11-09", items=[PlannerItem("A:b", "task", "x")], demo=False)
    with pytest.raises(GenUIError):
        day_planner_widget(
            date="2026-11-09",
            items=[PlannerItem(f"p{i}", "task", "x") for i in range(41)],
            demo=False,
        )
    with pytest.raises(GenUIError):
        day_planner_widget(date="tomorrow", items=[], demo=False)


def test_memory_list_widget() -> None:
    items = [MemoryItem("diet", "Diet", "vegetarian"), MemoryItem("home", f"Home {HOSTILE}", JS)]
    wire = to_wire([memory_list_widget(items, consent_text="Kept only to personalise answers.")])[0]
    assert wire["id"] == "memory" and wire["consent_text"].startswith("Kept")
    assert "<script" not in wire["items"][1]["label"]
    assert "javascript:" not in wire["items"][1]["value"].lower()
    with pytest.raises(GenUIError):
        memory_list_widget([MemoryItem("Bad Key", "x", "y")], consent_text="c")
    with pytest.raises(GenUIError):
        memory_list_widget([MemoryItem(f"k{i}", "x", "y") for i in range(31)], consent_text="c")


def test_calendar_view_widget() -> None:
    days = [
        DayInput(
            "2026-11-09",
            (
                DayEvent("e1", f"Standup {HOSTILE}", "10:00", "10:30"),
                DayEvent("e2", "Dinner", "19:30", "21:00", detail=JS),
            ),
        ),
        DayInput("2026-11-10"),
    ]
    wire = to_wire([calendar_view_widget(days, title="This week", demo=True)])[0]
    assert wire["id"] == "calendar" and wire["demo"] is True and wire["title"] == "This week"
    assert "<script" not in wire["days"][0]["events"][0]["title"]
    assert wire["days"][1]["events"] == []
    with pytest.raises(GenUIError):
        calendar_view_widget([DayInput("2026-11-09", (DayEvent("e", "x", "9", "10"),))], demo=False)


def test_composer_widget_states() -> None:
    kwargs: dict[str, Any] = {
        "channel": "email",
        "to": f"asha@example.com {HOSTILE}",
        "subject": f"Hi {JS}",
        "body": "Hi Asha,\n\n\n\nShall we meet?\n<b>x</b> javascript:y\nThanks",
        "demo": True,
        "editable": True,
    }
    wire = to_wire([composer_widget("t7", state="draft", **kwargs)])[0]
    assert wire["id"] == "composer.t7" and wire["send"]["action_id"] == "task.approve:t7"
    assert wire["state"] == "draft" and wire["demo"] is True and wire["editable"] is True
    assert "\n" in wire["body"] and "\n\n\n" not in wire["body"]
    assert "<script" not in wire["to"] and "javascript:" not in wire["subject"].lower()
    assert "<b" not in wire["body"] and "javascript:" not in wire["body"].lower()
    for state in ("sent", "copied", "failed"):
        w = to_wire([composer_widget("t7", state=state, detail="delivered", **kwargs)])[0]
        assert w["state"] == state and w["detail"] == "delivered"
    msg = to_wire(
        [composer_widget("t8", channel="message", body="Hello", state="draft", demo=False)]
    )[0]
    assert "to" not in msg and "subject" not in msg and msg["editable"] is False
    with pytest.raises(GenUIError):
        composer_widget("Bad Id", channel="message", body="x", state="draft", demo=False)
    with pytest.raises(TypeError):
        composer_widget("t", channel="message", body="x", state="draft")  # type: ignore[call-arg]


def test_task_board_builder_extensions() -> None:
    tasks = [
        TaskInput(
            "a1",
            "Email Asha",
            "waiting_for_approval",
            approvable=True,
            viewable=True,
            editable=True,
        ),
        TaskInput("a2", "Check", "running", cancellable=True),
    ]
    wire = to_wire([task_board_widget(tasks, demo=True, summary="1 of 2 waiting")])[0]
    row = wire["tasks"][0]
    assert row["approve_action_id"] == "task.approve:a1"
    assert row["view_action_id"] == "task.view:a1" and row["edit_action_id"] == "task.edit:a1"
    assert wire["demo"] is True and wire["summary"] == "1 of 2 waiting"
    assert "approve_action_id" not in wire["tasks"][1]
    flagged = (
        TaskInput("Bad Id", "x", "pending", approvable=True),
        TaskInput("Bad Id", "x", "pending", viewable=True),
        TaskInput("Bad Id", "x", "pending", editable=True),
    )
    for bad in flagged:
        with pytest.raises(GenUIError):
            task_board_widget([bad], demo=False)
    with pytest.raises(TypeError):
        task_board_widget(tasks)  # type: ignore[call-arg]


def test_clean_multiline() -> None:
    assert clean_multiline("a  b\r\n\r\n\r\n\r\nc\t\x00d", 100) == "a b\n\nc d"
    assert clean_multiline("java\nscript:alert(1)", 100).lower().count("javascript:") == 0
    assert len(clean_multiline("x\n" * 100, 20)) <= 20
    assert clean_multiline("anything", 0) == ""
    validate_widget(_bad(text=clean_multiline("<b>hi</b>\njava\nscript:x", 100)))
