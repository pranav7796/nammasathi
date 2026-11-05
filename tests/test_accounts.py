"""Accounts: registration, sign-in, recovery, and the web security around them."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.accounts.crypto import (
    TokenCipher,
    TokenDecryptError,
    hash_password,
    parse_keys,
    verify_password,
)
from app.accounts.service import MAX_FAILURES_PER_ACCOUNT, AccountService
from app.config import Settings, load_settings
from app.llm.scripted import ScriptedExtractor
from app.web import create_app

BASE = "https://saathi.example"
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery"
EMAIL = "asha@example.com"


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
        return self.sent[-1][2].split(f"#{marker}=", maxsplit=1)[1].split()[0]


@dataclass
class Site:
    app: FastAPI
    mailer: RecordingMailer

    @property
    def service(self) -> AccountService:
        return cast(AccountService, self.app.state.accounts)

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)


@asynccontextmanager
async def site(
    tmp_path: Path, *, can_send: bool = True, open_registration: bool = True
) -> AsyncGenerator[Site]:
    settings = Settings(
        demo_understanding="scripted",
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'saathi.db'}",
        token_encryption_keys=KEY,
        open_registration=open_registration,
        support_email="help@example.com",
    )
    mailer = RecordingMailer(can_send=can_send)
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
        mailer=mailer,
    )
    async with app.router.lifespan_context(app):
        yield Site(app, mailer)


async def register(browser: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    return await browser.post(
        "/api/account/register", json={"email": email, "password": PASSWORD, "consent": True}
    )


# Registration


async def test_register_signs_in_with_a_hardened_cookie(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        response = await register(browser)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-saathi_session=")
        for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            assert attribute.lower() in cookie.lower()
        me = (await browser.get("/api/account/me")).json()
        assert me["user"]["email"] == EMAIL
        assert me["user"]["email_verified"] is False
        assert me["user"]["live_access"] is False
        assert "password" not in str(me)


@pytest.mark.parametrize(
    ("email", "password", "consent"),
    [
        ("not-an-email", PASSWORD, True),
        (EMAIL, "short", True),
        (EMAIL, "aaaaaaaaaaaa", True),
        (EMAIL, "1234567890", True),
        (EMAIL, EMAIL, True),
        (EMAIL, PASSWORD, False),
    ],
)
async def test_bad_registrations_are_refused(
    tmp_path: Path, email: str, password: str, consent: bool
) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        response = await browser.post(
            "/api/account/register",
            json={"email": email, "password": password, "consent": consent},
        )
        assert response.status_code == 422
        assert (await browser.get("/api/account/me")).json()["user"] is None


async def test_email_is_unique_regardless_of_case(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        again = await register(s.browser(), "  ASHA@Example.com ")
        assert again.status_code == 409


async def test_registration_can_be_closed(tmp_path: Path) -> None:
    async with site(tmp_path, open_registration=False) as s, s.browser() as browser:
        assert (await register(browser)).status_code == 403


async def test_passwords_and_session_tokens_are_never_stored_plainly(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        response = await register(browser)
        session = response.cookies["__Host-saathi_session"]
        raw = (tmp_path / "saathi.db").read_bytes()
        assert PASSWORD.encode() not in raw
        assert session.encode() not in raw
        assert b"scrypt$" in raw


# Sign-in


async def test_sign_in_and_out(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as first:
        await register(first)
        async with s.browser() as second:
            ok = await second.post(
                "/api/account/login", json={"email": EMAIL.upper(), "password": PASSWORD}
            )
            assert ok.status_code == 200
            assert (await second.get("/api/account/me")).json()["user"]["email"] == EMAIL
            await second.post("/api/account/logout")
            assert (await second.get("/api/account/me")).json()["user"] is None
        assert (await first.get("/api/account/me")).json()["user"]["email"] == EMAIL


async def test_a_wrong_password_and_an_unknown_email_look_the_same(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        wrong = await s.browser().post(
            "/api/account/login", json={"email": EMAIL, "password": "not the password"}
        )
        unknown = await s.browser().post(
            "/api/account/login", json={"email": "nobody@example.com", "password": "whatever long"}
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()


async def test_repeated_guessing_is_stopped_even_with_the_right_password(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        for _ in range(MAX_FAILURES_PER_ACCOUNT):
            await browser.post("/api/account/login", json={"email": EMAIL, "password": "nope nope"})
        blocked = await browser.post(
            "/api/account/login", json={"email": EMAIL, "password": PASSWORD}
        )
        assert blocked.status_code == 429


async def test_a_forged_session_cookie_is_nobody(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        browser.cookies.set("__Host-saathi_session", "forged-value")
        assert (await browser.get("/api/account/me")).json()["user"] is None


# Account management


async def test_changing_a_password_signs_out_other_browsers(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as here, s.browser() as elsewhere:
        await register(here)
        await elsewhere.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        wrong = await here.post(
            "/api/account/password",
            json={"current_password": "wrong one", "new_password": "a brand new phrase"},
        )
        assert wrong.status_code == 401
        done = await here.post(
            "/api/account/password",
            json={"current_password": PASSWORD, "new_password": "a brand new phrase"},
        )
        assert done.status_code == 200
        assert (await here.get("/api/account/me")).json()["user"] is not None
        assert (await elsewhere.get("/api/account/me")).json()["user"] is None


async def test_sign_out_everywhere(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as here, s.browser() as elsewhere:
        await register(here)
        await elsewhere.post("/api/account/login", json={"email": EMAIL, "password": PASSWORD})
        await here.post("/api/account/logout-everywhere")
        assert (await here.get("/api/account/me")).json()["user"] is None
        assert (await elsewhere.get("/api/account/me")).json()["user"] is None


async def test_email_verification_works_once_and_stays_out_of_the_server_address(
    tmp_path: Path,
) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        assert f"{BASE}/#verify=" in s.mailer.sent[-1][2]
        token = s.mailer.token("verify")
        assert (await browser.post("/api/account/verify", json={"token": token})).status_code == 200
        assert (await browser.get("/api/account/me")).json()["user"]["email_verified"] is True
        assert (await browser.post("/api/account/verify", json={"token": token})).status_code == 400


async def test_password_reset_works_once_and_ends_sessions(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        await browser.post("/api/account/forgot", json={"email": EMAIL})
        token = s.mailer.token("reset")
        weak = await browser.post("/api/account/reset", json={"token": token, "password": "short"})
        assert weak.status_code == 422
        await browser.post("/api/account/forgot", json={"email": EMAIL})
        token = s.mailer.token("reset")
        new = "a brand new phrase"
        done = await browser.post("/api/account/reset", json={"token": token, "password": new})
        assert done.status_code == 200
        assert (await browser.get("/api/account/me")).json()["user"] is None
        used = await browser.post("/api/account/reset", json={"token": token, "password": new})
        assert used.status_code == 400
        ok = await browser.post("/api/account/login", json={"email": EMAIL, "password": new})
        assert ok.status_code == 200
        old = await s.browser().post(
            "/api/account/login", json={"email": EMAIL, "password": PASSWORD}
        )
        assert old.status_code == 401


async def test_forgot_password_does_not_reveal_who_has_an_account(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        count = len(s.mailer.sent)
        known = await browser.post("/api/account/forgot", json={"email": EMAIL})
        unknown = await browser.post("/api/account/forgot", json={"email": "no@example.com"})
        assert known.json() == unknown.json()
        assert len(s.mailer.sent) == count + 1


async def test_without_email_the_page_is_told_the_truth(tmp_path: Path) -> None:
    async with site(tmp_path, can_send=False) as s, s.browser() as browser:
        await register(browser)
        assert s.mailer.sent == []
        forgot = (await browser.post("/api/account/forgot", json={"email": EMAIL})).json()
        assert forgot == {"status": "unavailable", "support_email": "help@example.com"}
        me = (await browser.get("/api/account/me")).json()
        assert me["email_available"] is False


async def test_export_holds_only_this_persons_data(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        exported = (await browser.get("/api/account/export")).json()
        assert exported["account"]["email"] == EMAIL
        assert exported["swiggy_connected"] is False
        assert exported["order_attempts"] == []
        assert "password" not in str(exported)
        assert (await s.browser().get("/api/account/export")).status_code == 401


async def test_deleting_an_account_frees_the_email_and_needs_the_password(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        wrong = await browser.post("/api/account/delete", json={"password": "wrong password"})
        assert wrong.status_code == 401
        done = await browser.post("/api/account/delete", json={"password": PASSWORD})
        assert done.status_code == 200
        assert (await browser.get("/api/account/me")).json()["user"] is None
        assert (await register(s.browser())).status_code == 200


# Conversations and accounts


async def test_the_demo_needs_no_account(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        started = await browser.post("/api/sessions", json={"language": "en-IN"})
        assert started.status_code == 200
        assert started.json()["commerce_mode"] == "mock"


async def test_signing_out_ends_that_persons_conversations(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        started = (await browser.post("/api/sessions", json={"language": "en-IN"})).json()
        headers = {"X-Session-ID": started["session_id"]}
        assert (await browser.get("/api/session", headers=headers)).status_code == 200
        await browser.post("/api/account/logout")
        assert (await browser.get("/api/session", headers=headers)).status_code == 410


async def test_mock_orders_never_appear_as_real_orders(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await register(browser)
        started = (await browser.post("/api/sessions", json={"language": "en-IN"})).json()
        headers = {"X-Session-ID": started["session_id"]}
        for text in ("Two kilos of rice and one litre of milk", "Yes"):
            await browser.post("/api/session/text", json={"text": text}, headers=headers)
        assert (await browser.get("/api/account/orders")).json()["orders"] == []


# Web security


async def test_cross_site_writes_are_refused(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        evil = {"Origin": "https://evil.example"}
        payload = {"email": EMAIL, "password": PASSWORD}
        refused = await browser.post("/api/account/login", json=payload, headers=evil)
        assert refused.status_code == 403
        fetched = await browser.post(
            "/api/account/logout", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert fetched.status_code == 403
        same = await browser.post("/api/account/logout", headers={"Origin": BASE})
        assert same.status_code == 200


async def test_oversized_requests_are_refused(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        response = await browser.post(
            "/api/account/login",
            content=b"x" * 70_000,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


async def test_sign_ups_from_one_address_are_bounded(tmp_path: Path) -> None:
    from app.accounts.service import SIGNUPS_PER_ADDRESS_PER_HOUR

    async with site(tmp_path) as s, s.browser() as browser:
        for _ in range(SIGNUPS_PER_ADDRESS_PER_HOUR):
            s.service.limiter.record_signup("127.0.0.1")
        assert (await register(browser)).status_code == 429


async def test_security_headers_are_on_every_response(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        for path in ("/", "/privacy", "/api/account/me", "/healthz"):
            headers = (await browser.get(path)).headers
            assert "script-src 'self'" in headers["content-security-policy"]
            assert "frame-ancestors 'none'" in headers["content-security-policy"]
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert "max-age" in headers["strict-transport-security"]
        assert (await browser.get("/api/account/me")).headers["cache-control"] == "no-store"


async def test_the_page_runs_under_its_own_content_security_policy() -> None:
    """No inline script or style, which the policy would block."""
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    for name in ("index.html", "privacy.html"):
        text = (root / name).read_text(encoding="utf-8")
        assert 'style="' not in text, name
        assert "<style" not in text, name
        assert "<script>" not in text, name
        assert " onclick=" not in text, name


async def test_readiness_reports_the_database(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        body = (await browser.get("/readyz")).json()
        assert body["status"] == "ok"
        assert body["database"] == "sqlite"


async def test_privacy_page_escapes_its_contact(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        page = (await browser.get("/privacy")).text
        assert "help@example.com" in page
        assert "{{contact}}" not in page


async def test_accounts_stay_off_without_a_database() -> None:
    app = create_app(
        settings=Settings(demo_understanding="scripted"),
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as browser:
            assert (await browser.get("/api/account/me")).json()["enabled"] is False
            refused = await browser.post(
                "/api/account/login", json={"email": EMAIL, "password": PASSWORD}
            )
            assert refused.status_code == 404
            live = await browser.post("/api/sessions", json={"language": "en-IN", "live": True})
            assert live.status_code == 403


# Primitives


def test_password_hash_round_trip_and_uniqueness() -> None:
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert not verify_password("something else", first)
    assert not verify_password(PASSWORD, "garbage")


def test_token_cipher_rotates_keys_and_binds_to_the_user() -> None:
    old_key = base64.urlsafe_b64encode(b"o" * 32).decode()
    old = TokenCipher(parse_keys(old_key))
    sealed = old.encrypt("secret", user_id="u1", issuer="i")
    rotated = TokenCipher(parse_keys(f"{KEY},{old_key}"))
    assert rotated.decrypt(sealed, user_id="u1", issuer="i") == "secret"
    resealed = rotated.encrypt("secret", user_id="u1", issuer="i")
    with pytest.raises(TokenDecryptError):
        old.decrypt(resealed, user_id="u1", issuer="i")
    with pytest.raises(TokenDecryptError):
        rotated.decrypt(sealed, user_id="u2", issuer="i")
    with pytest.raises(TokenDecryptError):
        rotated.decrypt(sealed, user_id="u1", issuer="another")


def test_settings_require_a_key_with_a_database() -> None:
    from app.config import ConfigurationError

    with pytest.raises(ConfigurationError):
        load_settings({"DATABASE_URL": "sqlite:///x.db"})
    loaded = load_settings({"DATABASE_URL": "sqlite:///x.db", "TOKEN_ENCRYPTION_KEYS": KEY})
    assert loaded.accounts_enabled
    assert not load_settings({}).accounts_enabled
