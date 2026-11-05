"""What real people need before Swiggy grants access: accounts that work, a demo that is theirs,
privacy between them, and a site that keeps working when the database does not."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import socket
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from fastapi import FastAPI

from app.accounts import service as service_module
from app.accounts.db import SCHEMA_VERSION, connect, migrate
from app.accounts.service import (
    SIGNUPS_PER_ADDRESS_PER_HOUR,
    SIGNUPS_PER_HOUR,
    AccountService,
)
from app.accounts.store import HISTORY_KEPT_TURNS, AccountStore, Turn
from app.config import Settings
from app.llm.scripted import ScriptedExtractor
from app.web import create_app
from tests.conftest import _real_connect  # pyright: ignore[reportPrivateUsage]
from tests.test_multi_user_live import (
    database_url,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

BASE = "https://saathi.example"
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery"

JOURNEYS = {
    "en-IN": {
        "grocery": ("Two kilos of rice and one litre of milk", "Yes"),
        "food": ("I want something for dinner", "Chicken biryani", "The second one", "Yes"),
        "dining": (
            "Book a table for two in Indiranagar tomorrow at 7 PM, Italian",
            "First",
            "Yes",
        ),
    },
    "hi-IN": {"grocery": ("दो किलो चावल और एक लीटर दूध", "हाँ")},
    "kn-IN": {"grocery": ("ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು", "ಹೌದು")},
}


class SilentTTS:
    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        del text
        destination = Path(output_path)
        destination.write_bytes(b"RIFF")
        return destination


def settings_for(url: str, **extra: Any) -> Settings:
    values: dict[str, Any] = {
        "demo_understanding": "scripted",
        "public_deployment": True,
        "public_base_url": BASE,
        "database_url": url,
        "token_encryption_keys": KEY,
    }
    values.update(extra)
    return Settings(**values)


@asynccontextmanager
async def running(settings: Settings) -> AsyncGenerator[FastAPI]:
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
    )
    async with app.router.lifespan_context(app):
        yield app


def browser(app: FastAPI, address: str = "203.0.113.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(address, 4321))
    return httpx.AsyncClient(transport=transport, base_url=BASE)


async def sign_up(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post(
        "/api/account/register", json={"email": email, "password": PASSWORD, "consent": True}
    )
    assert response.status_code == 200, response.text


async def converse(
    client: httpx.AsyncClient, language: str, turns: tuple[str, ...]
) -> dict[str, Any]:
    started = await client.post("/api/sessions", json={"language": language})
    assert started.status_code == 200, started.text
    headers = {"X-Session-ID": started.json()["session_id"]}
    reply: dict[str, Any] = started.json()
    for text in turns:
        response = await client.post("/api/session/text", json={"text": text}, headers=headers)
        assert response.status_code == 200, response.text
        reply = response.json()
    reply["session_id"] = headers["X-Session-ID"]
    return reply


# Sign-up limits


async def test_many_people_can_sign_up_at_once(database_url: str) -> None:  # noqa: F811
    """The bug this replaces: after ten sign-ups anywhere, nobody else could sign up."""
    async with running(settings_for(database_url)) as app:
        for number in range(25):
            async with browser(app, f"198.51.100.{number}") as client:
                await sign_up(client, f"person{number}@example.com")


async def test_one_address_is_bounded_without_affecting_others(database_url: str) -> None:  # noqa: F811
    async with running(settings_for(database_url)) as app:
        service = cast(AccountService, app.state.accounts)
        for _ in range(SIGNUPS_PER_ADDRESS_PER_HOUR):
            service.limiter.record_signup("203.0.113.9")
        async with browser(app, "203.0.113.9") as busy:
            refused = await busy.post(
                "/api/account/register",
                json={"email": "late@example.com", "password": PASSWORD, "consent": True},
            )
            assert refused.status_code == 429
        async with browser(app, "203.0.113.10") as other:
            await sign_up(other, "fine@example.com")


async def test_failed_sign_ins_do_not_block_sign_ups(database_url: str) -> None:  # noqa: F811
    async with running(settings_for(database_url)) as app, browser(app) as client:
        for _ in range(60):
            await client.post(
                "/api/account/login", json={"email": "x@example.com", "password": "wrong guess"}
            )
        await sign_up(client, "new@example.com")


async def test_the_whole_site_has_a_sign_up_ceiling(tmp_path: Path) -> None:
    async with running(settings_for(f"sqlite:///{tmp_path / 'db.sqlite'}")) as app:
        service = cast(AccountService, app.state.accounts)
        for number in range(SIGNUPS_PER_HOUR):
            service.limiter.record_signup(f"10.{number // 250}.{number % 250}.1")
        async with browser(app, "192.0.2.77") as client:
            refused = await client.post(
                "/api/account/register",
                json={"email": "flood@example.com", "password": PASSWORD, "consent": True},
            )
            assert refused.status_code == 429


async def test_signing_in_replaces_any_planted_session(database_url: str) -> None:  # noqa: F811
    async with running(settings_for(database_url)) as app, browser(app) as client:
        await sign_up(client, "asha@example.com")
        planted = client.cookies.get("__Host-saathi_session")
        assert planted
        await client.post(
            "/api/account/login", json={"email": "asha@example.com", "password": PASSWORD}
        )
        async with browser(app) as attacker:
            attacker.cookies.set("__Host-saathi_session", planted)
            assert (await attacker.get("/api/account/me")).json()["user"] is None


# A demo that belongs to the person


async def test_three_languages_and_three_journeys_complete_for_a_signed_in_person(
    database_url: str,  # noqa: F811
) -> None:
    async with running(settings_for(database_url)) as app, browser(app) as client:
        await sign_up(client, "asha@example.com")
        for language, journeys in JOURNEYS.items():
            for capability, turns in journeys.items():
                reply = await converse(client, language, turns)
                assert reply["state"] == "DONE", (language, capability, reply["assistant_text"])
                assert reply["capability"] == capability
                assert reply["commerce_mode"] == "mock"
                assert reply["order_ids"]
        orders = (await client.get("/api/account/demo-orders")).json()
        assert orders["demo"] is True
        assert sorted(o["capability"] for o in orders["orders"]) == sorted(
            ["grocery", "food", "dining", "grocery", "grocery"]
        )
        assert (await client.get("/api/account/orders")).json()["orders"] == [], "never real"


async def test_mock_replies_never_claim_to_be_real(database_url: str) -> None:  # noqa: F811
    async with running(settings_for(database_url)) as app, browser(app) as client:
        await sign_up(client, "asha@example.com")
        for language, journeys in JOURNEYS.items():
            for turns in journeys.values():
                reply = await converse(client, language, turns)
                text = reply["assistant_text"]
                if language == "en-IN":
                    assert "demo" in text.casefold() or "not a real" in text.casefold(), text


async def test_each_persons_history_orders_and_language_are_their_own(
    database_url: str,  # noqa: F811
) -> None:
    async with (
        running(settings_for(database_url)) as app,
        browser(app, "203.0.113.1") as asha,
        browser(app, "203.0.113.2") as ravi,
    ):
        await sign_up(asha, "asha@example.com")
        await sign_up(ravi, "ravi@example.com")
        await converse(asha, "kn-IN", JOURNEYS["kn-IN"]["grocery"])
        await converse(ravi, "hi-IN", ("दो किलो चावल",))

        asha_me = (await asha.get("/api/account/me")).json()
        ravi_me = (await ravi.get("/api/account/me")).json()
        assert asha_me["preferences"]["language"] == "kn-IN"
        assert ravi_me["preferences"]["language"] == "hi-IN"

        asha_history = (await asha.get("/api/account/history")).json()["turns"]
        ravi_history = (await ravi.get("/api/account/history")).json()["turns"]
        assert any("ಅಕ್ಕಿ" in turn["said"] for turn in asha_history)
        assert not any("ಅಕ್ಕಿ" in turn["said"] for turn in ravi_history)
        assert any("चावल" in turn["said"] for turn in ravi_history)
        assert len((await asha.get("/api/account/demo-orders")).json()["orders"]) == 1
        assert (await ravi.get("/api/account/demo-orders")).json()["orders"] == []

        await ravi.post("/api/account/history/clear")
        assert (await ravi.get("/api/account/history")).json()["turns"] == []
        assert (await asha.get("/api/account/history")).json()["turns"], "untouched"


async def test_anonymous_conversations_are_never_stored(database_url: str) -> None:  # noqa: F811
    async with running(settings_for(database_url)) as app, browser(app) as client:
        await converse(client, "en-IN", JOURNEYS["en-IN"]["grocery"])
        service = cast(AccountService, app.state.accounts)
        with service.database.transaction() as tx:
            assert tx.execute("SELECT COUNT(*) AS n FROM conversation_turns")[0]["n"] == 0
            assert tx.execute("SELECT COUNT(*) AS n FROM demo_orders")[0]["n"] == 0


async def test_a_conversation_follows_the_person_to_another_device(
    database_url: str,  # noqa: F811
) -> None:
    async with running(settings_for(database_url)) as app:
        async with browser(app) as laptop:
            await sign_up(laptop, "asha@example.com")
            reply = await converse(laptop, "en-IN", ("Two kilos of rice and one litre of milk",))
            cookie = laptop.cookies.get("__Host-saathi_session")
        async with browser(app) as phone:
            assert (await phone.get("/api/session/current")).json() == {"session_id": None}
            await phone.post(
                "/api/account/login", json={"email": "asha@example.com", "password": PASSWORD}
            )
            current = (await phone.get("/api/session/current")).json()["session_id"]
            assert current == reply["session_id"]
            state = (await phone.get("/api/session", headers={"X-Session-ID": current})).json()
            assert state["state"] == "READBACK"
        async with browser(app) as stranger:
            assert (await stranger.get("/api/session/current")).json() == {"session_id": None}
        assert cookie


async def test_everything_but_the_unfinished_cart_survives_a_restart(
    database_url: str,  # noqa: F811
) -> None:
    settings = settings_for(database_url)
    async with running(settings) as app, browser(app) as client:
        await sign_up(client, "asha@example.com")
        await converse(client, "hi-IN", JOURNEYS["hi-IN"]["grocery"])
        unfinished = await converse(client, "hi-IN", ("दो किलो चावल",))
        cookie = client.cookies.get("__Host-saathi_session")
    async with running(settings) as app, browser(app) as client:
        assert cookie
        client.cookies.set("__Host-saathi_session", cookie)
        me = (await client.get("/api/account/me")).json()
        assert me["user"]["email"] == "asha@example.com"
        assert me["preferences"]["language"] == "hi-IN"
        assert len((await client.get("/api/account/demo-orders")).json()["orders"]) == 1
        assert (await client.get("/api/account/history")).json()["turns"]
        gone = await client.get("/api/session", headers={"X-Session-ID": unfinished["session_id"]})
        assert gone.status_code == 410, "the in-progress cart is held in memory, and says so"


async def test_history_is_bounded(tmp_path: Path) -> None:
    database = connect(f"sqlite:///{tmp_path / 'db.sqlite'}")
    migrate(database)
    store = AccountStore(database)
    user = store.create_user("asha@example.com", "x")
    for number in range(HISTORY_KEPT_TURNS + 15):
        store.add_turn(user.id, Turn(1_000_000 + number, "en-IN", "grocery", str(number), "ok"))
    kept = store.turns(user.id, 1000)
    assert len(kept) == HISTORY_KEPT_TURNS
    assert kept[-1].said == str(HISTORY_KEPT_TURNS + 14)


async def test_export_and_delete_cover_history_and_demo_orders(database_url: str) -> None:  # noqa: F811
    async with running(settings_for(database_url)) as app, browser(app) as client:
        await sign_up(client, "asha@example.com")
        await converse(client, "en-IN", JOURNEYS["en-IN"]["grocery"])
        exported = (await client.get("/api/account/export")).json()
        assert exported["conversation_history"]
        assert exported["demo_orders"][0]["capability"] == "grocery"
        assert exported["preferences"] == {"language": "en-IN"}
        await client.post("/api/account/delete", json={"password": PASSWORD})
        service = cast(AccountService, app.state.accounts)
        with service.database.transaction() as tx:
            for table in ("conversation_turns", "demo_orders", "user_preferences", "users"):
                assert tx.execute(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0, table


# Allowances


async def test_a_person_has_their_own_daily_allowance(database_url: str) -> None:  # noqa: F811
    settings = settings_for(database_url, user_daily_turn_limit=3)
    async with (
        running(settings) as app,
        browser(app, "203.0.113.1") as asha,
        browser(app, "203.0.113.2") as ravi,
    ):
        await sign_up(asha, "asha@example.com")
        await sign_up(ravi, "ravi@example.com")
        reply = await converse(asha, "en-IN", ("Rice", "Milk"))
        over = await asha.post(
            "/api/session/text",
            json={"text": "Yes"},
            headers={"X-Session-ID": reply["session_id"]},
        )
        assert over.status_code == 429
        await converse(ravi, "en-IN", ("Rice",))


async def test_the_whole_site_has_a_daily_ceiling(database_url: str) -> None:  # noqa: F811
    settings = settings_for(database_url, global_daily_turn_limit=2)
    async with running(settings) as app, browser(app) as client:
        await sign_up(client, "asha@example.com")
        await converse(client, "en-IN", ("Rice",))
        assert (await client.post("/api/sessions", json={"language": "en-IN"})).status_code == 429


async def test_signed_in_people_do_not_count_against_the_anonymous_cap(
    database_url: str,  # noqa: F811
) -> None:
    async with running(settings_for(database_url, public_max_sessions=1)) as app:
        async with browser(app) as visitor:
            assert (await visitor.post("/api/sessions", json={"language": "en-IN"})).is_success
            refused = await visitor.post("/api/sessions", json={"language": "en-IN"})
            assert refused.status_code == 429
        async with browser(app) as member:
            await sign_up(member, "asha@example.com")
            for _ in range(3):  # each replaces the person's previous conversation
                assert (await member.post("/api/sessions", json={"language": "en-IN"})).is_success
            sessions = cast(dict[str, Any], app.state.sessions)
            assert sum(entry.owner is not None for entry in sessions.values()) == 1


# The database going away


async def test_an_unreachable_database_leaves_the_demo_working() -> None:
    settings = settings_for("postgresql://u:p@127.0.0.1:9/nothing?connect_timeout=1")
    async with running(settings) as app, browser(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        ready = await client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["demo"] == "ok"
        reply = await converse(client, "en-IN", JOURNEYS["en-IN"]["grocery"])
        assert reply["state"] == "DONE"
        me = (await client.get("/api/account/me")).json()
        assert me["available"] is False
        assert "demo" in me["message"]
        refused = await client.post(
            "/api/account/register",
            json={"email": "a@example.com", "password": PASSWORD, "consent": True},
        )
        assert refused.status_code == 503
        assert "demo" in refused.json()["detail"]


async def test_accounts_come_back_by_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real_migrate = service_module.migrate

    def flaky(database: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("database is starting")
        real_migrate(database)

    monkeypatch.setattr(service_module, "migrate", flaky)
    monkeypatch.setattr(service_module, "RETRY_DATABASE_SECONDS", 0.0)
    async with (
        running(settings_for(f"sqlite:///{tmp_path / 'db.sqlite'}")) as app,
        browser(app) as client,
    ):
        await sign_up(client, "asha@example.com")
        assert (await client.get("/readyz")).status_code == 200
    assert calls["n"] == 2


@asynccontextmanager
async def cuttable_proxy(target_host: str, target_port: int) -> AsyncGenerator[Any]:
    """A TCP relay the test can cut and restore, to stand in for a database outage."""
    connections: set[asyncio.StreamWriter] = set()
    state = {"up": True}

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        writer.close()

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        if not state["up"]:
            client_w.close()
            return
        server_r, server_w = await asyncio.open_connection(target_host, target_port)
        connections.update({client_w, server_w})
        await asyncio.gather(pipe(client_r, server_w), pipe(server_r, client_w))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    class Control:
        def __init__(self) -> None:
            self.port = port

        def cut(self) -> None:
            state["up"] = False
            for writer in list(connections):
                writer.close()
            connections.clear()

        def restore(self) -> None:
            state["up"] = True

    try:
        yield Control()
    finally:
        server.close()


async def test_a_database_outage_mid_run_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needs a local Postgres in TEST_DATABASE_URL (CI has one). Hosted databases route by TLS
    server name, which a local relay cannot carry."""
    url = os.environ.get("TEST_DATABASE_URL", "")
    parts = urlsplit(url)
    if parts.hostname not in {"127.0.0.1", "localhost"}:
        pytest.skip("Needs a local Postgres in TEST_DATABASE_URL")
    guarded = socket.socket.connect

    def loopback_only(self: socket.socket, address: Any) -> None:
        # The relay must reach the local database; everything else stays blocked.
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] in {"127.0.0.1", "::1"}:
            _real_connect(self, address)
            return
        guarded(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    async with cuttable_proxy(parts.hostname or "", parts.port or 5432) as proxy:
        relayed = urlunsplit(
            parts._replace(netloc=f"{parts.username}:{parts.password}@127.0.0.1:{proxy.port}")
        )
        async with running(settings_for(relayed)) as app, browser(app) as client:
            database = cast(AccountService, app.state.accounts).database

            def empty() -> None:
                with database.transaction() as tx:
                    tx.execute("DELETE FROM users")

            # Off the event loop: the relay runs on it, so a blocking call here would deadlock.
            await asyncio.to_thread(empty)
            await sign_up(client, "asha@example.com")
            proxy.cut()
            down = await client.get("/api/account/me")
            assert down.json()["available"] is False
            reply = await converse(client, "en-IN", JOURNEYS["en-IN"]["grocery"])
            assert reply["state"] == "DONE", "the demo carried on during the outage"
            proxy.restore()
            started = time.monotonic()
            back: dict[str, Any] = {}
            while time.monotonic() - started < 60:
                back = (await client.get("/api/account/me")).json()
                if back.get("user"):
                    break
                await asyncio.sleep(1)
            print(f"accounts back {time.monotonic() - started:.1f} s after the database returned")
            assert back["user"]["email"] == "asha@example.com"


# Upgrading an existing database


async def test_a_first_version_database_is_upgraded_in_place(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.sqlite'}"
    database = connect(url)
    migrate(database)
    store = AccountStore(database)
    user = store.create_user("early@example.com", "x")
    with database.transaction() as tx:
        for table in ("demo_orders", "conversation_turns", "user_preferences"):
            tx.execute(f"DROP TABLE {table}")
        tx.execute("UPDATE schema_version SET version = 1")
    migrate(database)
    with database.transaction() as tx:
        assert tx.execute("SELECT version FROM schema_version")[0]["version"] == SCHEMA_VERSION
    assert store.user_by_id(user.id) is not None
    store.set_language(user.id, "kn-IN")
    assert store.language(user.id) == "kn-IN"
