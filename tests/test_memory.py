"""Memory: allow-list, limits, sensitive values, isolation, source rule, routes."""

from __future__ import annotations

import logging
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
from app.accounts.store import AccountStore, Turn
from app.config import Settings
from app.llm.scripted import ScriptedExtractor
from app.memory.routes import memory_router
from app.memory.service import MemoryService, WriteLimited, WriteLimiter
from app.memory.store import (
    MEMORY_KEYS,
    InvalidValue,
    MemoryStore,
    SensitiveValue,
    UnknownKey,
)
from app.web import create_app
from tests.test_accounts import BASE, KEY, RecordingMailer, SilentTTS, register

type Fix = tuple[SqliteDatabase, AccountStore, str, str]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Fix]:
    database = SqliteDatabase(tmp_path / "m.db")
    migrate(database)
    accounts = AccountStore(database)
    yield (
        database,
        accounts,
        accounts.create_user("a@example.com", "x").id,
        accounts.create_user("b@example.com", "x").id,
    )


def test_allow_list_and_caps(db: Fix) -> None:
    database, _, a, _ = db
    store = MemoryStore(database)
    assert set(MEMORY_KEYS) == {
        "response_language",
        "dietary",
        "reminder_style",
        "frequent_services",
        "home_area",
        "greeting_name",
    }
    with pytest.raises(UnknownKey):
        store.set_item(a, "favourite_colour", "blue")
    with pytest.raises(UnknownKey):
        store.forget(a, "favourite_colour")
    assert store.set_item(a, "greeting_name", "  Pranav  ").value == "Pranav"
    for key, limit in (
        ("dietary", 200),
        ("reminder_style", 100),
        ("frequent_services", 200),
        ("home_area", 100),
        ("greeting_name", 60),
    ):
        store.set_item(a, key, ("x " * limit)[:limit])
        with pytest.raises(InvalidValue):
            store.set_item(a, key, ("x " * limit)[: limit + 1])
    with pytest.raises(InvalidValue):
        store.set_item(a, "dietary", "   ")
    with pytest.raises(InvalidValue):
        store.set_item(a, "dietary", "no\x00onions")
    with pytest.raises(InvalidValue):
        store.set_item(a, "dietary", "line\nbreak")


def test_response_language_values(db: Fix) -> None:
    database, _, a, _ = db
    store = MemoryStore(database)
    for lang in ("kn-IN", "hi-IN", "en-IN"):
        assert store.set_item(a, "response_language", lang).value == lang
    with pytest.raises(InvalidValue):
        store.set_item(a, "response_language", "fr-FR")


@pytest.mark.parametrize(
    "text",
    [
        "1234 5678 9012",
        "123456789012345",
        "1234-5678-9012-3456",
        "my password is hunter2",
        "OTP 482913",
        "the pin is 4",
        "my API key is abc",
        "api_key",
        "a secret thing",
        "sk1234567890abcdefghijklmnop",
    ],
)
def test_sensitive_values_rejected(db: Fix, text: str) -> None:
    database, _, a, _ = db
    store = MemoryStore(database)
    with pytest.raises(SensitiveValue) as err:
        store.set_item(a, "dietary", text)
    assert text not in str(err.value)
    assert store.list_items(a) == []


def test_ordinary_values_pass(db: Fix) -> None:
    database, _, a, _ = db
    store = MemoryStore(database)
    store.set_item(a, "dietary", "vegetarian, no onion or garlic, 2 spoons sugar")
    store.set_item(a, "home_area", "Indiranagar, Bengaluru 560038")
    assert len(store.list_items(a)) == 2


def test_crud_and_isolation(db: Fix) -> None:
    database, _, a, b = db
    store = MemoryStore(database)
    store.set_item(a, "dietary", "vegan", now_ms=1)
    store.set_item(a, "dietary", "jain", now_ms=2)
    store.set_item(b, "dietary", "eggs ok")
    item = store.get_item(a, "dietary")
    assert item is not None and (item.value, item.updated_at) == ("jain", 2)
    assert [i.value for i in store.list_items(b)] == ["eggs ok"]
    assert store.forget(b, "home_area") is False
    assert store.forget(a, "dietary") is True
    assert store.get_item(a, "dietary") is None
    assert store.get_item(b, "dietary") is not None
    store.set_item(a, "home_area", "x")
    store.forget_all(a)
    assert store.list_items(a) == []
    assert len(store.list_items(b)) == 1


def test_persists_and_cascades(tmp_path: Path, db: Fix) -> None:
    database, accounts, a, b = db
    MemoryStore(database).set_item(a, "greeting_name", "Asha")
    MemoryStore(database).set_item(b, "greeting_name", "Bala")
    reopened = MemoryStore(SqliteDatabase(tmp_path / "m.db"))
    item = reopened.get_item(a, "greeting_name")
    assert item is not None and item.value == "Asha"
    accounts.delete_user(a)
    assert reopened.list_items(a) == []
    assert len(reopened.list_items(b)) == 1


def test_memory_and_history_are_separate(db: Fix) -> None:
    database, accounts, a, _ = db
    store = MemoryStore(database)
    accounts.add_turn(a, Turn(1000, "en-IN", "groceries", "milk", "ok"))
    store.set_item(a, "dietary", "vegan")
    accounts.clear_turns(a)
    assert store.get_item(a, "dietary") is not None
    accounts.add_turn(a, Turn(2000, "en-IN", "groceries", "eggs", "ok"))
    store.forget_all(a)
    assert len(accounts.turns(a)) == 1


# Service


@asynccontextmanager
async def service_site(tmp_path: Path) -> AsyncGenerator[tuple[FastAPI, MemoryService]]:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        yield app, MemoryService(cast(AccountService, app.state.accounts))


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


@pytest.mark.parametrize("source", ["email", "web", "model", "tool", "", "User", "assistant"])
async def test_only_the_user_may_write(tmp_path: Path, source: str) -> None:
    async with service_site(tmp_path) as (app, service):
        accounts = cast(AccountService, app.state.accounts)
        user = await accounts.run(accounts.store.create_user, "s@example.com", "x")
        with pytest.raises(PermissionError):
            await service.set_item(user.id, "dietary", "vegan", source=source)
        with pytest.raises(PermissionError):
            await service.forget(user.id, "dietary", source=source)
        with pytest.raises(PermissionError):
            await service.forget_all(user.id, source=source)
        with pytest.raises(PermissionError):
            await service.create_contact(user.id, "Amma", phone="+919876543210", source=source)
        assert await service.list_items(user.id) == []
        assert await service.list_contacts(user.id) == []


async def test_service_context_language_and_no_log_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with service_site(tmp_path) as (app, service):
        accounts = cast(AccountService, app.state.accounts)
        a = await accounts.run(accounts.store.create_user, "a@example.com", "x")
        b = await accounts.run(accounts.store.create_user, "b@example.com", "x")
        assert await service.preferred_language(a.id) is None
        await service.set_item(a.id, "response_language", "kn-IN")
        await service.set_item(a.id, "dietary", "zzsecretdiet")
        await service.create_contact(a.id, "Zzname", email="zz@example.org", phone="+911234567890")
        with pytest.raises(SensitiveValue):
            await service.set_item(a.id, "home_area", "password is zzleak")
        assert await service.preferred_language(a.id) == "kn-IN"
        assert await service.preferred_language(b.id) is None
        ctx = await service.memory_context(a.id)
        assert ctx == {"response_language": "kn-IN", "dietary": "zzsecretdiet"}
        assert "zz@example.org" not in str(ctx) and "Zzname" not in str(ctx)
        assert await service.memory_context(b.id) == {}
    logged = caplog.text
    for secret in ("zzsecretdiet", "Zzname", "zz@example.org", "+911234567890", "zzleak"):
        assert secret not in logged


def test_write_limiter() -> None:
    clock = [1000.0]
    limiter = WriteLimiter(limit=2, clock=lambda: clock[0])
    limiter.check("u")
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
        app.include_router(memory_router(cast(AccountService, app.state.accounts)))
        yield Site(app)


async def test_signed_out_is_401(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as anon:
        assert (await anon.get("/api/memory")).status_code == 401
        assert (await anon.put("/api/memory/dietary", json={"value": "x"})).status_code == 401
        assert (await anon.delete("/api/memory/dietary")).status_code == 401
        assert (await anon.delete("/api/memory")).status_code == 401
        assert (await anon.get("/api/memory/panel")).status_code == 401


async def test_memory_routes(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as one, s.browser() as two:
        await register(one)
        await register(two, "other@example.com")
        listed = (await one.get("/api/memory")).json()
        assert listed["items"] == [] and "see, change or delete" in listed["consent"]
        assert {k["key"] for k in listed["allowed"]} == set(MEMORY_KEYS)

        put = await one.put("/api/memory/dietary", json={"value": " vegan "})
        assert put.status_code == 200
        assert put.json()["value"] == "vegan" and put.json()["label"]
        assert (await one.put("/api/memory/colour", json={"value": "blue"})).status_code == 422
        assert (await one.put("/api/memory/dietary", json={"value": "x " * 101})).status_code == 422
        assert (await one.put("/api/memory/dietary", json={"value": "otp 1"})).status_code == 422
        assert (await one.put("/api/memory/dietary", json={"value": 3})).status_code == 422
        extra = await one.put("/api/memory/dietary", json={"value": "a", "user_id": "x"})
        assert extra.status_code == 422

        assert (await two.get("/api/memory")).json()["items"] == []
        assert (await two.delete("/api/memory/dietary")).status_code == 200
        assert len((await one.get("/api/memory")).json()["items"]) == 1

        assert (await one.delete("/api/memory/dietary")).status_code == 200
        await one.put("/api/memory/home_area", json={"value": "Koramangala"})
        assert (await one.delete("/api/memory")).status_code == 200
        assert (await one.get("/api/memory")).json()["items"] == []


async def test_panel_is_ui_or_501(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        await browser.put("/api/memory/dietary", json={"value": "vegan"})
        response = await browser.get("/api/memory/panel")
        assert response.status_code == 200
        if response.status_code == 200:
            assert isinstance(response.json()["ui"], list)


async def test_write_rate_limit_is_429(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        codes = [
            (await browser.put("/api/memory/dietary", json={"value": "v"})).status_code
            for _ in range(61)
        ]
        assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_database_down_is_503(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        accounts = cast(AccountService, s.app.state.accounts)

        def broken() -> None:
            raise RuntimeError("down")

        accounts.database.transaction = broken  # type: ignore[method-assign,assignment]
        assert (await browser.get("/api/memory")).status_code == 503
        assert (await browser.get("/api/contacts")).status_code == 503
