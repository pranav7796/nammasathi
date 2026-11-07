"""The task engine's promises, each proven: concurrency, approval, no duplicates, recovery.

All against practice adapters and fakes. Nothing reaches Gmail, Google Calendar or WhatsApp.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.accounts.service import AccountService
from app.integrations.base import (
    Email,
    IntegrationError,
    NotConnected,
    OutgoingEmail,
    SentEmail,
)
from app.personal.dates import IST
from app.personal.service import PersonalService
from app.tasks import render
from app.tasks.demo import DemoCalendar, DemoEmail, DemoMessaging
from app.tasks.engine import TaskEngine, overlapping
from app.tasks.executors import DemoContacts, Services
from app.tasks.planner import Plan, PlannedTask
from app.tasks.store import TaskStore
from tests.test_accounts import site

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=IST)  # a Monday morning


class SlowEmail(DemoEmail):
    """Reads that take a moment, so overlap is measurable."""

    delay = 0.25

    async def important(self, user_id: str, limit: int) -> Any:
        await asyncio.sleep(self.delay)
        return await super().important(user_id, limit)


class SlowCalendar(DemoCalendar):
    async def events(self, user_id: str, start: datetime, end: datetime) -> Any:
        await asyncio.sleep(SlowEmail.delay)
        return await super().events(user_id, start, end)


class Harness:
    def __init__(self, service: AccountService) -> None:
        self.service = service
        self.email = DemoEmail(NOW)
        self.calendar = DemoCalendar(NOW)
        self.messaging = DemoMessaging()
        self.demo = {"gmail": True, "calendar": True, "whatsapp": True, "local": False}
        self.personal = PersonalService(service)
        self.engine = self.make_engine()

    def services(self) -> Services:
        return Services(
            email=self.email,
            calendar=self.calendar,
            messaging=self.messaging,
            contacts=DemoContacts(
                None, demo_email=self.demo["gmail"], demo_phone=self.demo["whatsapp"]
            ),
            personal=self.personal,
            demo=self.demo,
        )

    def make_engine(self) -> TaskEngine:
        async def provider(user_id: str) -> Services:
            del user_id
            return self.services()

        return TaskEngine(
            TaskStore(self.service.database),
            provider,
            run_blocking=self.service.run,
            clock=lambda: NOW,
        )

    def user(self, name: str) -> str:
        return self.service.store.create_user(f"{name}@example.com", "x").id

    async def run(self, user_id: str, *tasks: PlannedTask) -> Any:
        snapshot = await self.engine.submit(user_id, "test", "en-IN", Plan(tuple(tasks)))
        assert not isinstance(snapshot, str), snapshot
        await self.engine.settle()
        again = await self.engine.snapshot(user_id, snapshot.plan.id)
        assert again is not None
        return again


@asynccontextmanager
async def harness(tmp_path: Path) -> AsyncGenerator[Harness]:
    async with site(tmp_path) as s:
        yield Harness(s.service)


def send_mail(**extra: Any) -> PlannedTask:
    args: dict[str, Any] = {
        "to": ["rahul@example.com"],
        "subject": "Meeting",
        "body": "I'll attend.",
    }
    args.update(extra)
    return PlannedTask("gmail.send", args)


def status_of(snapshot: Any, kind: str) -> str:
    return next(t.status for t in snapshot.tasks if t.kind == kind)


# Concurrency and dependencies ----------------------------------------------------------------


async def test_independent_reads_really_run_at_the_same_time(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        h.email = SlowEmail(NOW)
        h.calendar = SlowCalendar(NOW)
        user = h.user("asha")
        started = time.monotonic()
        snapshot = await h.run(
            user, PlannedTask("gmail.important", {}), PlannedTask("calendar.today", {})
        )
        elapsed = time.monotonic() - started
        assert [t.status for t in snapshot.tasks] == ["completed", "completed"]
        assert elapsed < SlowEmail.delay * 1.8  # two 0.25 s reads finished in about one
        assert overlapping(snapshot.tasks) == 2
        board = (await render.widgets_for(snapshot, None))[0]
        assert "2 ran at the same time" in board.summary  # type: ignore[union-attr]


async def test_the_board_never_claims_concurrency_that_did_not_happen(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        availability = PlannedTask(
            "calendar.availability", {"date": "2026-11-10", "start": "15:00:00", "minutes": 30}
        )
        create = PlannedTask(
            "calendar.create",
            {"title": "Meeting", "date": "2026-11-10", "start": "15:00:00", "minutes": 30},
            depends_on=(0,),
        )
        snapshot = await h.run(user, availability, create)
        assert [t.status for t in snapshot.tasks] == ["completed", "waiting_for_approval"]
        assert overlapping(snapshot.tasks) <= 1  # one read, then a preview that needed it
        board = (await render.widgets_for(snapshot, None))[0]
        assert "same time" not in board.summary  # type: ignore[union-attr]


async def test_a_dependent_task_waits_for_what_it_needs_and_reuses_the_answer(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        availability = PlannedTask(
            "calendar.availability", {"date": "2026-11-10", "start": "11:00:00", "minutes": 30}
        )
        create = PlannedTask(
            "calendar.create",
            {"title": "Review", "date": "2026-11-10", "start": "11:00:00", "minutes": 30},
            depends_on=(0,),
        )
        calls = {"n": 0}
        original = h.calendar.conflicts

        async def counted(*args: Any) -> Any:
            calls["n"] += 1
            return await original(*args)

        h.calendar.conflicts = counted  # type: ignore[method-assign]
        snapshot = await h.run(user, availability, create)
        assert calls["n"] == 1  # the create step used the availability answer, not a second query
        preview = snapshot.tasks[1].result["view"]
        assert [c["title"] for c in preview["conflicts"]] == ["Study group"]  # a real clash
        assert snapshot.tasks[0].result["view"]["conflicts"][0]["title"] == "Study group"


async def test_a_failed_dependency_cancels_what_depended_on_it(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")

        async def broken(*args: Any) -> Any:
            raise NotConnected("not connected")

        h.calendar.conflicts = broken  # type: ignore[method-assign]
        availability = PlannedTask(
            "calendar.availability", {"date": "2026-11-10", "start": "11:00:00", "minutes": 30}
        )
        create = PlannedTask(
            "calendar.create",
            {"title": "Review", "date": "2026-11-10", "start": "11:00:00", "minutes": 30},
            depends_on=(0,),
        )
        snapshot = await h.run(user, availability, create)
        assert [t.status for t in snapshot.tasks] == ["failed", "cancelled"]


# Approval ---------------------------------------------------------------------------------------


async def test_nothing_that_changes_something_runs_before_approval(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(
            user,
            send_mail(),
            PlannedTask("whatsapp.send", {"to": "+919876543210", "body": "On my way"}),
            PlannedTask(
                "calendar.create",
                {"title": "Chat", "date": "2026-11-10", "start": "09:00:00", "minutes": 30},
            ),
            PlannedTask("reminder.create", {"text": "buy milk"}),
        )
        assert all(t.status == "waiting_for_approval" for t in snapshot.tasks)
        assert h.email.outbox == []
        assert h.messaging.outbox == []
        assert not any(e.title == "Chat" for e in h.calendar.items.values())
        assert await h.personal.list_reminders(user) == []


async def test_approving_twice_at_once_acts_once(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        task = snapshot.tasks[0]
        first, second = await asyncio.gather(
            h.engine.approve(user, task.id, values={"body": "I'll attend, see you."}),
            h.engine.approve(user, task.id, values={"body": "I'll attend, see you."}),
        )
        await h.engine.settle()
        assert sorted([first, second]).count("started") == 1
        assert len(h.email.outbox) == 1


async def test_what_is_sent_is_exactly_what_was_approved_after_editing(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        task = snapshot.tasks[0]
        before = task.result["hash"]
        assert await h.engine.edit(user, task.id, {"body": "Count me in."}) == "ok"
        edited = (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0]  # type: ignore[union-attr]
        assert edited.result["hash"] != before  # any change needs a fresh approval
        assert await h.engine.approve(user, task.id, shown_hash=before) == "stale"
        assert h.email.outbox == []
        assert await h.engine.approve(user, task.id, shown_hash=edited.result["hash"]) == "started"
        assert [m.body for m in h.email.outbox] == ["Count me in."]


async def test_approving_without_proof_of_what_was_shown_is_refused(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        assert await h.engine.approve(user, snapshot.tasks[0].id) == "stale"
        assert h.email.outbox == []


async def test_bad_edits_are_refused_with_a_reason(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        task = snapshot.tasks[0]
        assert "email" in (await h.engine.edit(user, task.id, {"to": "not-an-address"})).lower()
        assert await h.engine.edit(user, task.id, {"thread_id": "x"}) == (
            "That can't be changed here."
        )
        assert await h.engine.edit(user, task.id, {"subject": ""}) != "ok"
        assert h.email.outbox == []


async def test_a_header_injection_in_an_edit_cannot_reach_the_mail(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        task = snapshot.tasks[0]
        result = await h.engine.edit(user, task.id, {"to": "a@example.com\nbcc: evil@example.com"})
        assert result != "ok" or "evil" not in str(
            (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0].result["effect_args"]  # type: ignore[union-attr]
        )


# Honest outcomes --------------------------------------------------------------------------------


async def test_an_unconfirmed_send_is_never_called_a_success(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        task = snapshot.tasks[0]
        await h.engine.approve(user, task.id, values={"body": "Fine by me."})
        after = (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0]  # type: ignore[union-attr]
        # The practice mailbox never confirms a send, so it must read as unconfirmed.
        assert after.status == "failed"
        assert after.result["outcome_unknown"] is True
        assert "couldn't confirm" in (after.error or "")
        assert await h.engine.retry(user, task.id) == "check_first"  # never resend blindly
        assert len(h.email.outbox) == 1


async def test_an_ambiguous_service_error_blocks_a_blind_retry(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")

        async def explode(self_user: str, mail: OutgoingEmail, key: str) -> SentEmail:
            raise IntegrationError("connection reset")

        h.email.send = explode  # type: ignore[method-assign]
        snapshot = await h.run(user, send_mail())
        await h.engine.approve(user, snapshot.tasks[0].id, values={"body": "Okay."})
        after = (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0]  # type: ignore[union-attr]
        assert after.status == "failed" and after.result["outcome_unknown"]
        assert "check before trying again" in (after.error or "")
        assert await h.engine.retry(user, after.id) == "check_first"


async def test_a_service_that_was_never_reached_can_be_retried(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        calls = {"n": 0}
        original = h.email.important

        async def flaky(user_id: str, limit: int) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise NotConnected("not connected")
            return await original(user_id, limit)

        h.email.important = flaky  # type: ignore[method-assign]
        snapshot = await h.run(user, PlannedTask("gmail.important", {}))
        assert snapshot.tasks[0].status == "failed"
        assert await h.engine.retry(user, snapshot.tasks[0].id) == "retrying"
        await h.engine.settle()
        done = (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0]  # type: ignore[union-attr]
        assert done.status == "completed" and done.attempts == 2


async def test_a_confirmed_calendar_event_is_created_once_and_read_back(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(
            user,
            PlannedTask(
                "calendar.create",
                {"title": "Sync", "date": "2026-11-10", "start": "09:00:00", "minutes": 30},
            ),
        )
        task = snapshot.tasks[0]
        values = {"title": "Sync", "date": "2026-11-10", "start": "09:00", "minutes": 30}
        await h.engine.approve(user, task.id, values=values)
        await h.engine.approve(user, task.id, values=values)  # a second tap does nothing
        done = (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0]  # type: ignore[union-attr]
        assert done.status == "completed"
        assert sum(1 for e in h.calendar.items.values() if e.title == "Sync") == 1


# Cancel, retry, restart ---------------------------------------------------------------------


async def test_cancelling_a_waiting_task_stops_it_and_what_depends_on_it(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        assert await h.engine.cancel(user, snapshot.tasks[0].id) == "cancelled"
        await h.engine.settle()
        assert (await h.engine.approve(user, snapshot.tasks[0].id)) == "not_waiting"
        assert h.email.outbox == []


async def test_an_interrupted_effect_is_flagged_not_retried(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, send_mail())
        task = snapshot.tasks[0]
        # Pretend the server died after approval started the send.
        store = h.engine.store
        assert store.move(user, task.id, ("waiting_for_approval",), "running", approved_hash="x")
        assert store.recover_interrupted() == 1
        after = store.task(user, task.id)
        assert after is not None
        assert after.status == "failed" and after.result["outcome_unknown"] is True
        assert "can't say whether it went through" in (after.error or "")
        assert await h.engine.retry(user, task.id) == "check_first"


async def test_state_survives_a_restart_and_leftover_work_resumes(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, PlannedTask("gmail.important", {}), send_mail())
        fresh = h.make_engine()  # a "restarted" server over the same database
        again = await fresh.snapshot(user, snapshot.plan.id)
        assert again is not None
        assert [t.status for t in again.tasks] == ["completed", "waiting_for_approval"]
        # A task still pending after a crash picks up where it stopped.
        created = await fresh.submit(user, "x", "en-IN", Plan((PlannedTask("calendar.today", {}),)))
        assert not isinstance(created, str)
        h.engine.store.move(user, created.tasks[0].id, ("pending", "running"), "pending")
        await fresh.settle()
        resumed = await fresh.snapshot(user, created.plan.id)
        await fresh.settle()
        resumed = await fresh.snapshot(user, created.plan.id)
        assert resumed is not None and resumed.tasks[0].status == "completed"


# Isolation --------------------------------------------------------------------------------------


async def test_one_person_cannot_touch_anothers_tasks(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        asha, ravi = h.user("asha"), h.user("ravi")
        snapshot = await h.run(asha, send_mail())
        task = snapshot.tasks[0]
        assert await h.engine.snapshot(ravi, snapshot.plan.id) is None
        assert await h.engine.approve(ravi, task.id, values={"body": "hi"}) == "not_found"
        assert await h.engine.edit(ravi, task.id, {"body": "hi"}) == "not_found"
        assert await h.engine.cancel(ravi, task.id) == "not_found"
        assert await h.engine.retry(ravi, task.id) == "not_found"
        assert (await h.engine.latest(ravi)) is None
        assert h.email.outbox == []
        mine = await h.engine.snapshot(asha, snapshot.plan.id)
        assert mine is not None and mine.tasks[0].status == "waiting_for_approval"


# Contacts and input -------------------------------------------------------------------------------


async def test_an_unknown_recipient_waits_for_input_and_is_never_guessed(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(
            user, PlannedTask("whatsapp.send", {"to": "Dr Nobody", "body": "Running late"})
        )
        task = snapshot.tasks[0]
        assert task.status == "waiting_for_input"
        assert "don't have a WhatsApp number" in task.result["needs"]
        assert await h.engine.approve(user, task.id, values={"body": "Running late"}) == (
            "needs_input"
        )
        assert h.messaging.outbox == []
        assert await h.engine.edit(user, task.id, {"to": "+919876543210"}) == "ok"
        moved = (await h.engine.snapshot(user, snapshot.plan.id)).tasks[0]  # type: ignore[union-attr]
        assert moved.status == "waiting_for_approval"


async def test_demo_whatsapp_never_reports_delivery(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(
            user,
            PlannedTask("whatsapp.send", {"to": "my mother", "body": "Reaching in 15 minutes"}),
        )
        task = snapshot.tasks[0]
        assert task.status == "waiting_for_approval"
        assert task.result["view"]["demo"] is True
        await h.engine.approve(user, task.id, values={"body": "Reaching in 15 minutes"})
        done = await h.engine.snapshot(user, snapshot.plan.id)
        assert done is not None and done.tasks[0].status == "completed"
        widgets = await render.widgets_for(done, h.messaging)
        text = repr([w.model_dump() for w in widgets]).lower()
        assert "delivered" not in text and "read" not in text.replace("already", "")
        assert "demo" in text


async def test_a_demo_contact_is_never_used_for_a_real_channel(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        h.demo["whatsapp"] = False  # WhatsApp is real here, Gmail is practice
        contacts = h.services().contacts
        real = await contacts.resolve(user, "Amma", "phone")
        assert real.kind == "none"  # the made-up number is not offered to a real channel
        assert (await contacts.resolve(user, "Amma", "email")).kind == "one"


# Untrusted content -----------------------------------------------------------------------------


async def test_instructions_inside_an_email_are_inert_data(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(user, PlannedTask("gmail.search", {"query": "sale"}))
        view = snapshot.tasks[0].result["view"]
        text = " ".join(e["snippet"] + e["subject"] for e in view["emails"])
        assert "Ignore all previous instructions" in text  # shown as plain text
        assert len(snapshot.tasks) == 1  # it created no tasks
        assert h.email.outbox == [] and h.messaging.outbox == []
        mail: Email = await h.email.get(user, "demo05")
        assert "attacker@example.com" in mail.body_text
        widgets = await render.widgets_for(snapshot, None)
        assert all(w.type in ("task_board", "email_list") for w in widgets)


# Limits ----------------------------------------------------------------------------------------


async def test_too_many_open_plans_are_refused(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        for _ in range(3):
            assert not isinstance(
                await h.engine.submit(user, "x", "en-IN", Plan((send_mail(),))), str
            )
            await h.engine.settle()
        assert await h.engine.submit(user, "x", "en-IN", Plan((send_mail(),))) == "too_many"


async def test_an_empty_plan_is_not_run(tmp_path: Path) -> None:
    async with harness(tmp_path) as h:
        assert await h.engine.submit(h.user("asha"), "x", "en-IN", Plan(())) == "empty"


# Day plan -------------------------------------------------------------------------------------


async def test_the_day_plan_combines_calendar_reminders_and_email_and_keeps_edits(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        await h.personal.create_reminder(user, "Call the bank", None)
        snapshot = await h.run(user, PlannedTask("day.plan", {}))
        view = snapshot.tasks[0].result["view"]
        kinds = {i["kind"] for i in view["items"]}
        assert {"event", "reminder", "email"} <= kinds
        assert view["demo"] is True
        reminder = next(i for i in view["items"] if i["kind"] == "reminder")
        task_id = snapshot.tasks[0].id
        assert await h.engine.plan_action(user, task_id, "plan.toggle", reminder["id"], {})
        assert (await h.personal.list_reminders(user))[0].done_at is not None  # the real one
        assert await h.engine.plan_action(
            user, task_id, "plan.priority", reminder["id"], {"priority": "low"}
        )
        assert await h.engine.plan_action(user, task_id, "plan.add", "", {"title": "Water plants"})
        assert not await h.engine.plan_action(
            user, task_id, "plan.priority", reminder["id"], {"priority": "urgent"}
        )
        again = await h.engine.snapshot(user, snapshot.plan.id)
        items = again.tasks[0].result["view"]["items"]  # type: ignore[union-attr]
        assert any(i["title"] == "Water plants" for i in items)
        assert next(i for i in items if i["id"] == reminder["id"])["priority"] == "low"
        assert h.calendar.created == {} and h.email.outbox == []  # the plan changed nothing outside


# Spoken approval -------------------------------------------------------------------------------


async def test_voice_approval_is_a_two_step_readback(tmp_path: Path) -> None:
    from app.tasks import turn

    async with harness(tmp_path) as h:
        user = h.user("asha")
        await h.run(
            user, PlannedTask("whatsapp.send", {"to": "+919876543210", "body": "Late by 10"})
        )

        async def say(text: str) -> turn.TurnReply | None:
            return await turn.handle(h.engine, user, "en-IN", text, None, order_busy=False)

        assert await say("yes") is None  # a bare yes starts nothing
        reply = await say("send it")
        assert reply is not None and "Late by 10" in reply.message and "confirm" in reply.message
        assert h.messaging.outbox == []  # reading it back sent nothing
        done = await say("confirm")
        assert done is not None
        await h.engine.settle()
        assert len(h.messaging.outbox) == 1
        assert await say("confirm") is None  # the readback is used up


async def test_voice_approval_is_void_if_the_content_changed_after_the_readback(
    tmp_path: Path,
) -> None:
    from app.tasks import turn

    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(
            user, PlannedTask("whatsapp.send", {"to": "+919876543210", "body": "Late by 10"})
        )

        async def say(text: str) -> turn.TurnReply | None:
            return await turn.handle(h.engine, user, "en-IN", text, None, order_busy=False)

        await say("send it")
        assert await h.engine.edit(user, snapshot.tasks[0].id, {"body": "Actually on time"}) == "ok"
        reply = await say("confirm")
        assert reply is not None  # it answered, but sent nothing
        assert h.messaging.outbox == []


async def test_voice_approval_never_steals_an_order_confirmation(tmp_path: Path) -> None:
    from app.tasks import turn

    async with harness(tmp_path) as h:
        user = h.user("asha")
        await h.run(user, PlannedTask("whatsapp.send", {"to": "+919876543210", "body": "Hi"}))
        reply = await turn.handle(h.engine, user, "en-IN", "send it", None, order_busy=True)
        assert reply is None  # the ordering assistant keeps the conversation
        assert h.messaging.outbox == []


# Changing your mind -----------------------------------------------------------------------------


async def test_changing_the_time_updates_the_same_widget_instead_of_adding_one(
    tmp_path: Path,
) -> None:
    async with harness(tmp_path) as h:
        user = h.user("asha")
        snapshot = await h.run(
            user,
            PlannedTask(
                "calendar.create",
                {
                    "title": "Meeting with Rahul",
                    "date": "2026-11-10",
                    "start": "15:00:00",
                    "minutes": 30,
                },
            ),
        )
        first = [w for w in await render.widgets_for(snapshot, None) if w.type == "event_preview"]
        assert first[0].fields.start == "15:00"  # type: ignore[union-attr]
        assert await h.engine.refine_time(user, "make it 4 PM instead") == snapshot.tasks[0].id
        again = await h.engine.snapshot(user, snapshot.plan.id)
        second = [w for w in await render.widgets_for(again, None) if w.type == "event_preview"]  # type: ignore[arg-type]
        assert second[0].id == first[0].id  # same id: the browser replaces it in place
        assert second[0].fields.start == "16:00"  # type: ignore[union-attr]
        assert len([t for t in again.tasks if t.kind == "calendar.create"]) == 1  # type: ignore[union-attr]
        assert await h.engine.refine_time(h.user("ravi"), "make it 5 PM") is None  # not Ravi's
