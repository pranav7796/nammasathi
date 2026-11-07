"""Reminders and notes: store, service, and routes, with strict per-user isolation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.accounts.db import SqliteDatabase, migrate
from app.accounts.service import AccountService
from app.accounts.store import AccountStore
from app.config import Settings
from app.llm.scripted import ScriptedExtractor
from app.personal.routes import personal_router
from app.personal.service import PersonalService, WriteLimited, WriteLimiter
from app.personal.store import (
    MAX_NOTE_BODY,
    MAX_NOTES,
    MAX_REMINDER_TEXT,
    MAX_REMINDERS,
    InvalidText,
    LimitReached,
    NotFound,
    PersonalStore,
)
from app.web import create_app
from tests.test_accounts import BASE, KEY, RecordingMailer, SilentTTS, register

# Store


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[SqliteDatabase, AccountStore, str, str]]:
    database = SqliteDatabase(tmp_path / "p.db")
    migrate(database)
    accounts = AccountStore(database)
    a = accounts.create_user("a@example.com", "x").id
    b = accounts.create_user("b@example.com", "x").id
    yield database, accounts, a, b


def test_reminder_crud_and_order(db: tuple[SqliteDatabase, AccountStore, str, str]) -> None:
    database, _, a, _ = db
    store = PersonalStore(database)
    late = store.create_reminder(a, "  late  ", 2000, now_ms=1)
    soon = store.create_reminder(a, "soon", 1000, now_ms=2)
    nodue = store.create_reminder(a, "no time", None, now_ms=3)
    assert late.text == "late"
    assert [r.id for r in store.list_reminders(a)] == [soon.id, late.id, nodue.id]

    store.complete_reminder(a, soon.id, now_ms=10)
    store.complete_reminder(a, late.id, now_ms=20)
    # open first, then done ones, newest completion first
    assert [r.id for r in store.list_reminders(a)] == [nodue.id, late.id, soon.id]
    assert store.get_reminder(a, late.id).done_at == 20

    store.uncomplete_reminder(a, late.id, now_ms=30)
    assert store.get_reminder(a, late.id).done_at is None

    edited = store.update_reminder(a, nodue.id, text="renamed", due_at=500, change_due=True)
    assert (edited.text, edited.due_at) == ("renamed", 500)
    kept = store.update_reminder(a, nodue.id, text="again")
    assert kept.due_at == 500
    cleared = store.update_reminder(a, nodue.id, due_at=None, change_due=True)
    assert cleared.due_at is None and cleared.text == "again"

    store.delete_reminder(a, soon.id)
    with pytest.raises(NotFound):
        store.get_reminder(a, soon.id)
    assert len(soon.id) == 32 and int(soon.id, 16) >= 0


def test_note_crud(db: tuple[SqliteDatabase, AccountStore, str, str]) -> None:
    database, _, a, _ = db
    store = PersonalStore(database)
    first = store.create_note(a, "first\nline two", now_ms=1)
    second = store.create_note(a, "second", now_ms=2)
    assert [n.id for n in store.list_notes(a)] == [second.id, first.id]
    store.update_note(a, first.id, "changed", now_ms=5)
    assert [n.id for n in store.list_notes(a)] == [first.id, second.id]
    store.delete_note(a, second.id)
    assert [n.body for n in store.list_notes(a)] == ["changed"]
    with pytest.raises(NotFound):
        store.update_note(a, second.id, "x")


@pytest.mark.parametrize(
    "bad", ["", "   ", "x" * (MAX_REMINDER_TEXT + 1), "bell\x07", "a\nb", "z\x00"]
)
def test_reminder_text_rules(db: tuple[SqliteDatabase, AccountStore, str, str], bad: str) -> None:
    store = PersonalStore(db[0])
    with pytest.raises(InvalidText):
        store.create_reminder(db[2], bad, None)


def test_note_text_rules(db: tuple[SqliteDatabase, AccountStore, str, str]) -> None:
    store = PersonalStore(db[0])
    store.create_note(db[2], "x" * MAX_NOTE_BODY)
    for bad in ("", " \n ", "x" * (MAX_NOTE_BODY + 1), "bad\x1b"):
        with pytest.raises(InvalidText):
            store.create_note(db[2], bad)


def test_limits_are_per_user(db: tuple[SqliteDatabase, AccountStore, str, str]) -> None:
    database, _, a, b = db
    store = PersonalStore(database)
    with database.transaction() as tx:
        for i in range(MAX_REMINDERS):
            tx.execute(
                "INSERT INTO reminders (id, user_id, text, created_at, updated_at) "
                "VALUES (?, ?, 't', 1, 1)",
                (f"r{i}", a),
            )
        for i in range(MAX_NOTES):
            tx.execute(
                "INSERT INTO notes (id, user_id, body, created_at, updated_at) "
                "VALUES (?, ?, 'n', 1, 1)",
                (f"n{i}", a),
            )
    with pytest.raises(LimitReached):
        store.create_reminder(a, "one more", None)
    with pytest.raises(LimitReached):
        store.create_note(a, "one more")
    store.create_reminder(b, "fine", None)
    store.create_note(b, "fine")
    store.delete_reminder(a, "r0")
    store.create_reminder(a, "now there is room", None)


def test_isolation_in_the_store(db: tuple[SqliteDatabase, AccountStore, str, str]) -> None:
    database, _, a, b = db
    store = PersonalStore(database)
    reminder = store.create_reminder(a, "secret", 1)
    note = store.create_note(a, "secret")
    assert store.list_reminders(b) == [] and store.list_notes(b) == []
    for call in (
        lambda: store.get_reminder(b, reminder.id),
        lambda: store.update_reminder(b, reminder.id, text="hacked"),
        lambda: store.complete_reminder(b, reminder.id),
        lambda: store.uncomplete_reminder(b, reminder.id),
        lambda: store.delete_reminder(b, reminder.id),
        lambda: store.get_note(b, note.id),
        lambda: store.update_note(b, note.id, "hacked"),
        lambda: store.delete_note(b, note.id),
    ):
        with pytest.raises(NotFound):
            call()
    assert store.get_reminder(a, reminder.id).text == "secret"
    assert store.get_reminder(a, reminder.id).done_at is None
    assert store.get_note(a, note.id).body == "secret"


def test_data_survives_a_restart_and_goes_with_the_user(
    tmp_path: Path, db: tuple[SqliteDatabase, AccountStore, str, str]
) -> None:
    database, accounts, a, b = db
    PersonalStore(database).create_reminder(a, "remember", 123)
    PersonalStore(database).create_note(a, "kept")
    PersonalStore(database).create_note(b, "other")

    reopened = SqliteDatabase(tmp_path / "p.db")
    store = PersonalStore(reopened)
    assert [r.text for r in store.list_reminders(a)] == ["remember"]
    assert [n.body for n in store.list_notes(a)] == ["kept"]

    accounts.delete_user(a)
    assert store.list_reminders(a) == [] and store.list_notes(a) == []
    assert [n.body for n in store.list_notes(b)] == ["other"]
    with database.transaction() as tx:
        assert tx.execute("SELECT id FROM reminders") == []


# Service


@asynccontextmanager
async def service_site(tmp_path: Path) -> AsyncGenerator[tuple[FastAPI, PersonalService]]:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        accounts = cast(AccountService, app.state.accounts)
        yield app, PersonalService(accounts, now=lambda: 1_794_000_000.0)


def _app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        demo_understanding="scripted",
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'saathi.db'}",
        token_encryption_keys=KEY,
        open_registration=True,
        support_email="help@example.com",
    )
    return create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
        mailer=RecordingMailer(),
    )


async def test_service_parses_a_spoken_reminder_and_ignores_double_submits(tmp_path: Path) -> None:
    async with service_site(tmp_path) as (app, service):
        accounts = cast(AccountService, app.state.accounts)
        user = await accounts.run(accounts.store.create_user, "s@example.com", "x")
        first = await service.create_reminder_from_text(user.id, "Call mom tomorrow at 7 pm")
        again = await service.create_reminder_from_text(user.id, "Call mom tomorrow at 7 pm")
        assert first.text == "Call mom" and first.due_at is not None
        assert again.id == first.id
        assert len(await service.list_reminders(user.id)) == 1

        plain = await service.create_reminder_from_text(user.id, "buy a gift")
        assert plain.due_at is None and plain.text == "buy a gift"
        assert (await service.create_reminder_from_text(user.id, "buy a gift")).id == plain.id
        only_time = await service.create_reminder_from_text(user.id, "tomorrow at 7 pm")
        assert only_time.text == "tomorrow at 7 pm"

        # A different time is a different reminder; so is the same text a minute later.
        other = await service.create_reminder_from_text(user.id, "Call mom tomorrow at 8 pm")
        assert other.id != first.id
        late = PersonalService(accounts, now=lambda: 1_794_000_000.0 + 61)
        assert (await late.create_reminder_from_text(user.id, "buy a gift")).id != plain.id


async def test_duplicates_are_per_user(tmp_path: Path) -> None:
    async with service_site(tmp_path) as (app, service):
        accounts = cast(AccountService, app.state.accounts)
        a = await accounts.run(accounts.store.create_user, "a@example.com", "x")
        b = await accounts.run(accounts.store.create_user, "b@example.com", "x")
        one = await service.create_reminder_from_text(a.id, "same thing")
        two = await service.create_reminder_from_text(b.id, "same thing")
        assert one.id != two.id


def test_write_limiter_slides() -> None:
    clock = [1000.0]
    limiter = WriteLimiter(limit=3, clock=lambda: clock[0])
    for _ in range(3):
        limiter.check("u")
    with pytest.raises(WriteLimited):
        limiter.check("u")
    limiter.check("other")
    clock[0] += 61
    limiter.check("u")


# Routes


@dataclass
class Site:
    app: FastAPI

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)


@asynccontextmanager
async def route_site(tmp_path: Path) -> AsyncGenerator[Site]:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        app.include_router(personal_router(cast(AccountService, app.state.accounts)))
        yield Site(app)


async def test_signed_out_is_401(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as anon:
        assert (await anon.get("/api/personal/reminders")).status_code == 401
        assert (await anon.get("/api/personal/notes")).status_code == 401
        assert (await anon.post("/api/personal/notes", json={"body": "x"})).status_code == 401
        assert (await anon.delete("/api/personal/reminders/abc")).status_code == 401


async def test_reminder_routes(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        created = await browser.post(
            "/api/personal/reminders",
            json={"text": "Call mom", "due": "2026-11-08T19:00:00+05:30"},
        )
        assert created.status_code == 200
        item = created.json()
        assert item["due"] == "2026-11-08T19:00:00+05:30"
        assert item["done"] is False

        # UTC input is returned in IST
        utc = await browser.post(
            "/api/personal/reminders", json={"text": "tea", "due": "2026-11-08T00:00:00Z"}
        )
        assert utc.json()["due"] == "2026-11-08T05:30:00+05:30"

        spoken = await browser.post(
            "/api/personal/reminders",
            json={"text": "Water plants", "when_text": "tomorrow at 7 pm"},
        )
        assert spoken.json()["due"].endswith("19:00:00+05:30")
        untimed = await browser.post("/api/personal/reminders", json={"text": "someday"})
        assert untimed.json()["due"] is None
        unclear = await browser.post(
            "/api/personal/reminders", json={"text": "vague", "when_text": "at 7"}
        )
        assert unclear.json()["due"] is None

        listed = (await browser.get("/api/personal/reminders")).json()["reminders"]
        assert [r["text"] for r in listed][-2:] == ["someday", "vague"]

        rid = item["id"]
        patched = await browser.patch(f"/api/personal/reminders/{rid}", json={"text": "Call dad"})
        assert patched.json()["text"] == "Call dad"
        assert patched.json()["due"] == "2026-11-08T19:00:00+05:30"
        cleared = await browser.patch(f"/api/personal/reminders/{rid}", json={"due": None})
        assert cleared.json()["due"] is None
        done = await browser.post(f"/api/personal/reminders/{rid}/done")
        assert done.json()["done"] is True
        undone = await browser.post(f"/api/personal/reminders/{rid}/undo")
        assert undone.json()["done"] is False
        assert (await browser.delete(f"/api/personal/reminders/{rid}")).json() == {"deleted": True}
        assert (await browser.delete(f"/api/personal/reminders/{rid}")).status_code == 404


async def test_note_routes(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        note = (await browser.post("/api/personal/notes", json={"body": "wifi: 1234"})).json()
        patched = await browser.patch(f"/api/personal/notes/{note['id']}", json={"body": "new"})
        assert patched.json()["body"] == "new"
        assert (await browser.get("/api/personal/notes")).json()["notes"][0]["body"] == "new"
        assert (await browser.delete(f"/api/personal/notes/{note['id']}")).status_code == 200
        assert (await browser.get("/api/personal/notes")).json() == {"notes": []}


async def test_bad_input_is_refused(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        post = browser.post
        assert (await post("/api/personal/reminders", json={"text": ""})).status_code == 422
        assert (
            await post("/api/personal/reminders", json={"text": "x", "user_id": "other"})
        ).status_code == 422
        assert (
            await post("/api/personal/reminders", json={"text": "x", "due": "tomorrow"})
        ).status_code == 422
        assert (await post("/api/personal/reminders", json={"text": 5})).status_code == 422
        assert (await post("/api/personal/reminders", content=b"not json")).status_code == 422
        assert (await post("/api/personal/notes", json={"body": "x" * 9000})).status_code == 422
        assert (await post("/api/personal/notes", json={"body": "x", "id": "1"})).status_code == 422
        too_long = await post("/api/personal/reminders", json={"text": "x" * 501})
        assert too_long.status_code == 422


async def test_users_cannot_touch_each_others_items(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as alice, s.browser() as bob:
        await register(alice, "alice@example.com")
        await register(bob, "bob@example.com")
        reminder = (
            await alice.post("/api/personal/reminders", json={"text": "alice private"})
        ).json()
        note = (await alice.post("/api/personal/notes", json={"body": "alice note"})).json()

        assert (await bob.get("/api/personal/reminders")).json() == {"reminders": []}
        assert (await bob.get("/api/personal/notes")).json() == {"notes": []}
        r, n = reminder["id"], note["id"]
        attempts = [
            await bob.patch(f"/api/personal/reminders/{r}", json={"text": "bob"}),
            await bob.post(f"/api/personal/reminders/{r}/done"),
            await bob.post(f"/api/personal/reminders/{r}/undo"),
            await bob.delete(f"/api/personal/reminders/{r}"),
            await bob.patch(f"/api/personal/notes/{n}", json={"body": "bob"}),
            await bob.delete(f"/api/personal/notes/{n}"),
        ]
        assert [a.status_code for a in attempts] == [404] * 6
        assert "alice" not in "".join(a.text for a in attempts)

        mine = (await alice.get("/api/personal/reminders")).json()["reminders"]
        assert [(i["text"], i["done"]) for i in mine] == [("alice private", False)]
        assert (await alice.get("/api/personal/notes")).json()["notes"][0]["body"] == "alice note"


async def test_write_rate_limit(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        codes = [
            (await browser.post("/api/personal/notes", json={"body": f"n{i}"})).status_code
            for i in range(62)
        ]
        assert codes[:60] == [200] * 60
        assert codes[60:] == [429, 429]
        assert (await browser.get("/api/personal/notes")).status_code == 200


async def test_limit_reached_is_a_clear_conflict(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        accounts = cast(AccountService, s.app.state.accounts)
        me = (await browser.get("/api/account/me")).json()["user"]["email"]
        user = await accounts.run(accounts.store.user_by_email, me)
        assert user is not None
        with accounts.database.transaction() as tx:
            for i in range(MAX_REMINDERS):
                tx.execute(
                    "INSERT INTO reminders (id, user_id, text, created_at, updated_at) "
                    "VALUES (?, ?, 't', 1, 1)",
                    (f"r{i}", user.id),
                )
        full = await browser.post("/api/personal/reminders", json={"text": "more"})
        assert full.status_code == 409
        assert "200" in full.json()["detail"]


async def test_database_down_is_503(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        accounts = cast(AccountService, s.app.state.accounts)

        def broken() -> None:
            raise RuntimeError("down")

        accounts.database.transaction = broken  # type: ignore[method-assign,assignment]
        # The session cookie itself is checked in the database, so this fails closed too.
        for response in (
            await browser.get("/api/personal/reminders"),
            await browser.post("/api/personal/notes", json={"body": "x"}),
        ):
            assert response.status_code == 503


async def test_database_failing_after_sign_in_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)

        def broken(self: PersonalStore, user_id: str) -> None:
            raise RuntimeError("database went away")

        monkeypatch.setattr(PersonalStore, "list_reminders", broken)
        response = await browser.get("/api/personal/reminders")
        assert response.status_code == 503
        assert "went away" not in response.text


async def test_a_reminder_written_in_plain_words_has_its_time_taken_out_of_the_text(
    tmp_path: Path,
) -> None:
    """The My Day box sends "call Amma tomorrow at 7 pm" as both the text and the when."""
    from tests.test_accounts import register, site

    async with site(tmp_path) as s, s.browser() as client:
        await register(client)
        words = "call Amma tomorrow at 7 pm"
        made = await client.post(
            "/api/personal/reminders", json={"text": words, "when_text": words}
        )
        assert made.status_code == 200, made.text
        item = made.json()
        assert item["text"] == "call Amma" and item["due"] and "T19:00" in item["due"]
