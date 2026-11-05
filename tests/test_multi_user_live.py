"""Real adapters, real MCP SDK, real OAuth flow, against a stand-in Swiggy. Nothing here is real."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import httpx2
import pytest
from fastapi import FastAPI

from app.accounts.crypto import TokenCipher, TokenDecryptError, parse_keys
from app.accounts.db import connect, migrate
from app.accounts.journal import DatabaseOrderJournal
from app.accounts.service import AccountService, SwiggyStub
from app.commerce.base import CommerceAuthRequired, CommerceError
from app.commerce.order_journal import AttemptStatus, new_attempt, resolve_attempt
from app.commerce.swiggy import SWIGGY_STAGING_MCP_URL
from app.config import ConfigurationError, Settings, load_settings
from app.llm.scripted import ScriptedExtractor
from app.web import create_app
from tests.fake_swiggy import FakeSwiggy

BASE = "https://saathi.example"
CALLBACK = f"{BASE}/auth/callback"
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery"


class SilentTTS:
    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        del text
        destination = Path(output_path)
        destination.write_bytes(b"RIFF")
        return destination


@dataclass
class RecordingMailer:
    can_send: bool = True
    sent: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    async def send(self, to: str, subject: str, body: str) -> bool:
        self.sent.append((to, subject, body))
        return True

    def token(self, marker: str) -> str:
        body = self.sent[-1][2]
        return body.split(f"#{marker}=", maxsplit=1)[1].split()[0]


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'saathi.db'}"
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run against Postgres")
    database = connect(url)
    migrate(database)
    with database.transaction() as tx:
        tx.execute("DELETE FROM users")
        tx.execute("DELETE FROM oauth_clients")
    database.close()
    return url


def live_settings(database_url: str, **changes: Any) -> Settings:
    base = Settings(
        commerce_backend="swiggy",
        swiggy_live_access=True,
        swiggy_mcp_url=SWIGGY_STAGING_MCP_URL,
        multi_user_live=True,
        public_base_url=BASE,
        database_url=database_url,
        token_encryption_keys=KEY,
        allow_swiggy_cart_writes=True,
        allow_real_checkout=True,
        allow_real_booking=True,
        max_real_order_inr=None,
        real_order_limit=20,
    )
    return replace(base, **changes)


class Person:
    """One browser: its own cookie jar."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=world.app), base_url=BASE, follow_redirects=False
        )
        self.session = ""
        self.last: dict[str, Any] = {}
        self.email = ""

    async def register(self, email: str, password: str = PASSWORD) -> None:
        response = await self.http.post(
            "/api/account/register", json={"email": email, "password": password, "consent": True}
        )
        assert response.status_code == 200, response.text
        self.email = email

    async def approve(self) -> None:
        """Verified and invited, which is what an operator grants."""
        await self.http.post(
            "/api/account/verify", json={"token": self.world.mailer.token("verify")}
        )
        service = self.world.service
        user = service.store.user_by_email(self.email)
        assert user is not None
        service.store.set_live_access(user.id, True)

    async def connect_swiggy(self, account: str) -> httpx.Response:
        start = await self.http.get("/auth/login")
        assert start.status_code == 302, start.text
        location = start.headers["location"]
        assert location.startswith("https://fake-swiggy.example/auth/authorize?")
        async with self.world.swiggy_browser() as swiggy:
            consent = await swiggy.get(f"{location}&account={account}")
        assert consent.status_code == 302, consent.text
        back = urlsplit(consent.headers["location"])
        return await self.http.get(f"{back.path}?{back.query}")

    async def onboard(self, email: str, account: str) -> None:
        await self.register(email)
        await self.approve()
        done = await self.connect_swiggy(account)
        assert done.status_code == 303, done.text

    async def start(self, *, live: bool = True, language: str = "en-IN") -> dict[str, Any]:
        response = await self.http.post("/api/sessions", json={"language": language, "live": live})
        assert response.status_code == 200, response.text
        self.last = response.json()
        self.session = self.last["session_id"]
        return self.last

    def request_headers(self, heard: bool = True) -> dict[str, str]:
        headers = {"X-Session-ID": self.session}
        if heard and self.last.get("cart_hash"):
            headers["X-Heard-Cart-Version"] = str(self.last["cart_version"])
            headers["X-Heard-Cart-Hash"] = str(self.last["cart_hash"])
        return headers

    async def say(self, text: str, *, heard: bool = True) -> dict[str, Any]:
        response = await self.http.post(
            "/api/session/text", json={"text": text}, headers=self.request_headers(heard)
        )
        assert response.status_code == 200, response.text
        self.last = response.json()
        return self.last

    async def speak_until_readback(self, text: str) -> dict[str, Any]:
        """Say the request, then pick the first offered choice until Saathi reads it back."""
        reply = await self.say(text)
        for _ in range(4):
            if reply["state"] == "READBACK":
                return reply
            assert reply["options"], reply
            reply = await self.say("First")
        raise AssertionError(reply)


@dataclass
class World:
    app: FastAPI
    fake: FakeSwiggy
    mailer: RecordingMailer

    @property
    def service(self) -> AccountService:
        return cast(AccountService, self.app.state.accounts)

    def person(self) -> Person:
        return Person(self)

    @asynccontextmanager
    async def swiggy_browser(self) -> AsyncGenerator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=self.fake.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://fake-swiggy.example"
        ) as client:
            yield client


@asynccontextmanager
async def world(settings: Settings, fake: FakeSwiggy | None = None) -> AsyncGenerator[World]:
    swiggy = fake or FakeSwiggy({CALLBACK})
    mailer = RecordingMailer()
    stub = SwiggyStub(
        oauth_transport=httpx.ASGITransport(app=swiggy.app),
        mcp_transport=httpx2.ASGITransport(app=swiggy.app),
        browser_base="https://fake-swiggy.example",
    )
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
        mailer=mailer,
        swiggy_stub=stub,
    )
    async with app.router.lifespan_context(app):
        yield World(app, swiggy, mailer)


# Connecting


async def test_connecting_needs_a_signed_in_verified_invited_user(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        person = w.person()
        anonymous = await person.http.get("/auth/login")
        assert anonymous.status_code == 302
        assert anonymous.headers["location"] == "/#signin"

        await person.register("asha@example.com")
        assert (await person.http.get("/auth/login")).status_code == 403
        await person.approve()
        assert (await person.http.get("/auth/login")).status_code == 302
        assert w.fake.calls == []


async def test_a_connected_token_is_encrypted_and_not_readable_as_another_user(
    database_url: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        person = w.person()
        await person.onboard("asha@example.com", "asha")
        me = (await person.http.get("/api/account/me")).json()
        assert me["live"]["connected"] and me["live"]["available"]

        with w.service.database.transaction() as tx:
            rows = tx.execute("SELECT user_id, issuer, ciphertext FROM swiggy_tokens")
        assert len(rows) == 1
        ciphertext = cast(str, rows[0]["ciphertext"])
        assert "fake-" not in ciphertext
        cipher = TokenCipher(parse_keys(KEY))
        issuer = cast(str, rows[0]["issuer"])
        assert "fake-access" in cipher.decrypt(
            ciphertext, user_id=cast(str, rows[0]["user_id"]), issuer=issuer
        )
        with pytest.raises(TokenDecryptError):
            cipher.decrypt(ciphertext, user_id="someone-else", issuer=issuer)


async def test_a_token_copied_to_another_users_row_is_refused(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha, ravi = w.person(), w.person()
        await asha.onboard("asha@example.com", "asha")
        await ravi.register("ravi@example.com")
        await ravi.approve()
        store = w.service.store
        asha_user = store.user_by_email("asha@example.com")
        ravi_user = store.user_by_email("ravi@example.com")
        assert asha_user and ravi_user
        stolen = store.get_token(asha_user.id, w.service.issuer)
        assert stolen
        store.put_token(ravi_user.id, w.service.issuer, stolen.ciphertext, stolen.expires_at)

        assert not await w.service.tokens.connected(ravi_user.id)
        refused = await ravi.http.post("/api/sessions", json={"language": "en-IN", "live": True})
        assert refused.status_code == 403
        assert w.fake.tools_called() == []


async def test_a_callback_is_refused_for_a_different_signed_in_user(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha, mallory = w.person(), w.person()
        await asha.register("asha@example.com")
        await asha.approve()
        await mallory.register("mallory@example.com")
        await mallory.approve()
        start = await asha.http.get("/auth/login")
        async with w.swiggy_browser() as swiggy:
            consent = await swiggy.get(start.headers["location"] + "&account=asha")
        back = urlsplit(consent.headers["location"])
        # Same browser cookie, different signed-in user: this token must not land on mallory.
        cookie = asha.http.cookies.get("saathi_auth", domain="saathi.example.local", path="/auth")
        if cookie:
            mallory.http.cookies.set("saathi_auth", cookie, path="/auth")
        response = await mallory.http.get(f"{back.path}?{back.query}")
        assert response.status_code == 400
        user = w.service.store.user_by_email("mallory@example.com")
        assert user and not await w.service.tokens.connected(user.id)


async def test_a_forged_state_is_refused(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        person = w.person()
        await person.register("asha@example.com")
        response = await person.http.get("/auth/callback", params={"code": "x", "state": "forged"})
        assert response.status_code == 400


async def test_registration_is_refused_for_a_redirect_swiggy_has_not_allowlisted(
    database_url: str,
) -> None:
    fake = FakeSwiggy({"https://something-else.example/auth/callback"})
    async with world(live_settings(database_url), fake) as w:
        person = w.person()
        await person.register("asha@example.com")
        await person.approve()
        response = await person.http.get("/auth/login")
        assert response.status_code == 503
        assert "allowlisted" in response.text


async def test_dynamic_registration_survives_a_restart(database_url: str) -> None:
    fake = FakeSwiggy({CALLBACK})
    settings = live_settings(database_url)
    async with world(settings, fake) as w:
        person = w.person()
        await person.onboard("asha@example.com", "asha")
    async with world(settings, fake) as w:
        ravi = w.person()
        await ravi.onboard("ravi@example.com", "ravi")
    assert fake.registrations == 1


# Isolation


async def test_two_users_see_only_their_own_swiggy_data(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha, ravi = w.person(), w.person()
        await asha.onboard("asha@example.com", "asha")
        await ravi.onboard("ravi@example.com", "ravi")

        asha_addresses = (await asha.http.get("/api/addresses?live=1")).json()["addresses"]
        ravi_addresses = (await ravi.http.get("/api/addresses?live=1")).json()["addresses"]
        assert [a["full_address"] for a in asha_addresses] == ["12 CMH Road, Indiranagar"]
        assert [a["full_address"] for a in ravi_addresses] == ["9 Temple Street, Malleshwaram"]

        await asha.start()
        await ravi.start()
        reply = await asha.speak_until_readback("One litre of milk")
        assert "Malleshwaram" not in json.dumps(reply)
        assert w.fake.accounts["asha"].cart
        assert w.fake.accounts["ravi"].cart == []

        await asha.say("Yes")
        assert len(w.fake.accounts["asha"].orders) == 1
        assert w.fake.accounts["ravi"].orders == []
        assert (await ravi.http.get("/api/account/orders")).json()["orders"] == []
        assert len((await asha.http.get("/api/account/orders")).json()["orders"]) == 1


async def test_nobody_else_can_use_a_signed_in_users_conversation(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha, ravi, anonymous = w.person(), w.person(), w.person()
        await asha.onboard("asha@example.com", "asha")
        await ravi.register("ravi@example.com")
        await asha.start()
        stolen = {"X-Session-ID": asha.session}
        for other in (ravi, anonymous):
            assert (await other.http.get("/api/session", headers=stolen)).status_code == 410
            turn = await other.http.post("/api/session/text", json={"text": "Rice"}, headers=stolen)
            assert turn.status_code == 410
            action = await other.http.post(
                "/api/session/action", json={"action": "cancel"}, headers=stolen
            )
            assert action.status_code == 410
        assert w.fake.tools_called("ravi") == []
        assert "search_products" not in w.fake.tools_called("asha")


async def test_one_users_uncertain_order_blocks_only_that_user(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha, ravi = w.person(), w.person()
        await asha.onboard("asha@example.com", "asha")
        await ravi.onboard("ravi@example.com", "ravi")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        journal = w.service.journal(user.id)
        attempt = new_attempt("hash", "100", (), ("Rice",))
        journal.begin(attempt, lambda _: None)
        resolve_attempt(journal, attempt, AttemptStatus.UNKNOWN)

        blocked = await asha.start()
        assert blocked["state"] == "ORDER_UNKNOWN"
        await ravi.start()
        reply = await ravi.speak_until_readback("One litre of milk")
        assert reply["can_confirm"] is True
        await ravi.say("Yes")
        assert len(w.fake.accounts["ravi"].orders) == 1


async def test_a_second_live_conversation_ends_the_first(database_url: str) -> None:
    """A Swiggy account has one cart, so two tabs must not overwrite it."""
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        first = asha.session
        await asha.start()
        gone = await asha.http.get("/api/session", headers={"X-Session-ID": first})
        assert gone.status_code == 410
        assert (
            await asha.http.get("/api/session", headers=asha.request_headers())
        ).status_code == 200


# Modes never blur


async def test_anonymous_visitors_on_a_live_server_get_labelled_mock_shops(
    database_url: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        visitor = w.person()
        addresses = (await visitor.http.get("/api/addresses")).json()
        assert addresses["commerce_mode"] == "mock"
        reply = await visitor.start(live=False)
        assert reply["commerce_mode"] == "mock"
        reply = await visitor.say("Two kilos of rice and one litre of milk")
        reply = await visitor.say("Yes")
        assert reply["state"] == "DONE"
        assert reply["commerce_mode"] == "mock"
        assert "demo" in reply["assistant_text"].casefold()
        assert w.fake.calls == []


@pytest.mark.parametrize("state", ["anonymous", "unverified", "uninvited", "unconnected"])
async def test_asking_for_live_without_permission_is_refused_not_mocked(
    database_url: str, state: str
) -> None:
    async with world(live_settings(database_url)) as w:
        person = w.person()
        if state != "anonymous":
            await person.register("asha@example.com")
        if state == "uninvited":
            token = w.mailer.token("verify")
            await person.http.post("/api/account/verify", json={"token": token})
        if state == "unconnected":
            await person.approve()
        response = await person.http.post("/api/sessions", json={"language": "en-IN", "live": True})
        assert response.status_code == 403
        assert (await person.http.get("/api/addresses?live=1")).status_code == 403
        assert w.fake.calls == []


async def test_a_mock_server_refuses_live_requests_and_never_starts_oauth(tmp_path: Path) -> None:
    settings = Settings(
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
        token_encryption_keys=KEY,
    )
    async with world(settings) as w:
        person = w.person()
        await person.register("asha@example.com")
        assert (await person.http.get("/auth/login")).status_code == 403
        response = await person.http.post("/api/sessions", json={"language": "en-IN", "live": True})
        assert response.status_code == 403


# The three journeys, live path, stand-in Swiggy


async def test_grocery_order_end_to_end(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        started = await asha.start()
        assert started["commerce_mode"] == "swiggy"
        assert started["swiggy_environment"] == "staging"

        reply = await asha.speak_until_readback("Two kilos of rice and one litre of milk")
        assert reply["state"] == "READBACK"
        assert reply["can_confirm"] is True
        assert w.fake.accounts["asha"].orders == []
        assert "checkout" not in w.fake.tools_called()

        reply = await asha.say("Yes")
        assert reply["state"] == "DONE"
        assert reply["order_verified"] is True
        assert w.fake.tools_called().count("checkout") == 1
        order = w.fake.accounts["asha"].orders[0]
        assert reply["order_ids"] == [order["orderId"]]
        assert "demo" not in reply["assistant_text"].casefold()


async def test_a_spoken_yes_needs_the_readback_this_browser_received(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        reply = await asha.say("Yes", heard=False)
        assert reply["state"] != "DONE"
        assert "checkout" not in w.fake.tools_called()


async def test_food_order_end_to_end(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        ravi = w.person()
        await ravi.onboard("ravi@example.com", "ravi")
        await ravi.start()
        reply = await ravi.say("I want something for dinner")
        reply = await ravi.speak_until_readback("Chicken biryani")
        assert "Meghana" in json.dumps(reply["cart"])
        assert "place_food_order" not in w.fake.tools_called()
        reply = await ravi.say("Yes")
        assert reply["state"] == "DONE"
        assert w.fake.tools_called().count("place_food_order") == 1
        assert len(w.fake.accounts["ravi"].food_orders) == 1


async def test_table_booking_end_to_end(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        ravi = w.person()
        await ravi.onboard("ravi@example.com", "ravi")
        await ravi.start()
        reply = await ravi.speak_until_readback(
            "Book a table for two in Indiranagar tomorrow at 7 PM, Italian"
        )
        assert "book_table" not in w.fake.tools_called()
        reply = await ravi.say("Yes")
        assert reply["state"] == "DONE"
        assert w.fake.tools_called().count("book_table") == 1
        assert len(w.fake.accounts["ravi"].bookings) == 1
        arguments = next(c.arguments for c in w.fake.calls if c.tool == "book_table")
        assert arguments["itemId"].endswith("-47409"), "only the free deal"


# Switches


async def test_with_every_switch_off_nothing_changes_at_swiggy(database_url: str) -> None:
    settings = live_settings(
        database_url,
        allow_swiggy_cart_writes=False,
        allow_real_checkout=False,
        allow_real_booking=False,
    )
    async with world(settings) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.say("One litre of milk")
        await asha.say("First")
        await asha.say("Yes")
        written = {"update_cart", "clear_cart", "checkout", "update_food_cart", "book_table"}
        assert not written & set(w.fake.tools_called())
        assert w.fake.accounts["asha"].cart == []
        assert w.fake.accounts["asha"].orders == []


async def test_reads_and_cart_writes_on_with_checkout_off_never_orders(database_url: str) -> None:
    async with world(live_settings(database_url, allow_real_checkout=False)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        reply = await asha.speak_until_readback("One litre of milk")
        assert reply["can_confirm"] is False
        reply = await asha.say("Yes")
        assert "checkout" not in w.fake.tools_called()
        assert w.fake.accounts["asha"].orders == []


async def test_daily_limit_applies_per_user(database_url: str) -> None:
    async with world(live_settings(database_url, real_order_daily_limit=1)) as w:
        asha, ravi = w.person(), w.person()
        await asha.onboard("asha@example.com", "asha")
        await ravi.onboard("ravi@example.com", "ravi")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        assert (await asha.say("Yes"))["state"] == "DONE"

        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        await asha.say("Yes")
        assert w.fake.tools_called("asha").count("checkout") == 1, "the limit held"

        await ravi.start()
        await ravi.speak_until_readback("One litre of milk")
        assert (await ravi.say("Yes"))["state"] == "DONE", "another user's limit is untouched"


# Failures


async def test_a_lost_checkout_reply_finds_the_order_and_never_orders_twice(
    database_url: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        w.fake.fail_next("checkout", "lost_after_effect")
        reply = await asha.say("Yes")
        assert w.fake.tools_called().count("checkout") == 1
        assert len(w.fake.accounts["asha"].orders) == 1
        assert reply["state"] == "DONE"
        assert reply["order_ids"] == [w.fake.accounts["asha"].orders[0]["orderId"]]
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        attempts = w.service.journal(user.id).attempts()
        assert attempts[0].status is AttemptStatus.PLACED


async def test_a_lost_booking_reply_stays_unknown_and_blocks(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        ravi = w.person()
        await ravi.onboard("ravi@example.com", "ravi")
        await ravi.start()
        await ravi.speak_until_readback(
            "Book a table for two in Indiranagar tomorrow at 7 PM, Italian"
        )
        w.fake.fail_next("book_table", "lost_after_effect")
        await ravi.say("Yes")
        assert w.fake.tools_called().count("book_table") == 1
        again = await ravi.start()
        assert again["state"] == "ORDER_UNKNOWN"


async def test_a_refused_checkout_is_reported_and_not_recorded_as_placed(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        w.fake.fail_next("checkout", "refuse:Item out of stock")
        reply = await asha.say("Yes")
        assert reply["state"] != "DONE"
        assert w.fake.tools_called().count("checkout") == 1
        assert w.fake.accounts["asha"].orders == []


async def test_rate_limit_is_honoured_without_hammering(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        w.fake.fail_next("search_products", "http_429")
        await asha.say("One litre of milk")
        calls = w.fake.tools_called().count("search_products")
        await asha.say("One litre of milk")
        assert w.fake.tools_called().count("search_products") == calls


# Tokens over time


async def test_an_expiring_token_is_refreshed_before_use_and_rotates(database_url: str) -> None:
    fake = FakeSwiggy({CALLBACK}, access_lifetime=30)  # inside the 60 s refresh margin
    async with world(live_settings(database_url), fake) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        assert fake.refreshes >= 1
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        first = await w.service.tokens.access_token(user.id)
        refreshes = fake.refreshes
        second = await w.service.tokens.access_token(user.id)
        assert fake.refreshes == refreshes + 1, "rotated token was saved and used"
        assert first != second


async def test_a_401_mid_session_refreshes_once_and_the_call_succeeds(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        w.fake.fail_next("search_products", "http_401")
        reply = await asha.say("One litre of milk")
        assert w.fake.refreshes == 1
        assert reply["options"] or reply["state"] == "READBACK"


async def test_a_revoked_login_asks_to_reconnect_quickly_and_places_nothing(
    database_url: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        w.fake.revoke("asha")
        reply = await asha.say("Yes")
        assert reply["state"] != "DONE"
        assert "checkout" not in w.fake.tools_called()
        me = (await asha.http.get("/api/account/me")).json()
        assert me["live"]["connected"] is False
        assert "Connect" in me["live"]["reason"]
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        unresolved = [
            a
            for a in w.service.journal(user.id).attempts()
            if a.status in {AttemptStatus.PENDING, AttemptStatus.UNKNOWN}
        ]
        assert unresolved == [], "a refused login cannot have placed an order"


async def test_a_session_revoked_during_a_call_is_a_login_problem(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        adapters = w.service.adapters(cast(Any, w.service.store.user_by_email(asha.email)).id)
        await adapters.grocery.get_addresses()
        w.fake.fail_next("get_cart", "http_419")
        with pytest.raises(CommerceAuthRequired):
            await adapters.grocery.get_cart()
        await adapters.aclose()


async def test_a_refresh_outage_keeps_the_login(database_url: str) -> None:
    fake = FakeSwiggy({CALLBACK}, access_lifetime=30)
    async with world(live_settings(database_url), fake) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        fake.refresh_down = True
        with pytest.raises(CommerceError) as caught:
            await w.service.tokens.access_token(user.id)
        assert not isinstance(caught.value, CommerceAuthRequired)
        assert w.service.store.get_token(user.id, w.service.issuer) is not None


async def test_disconnecting_revokes_at_swiggy_and_ends_the_conversation(database_url: str) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        response = await asha.http.post("/auth/logout")
        assert response.json() == {"connected": False}
        assert w.fake.logouts == ["asha"]
        gone = await asha.http.get("/api/session", headers=asha.request_headers())
        assert gone.status_code == 410
        user = w.service.store.user_by_email("asha@example.com")
        assert user and w.service.store.get_token(user.id, w.service.issuer) is None


async def test_deleting_an_account_revokes_at_swiggy_and_removes_everything(
    database_url: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        await asha.say("Yes")
        refused = await asha.http.post("/api/account/delete", json={"password": "wrong password"})
        assert refused.status_code == 401
        done = await asha.http.post("/api/account/delete", json={"password": PASSWORD})
        assert done.status_code == 200
        assert w.fake.logouts == ["asha"]
        with w.service.database.transaction() as tx:
            for table in ("users", "auth_sessions", "swiggy_tokens", "order_attempts"):
                count = tx.execute(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
                assert count == 0, table


# Logs and limits


async def test_tokens_addresses_and_emails_stay_out_of_the_logs(
    database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        with caplog.at_level("DEBUG"):
            await asha.onboard("asha@example.com", "asha")
            await asha.start()
            await asha.speak_until_readback("One litre of milk")
            await asha.say("Yes")
        text = caplog.text
        for secret in ("fake-access", "fake-refresh", "CMH Road", "asha@example.com", PASSWORD):
            assert secret not in text, secret
        calls = [line for line in text.splitlines() if "mcp_tool_call" in line]
        assert calls
        assert all("sha256:" in line and "correlation_id" in line for line in calls)


async def test_revoking_live_access_ends_the_conversation_on_its_next_turn(
    database_url: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        w.service.store.set_live_access(user.id, False)
        refused = await asha.http.post(
            "/api/session/text", json={"text": "Yes"}, headers=asha.request_headers()
        )
        assert refused.status_code == 410
        assert "checkout" not in w.fake.tools_called()


async def test_a_busy_anonymous_demo_does_not_lock_out_signed_in_people(
    database_url: str,
) -> None:
    settings = live_settings(database_url, public_daily_turn_limit=1, public_deployment=True)
    async with world(settings) as w:
        visitor, member = w.person(), w.person()
        await visitor.start(live=False)  # spends the whole anonymous allowance
        refused = await visitor.http.post(
            "/api/sessions", json={"language": "en-IN", "live": False}
        )
        assert refused.status_code == 429
        await member.register("member@example.com")
        started = await member.start(live=False)
        assert started["commerce_mode"] == "mock"
        reply = await member.say("Two kilos of rice and one litre of milk")
        assert reply["state"] == "READBACK"


# The journal on a real database


async def test_journal_guard_is_atomic_and_survives_a_restart(database_url: str) -> None:
    database = connect(database_url)
    migrate(database)
    from app.accounts.store import AccountStore

    store = AccountStore(database)
    user = store.create_user("journal@example.com", "x")
    journal = DatabaseOrderJournal(database, user.id)
    attempt = new_attempt("hash", "100", ("old",), ("Rice",), domain="food")
    journal.begin(attempt, lambda _: None)

    class Refused(Exception):
        pass

    def refuse(_: object) -> None:
        raise Refused

    with pytest.raises(Refused):
        journal.begin(new_attempt("hash2", "5", (), ()), refuse)
    assert [a.attempt_id for a in journal.attempts()] == [attempt.attempt_id]

    database.close()
    reopened = connect(database_url)
    migrate(reopened)  # applying twice changes nothing
    loaded = DatabaseOrderJournal(reopened, user.id).attempts()
    assert loaded[0].baseline_order_ids == ("old",)
    assert loaded[0].domain == "food"
    assert DatabaseOrderJournal(reopened, "someone-else").attempts() == []
    reopened.close()


# Configuration


def _live_env(**extra: str) -> dict[str, str]:
    return {
        "PUBLIC_DEPLOYMENT": "true",
        "PUBLIC_BASE_URL": BASE,
        "COMMERCE_BACKEND": "swiggy",
        "SWIGGY_LIVE_ACCESS": "true",
        "MULTI_USER_LIVE": "true",
        "DATABASE_URL": "postgresql://u:p@db.example/saathi?sslmode=require",
        "TOKEN_ENCRYPTION_KEYS": KEY,
        **extra,
    }


def test_a_complete_live_configuration_loads_with_every_real_switch_off() -> None:
    settings = load_settings(_live_env())
    assert settings.multi_user_live
    assert not settings.allow_real_checkout
    assert not settings.allow_swiggy_cart_writes
    assert not settings.allow_real_booking


@pytest.mark.parametrize(
    "change",
    [
        {"DATABASE_URL": ""},
        {"DATABASE_URL": "sqlite:////tmp/x.db"},
        {"DATABASE_URL": "postgresql://u:p@db.example/saathi"},
        {"DATABASE_URL": "mysql://u:p@db.example/saathi"},
        {"TOKEN_ENCRYPTION_KEYS": ""},
        {"TOKEN_ENCRYPTION_KEYS": "too-short"},
        {"PUBLIC_BASE_URL": "http://saathi.example"},
        {"COMMERCE_BACKEND": "mock", "SWIGGY_LIVE_ACCESS": "false"},
        {"SWIGGY_MCP_URL": "https://evil.example/im"},
        {"ALLOW_REAL_CHECKOUT": "true"},  # still needs writes on and a spending limit
        {"DEMO_UNDERSTANDING": "scripted"},
    ],
)
def test_an_unsafe_live_configuration_refuses_to_start(change: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(_live_env(**change))


def test_secrets_stay_out_of_settings_repr() -> None:
    assert KEY not in repr(load_settings(_live_env()))
    assert "u:p@" not in repr(load_settings(_live_env()))
