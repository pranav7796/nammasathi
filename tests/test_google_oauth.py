"""Google OAuth: consent URL, state, code exchange, encrypted storage, refresh, disconnect."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.accounts.service import AccountService
from app.config import Settings
from app.google import oauth as oauth_module
from app.google.oauth import (
    SCOPES,
    AccessDenied,
    ConnectionStore,
    ExchangeFailed,
    GoogleOAuth,
    InvalidState,
    UnknownService,
    pkce_challenge,
)
from app.google.routes import google_router
from app.integrations.base import NotConnected, ReauthRequired
from app.llm.scripted import ScriptedExtractor
from app.web import create_app
from tests.fake_google import FakeGoogle
from tests.test_accounts import KEY, PASSWORD, SilentTTS

BASE = "https://saathi.example"


def settings_for(tmp_path: Path, *, google: bool = True, database: bool = True) -> Settings:
    return Settings(
        demo_understanding="scripted",
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'saathi.db'}" if database else None,
        token_encryption_keys=KEY,
        open_registration=True,
        google_client_id="client-id.apps.example" if google else "",
        google_client_secret="client-secret-value" if google else None,
    )


@dataclass
class GoogleSite:
    app: FastAPI
    fake: FakeGoogle
    oauth: GoogleOAuth

    @property
    def service(self) -> AccountService:
        return cast(AccountService, self.app.state.accounts)

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    async def user_id(self, email: str) -> str:
        user = await self.service.run(self.service.store.user_by_email, email)
        assert user is not None
        return user.id

    async def register(self, browser: httpx.AsyncClient, email: str) -> str:
        response = await browser.post(
            "/api/account/register", json={"email": email, "password": PASSWORD, "consent": True}
        )
        assert response.status_code == 200
        return await self.user_id(email)

    async def rows(self) -> list[dict[str, object]]:
        def read(service: AccountService) -> list[dict[str, object]]:
            with service.database.transaction() as tx:
                return [dict(r) for r in tx.execute("SELECT * FROM connections")]

        return await self.service.run(read, self.service)


@asynccontextmanager
async def google_site(
    tmp_path: Path, *, google: bool = True, fake: FakeGoogle | None = None
) -> AsyncGenerator[GoogleSite]:
    settings = settings_for(tmp_path, google=google)
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
    )
    app.include_router(google_router())
    fake = fake or FakeGoogle()
    async with app.router.lifespan_context(app):
        service = cast(AccountService, app.state.accounts)
        oauth = GoogleOAuth(settings, ConnectionStore(service), transport=fake.transport())
        app.state.google = oauth
        yield GoogleSite(app, fake, oauth)


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def connect(s: GoogleSite, user_id: str, service: str = "gmail") -> str:
    url = s.oauth.build_authorization_url(user_id, service)
    s.fake.start_auth(url)
    await s.oauth.handle_callback(user_id, "good-code", state_of(url))
    return url


# Authorization URL


async def test_url_has_pkce_offline_consent_and_exact_minimum_scopes(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = s.oauth.build_authorization_url(uid, "gmail")
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert query["response_type"] == "code"
        assert query["access_type"] == "offline"
        assert query["prompt"] == "consent"
        assert query["code_challenge_method"] == "S256"
        assert query["redirect_uri"] == f"{BASE}/api/google/callback"
        assert query["scope"].split() == [
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ]
        calendar = s.oauth.build_authorization_url(uid, "calendar")
        assert parse_qs(urlsplit(calendar).query)["scope"][0].split() == [
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.events",
        ]
        assert SCOPES["calendar"] == ["https://www.googleapis.com/auth/calendar.events"]
        with pytest.raises(UnknownService):
            s.oauth.build_authorization_url(uid, "drive")


def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636 appendix B
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    digest = hashlib.sha256(verifier.encode()).digest()
    assert pkce_challenge(verifier) == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def test_exchange_sends_the_verifier_matching_the_challenge(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = await connect(s, uid)
        challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
        sent = s.fake.token_requests[0]
        assert pkce_challenge(sent["code_verifier"]) == challenge
        assert sent["grant_type"] == "authorization_code"
        assert sent["redirect_uri"] == f"{BASE}/api/google/callback"


# State


async def test_another_users_state_is_rejected_and_burned(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b, s.browser() as b2:
        a = await s.register(b, "a@example.com")
        other = await s.register(b2, "b@example.com")
        url = s.oauth.build_authorization_url(a, "gmail")
        with pytest.raises(InvalidState):
            await s.oauth.handle_callback(other, "good-code", state_of(url))
        with pytest.raises(InvalidState):  # burned: even the owner cannot use it now
            await s.oauth.handle_callback(a, "good-code", state_of(url))
        assert s.fake.token_requests == []


async def test_expired_state_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = s.oauth.build_authorization_url(uid, "gmail")
        real = oauth_module.time.time
        monkeypatch.setattr(oauth_module.time, "time", lambda: real() + 601)
        with pytest.raises(InvalidState):
            await s.oauth.handle_callback(uid, "good-code", state_of(url))
        assert s.fake.token_requests == []


async def test_replayed_and_unknown_state_are_rejected(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = await connect(s, uid)
        with pytest.raises(InvalidState):
            await s.oauth.handle_callback(uid, "good-code", state_of(url))
        with pytest.raises(InvalidState):
            await s.oauth.handle_callback(uid, "good-code", "made-up")


async def test_denied_consent_stores_nothing(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = s.oauth.build_authorization_url(uid, "gmail")
        with pytest.raises(AccessDenied):
            await s.oauth.handle_callback(uid, "", state_of(url), "access_denied")
        assert await s.rows() == []
        assert s.fake.token_requests == []


async def test_bad_code_is_an_exchange_failure(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = s.oauth.build_authorization_url(uid, "gmail")
        with pytest.raises(ExchangeFailed):
            await s.oauth.handle_callback(uid, "bad-code", state_of(url))
        assert await s.rows() == []


async def test_missing_scope_is_refused_and_revoked(tmp_path: Path) -> None:
    fake = FakeGoogle(scope_granted="openid email https://www.googleapis.com/auth/gmail.readonly")
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        url = s.oauth.build_authorization_url(uid, "gmail")
        fake.start_auth(url)
        with pytest.raises(ExchangeFailed):
            await s.oauth.handle_callback(uid, "good-code", state_of(url))
        assert await s.rows() == []
        assert fake.revoked == ["refresh-1"]


# Storage


async def test_row_is_ciphertext_labelled_and_without_token_text(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        (row,) = await s.rows()
        assert row["provider"] == "gmail"
        assert row["account_label"] == "me@example.com"
        everything = " ".join(str(v) for v in row.values())
        assert "refresh-1" not in everything
        assert "access-1" not in everything
        assert "gmail.readonly" in str(row["scopes"])


async def test_row_copied_to_another_user_cannot_be_opened(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b, s.browser() as b2:
        a = await s.register(b, "a@example.com")
        other = await s.register(b2, "b@example.com")
        await connect(s, a)
        (row,) = await s.rows()
        await s.oauth.tokens._store.put(  # pyright: ignore[reportPrivateUsage]
            other, "gmail", str(row["ciphertext"]), "x", "x"
        )
        with pytest.raises(ReauthRequired):
            await s.oauth.tokens.access_token(other, "gmail")
        assert s.fake.token_requests[-1]["grant_type"] == "authorization_code"  # never refreshed
        # and a row copied to another provider fails the same way
        await s.oauth.tokens._store.put(  # pyright: ignore[reportPrivateUsage]
            a, "calendar", str(row["ciphertext"]), "x", "x"
        )
        with pytest.raises(ReauthRequired):
            await s.oauth.tokens.access_token(a, "calendar")


# Tokens


async def test_not_connected(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        with pytest.raises(NotConnected):
            await s.oauth.tokens.access_token(uid, "gmail")


async def test_cached_until_a_minute_before_expiry_then_refreshed(tmp_path: Path) -> None:
    fake = FakeGoogle(expires_in=3600)
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        assert await s.oauth.tokens.access_token(uid, "gmail") == "access-1"
        assert len(fake.token_requests) == 1  # served from memory

    fake = FakeGoogle(expires_in=30)  # inside the 60 s margin from the start
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "c@example.com")
        await connect(s, uid)
        fake.expires_in = 3600
        assert await s.oauth.tokens.access_token(uid, "gmail") == "access-2"
        assert fake.token_requests[-1]["grant_type"] == "refresh_token"
        assert fake.token_requests[-1]["refresh_token"] == "refresh-1"
        assert await s.oauth.tokens.access_token(uid, "gmail") == "access-2"  # now cached


async def test_concurrent_callers_refresh_once(tmp_path: Path) -> None:
    import asyncio

    fake = FakeGoogle(expires_in=30, refresh_delay=True)
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        fake.expires_in = 3600
        tokens = await asyncio.gather(
            *(s.oauth.tokens.access_token(uid, "gmail") for _ in range(6))
        )
        assert set(tokens) == {"access-2"}
        refreshes = [r for r in fake.token_requests if r["grant_type"] == "refresh_token"]
        assert len(refreshes) == 1


async def test_rotated_refresh_token_is_stored(tmp_path: Path) -> None:
    fake = FakeGoogle(expires_in=30, rotate_refresh_to="refresh-2")
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        await s.oauth.tokens.access_token(uid, "gmail")
        fake.expires_in = 30
        await s.oauth.tokens.access_token(uid, "gmail")
        assert fake.token_requests[-1]["refresh_token"] == "refresh-2"
        assert "refresh-2" not in str((await s.rows())[0])


async def test_invalid_grant_removes_the_credential(tmp_path: Path) -> None:
    fake = FakeGoogle(expires_in=30)
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        fake.refresh_error = "invalid_grant"
        with pytest.raises(ReauthRequired):
            await s.oauth.tokens.access_token(uid, "gmail")
        assert await s.rows() == []
        with pytest.raises(NotConnected):
            await s.oauth.tokens.access_token(uid, "gmail")


async def test_other_refresh_errors_keep_the_credential(tmp_path: Path) -> None:
    fake = FakeGoogle(expires_in=30)
    async with google_site(tmp_path, fake=fake) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        fake.refresh_error = "invalid_client"
        with pytest.raises(Exception) as caught:
            await s.oauth.tokens.access_token(uid, "gmail")
        assert not isinstance(caught.value, ReauthRequired)
        assert len(await s.rows()) == 1


async def test_disconnect_revokes_and_deletes(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)
        await s.oauth.disconnect(uid, "gmail")
        assert s.fake.revoked == ["refresh-1"]
        assert await s.rows() == []
        with pytest.raises(NotConnected):
            await s.oauth.tokens.access_token(uid, "gmail")


async def test_disconnect_deletes_even_if_google_is_down(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        uid = await s.register(b, "a@example.com")
        await connect(s, uid)

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        s.oauth.tokens._transport = httpx.MockTransport(down)  # pyright: ignore[reportPrivateUsage]
        await s.oauth.disconnect(uid, "gmail")
        assert await s.rows() == []


async def test_status_is_per_user_and_per_service(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b, s.browser() as b2:
        a = await s.register(b, "a@example.com")
        other = await s.register(b2, "b@example.com")
        await connect(s, a, "gmail")
        assert await s.oauth.status(a) == {
            "gmail": {"connected": True, "account": "me@example.com"},
            "calendar": {"connected": False, "account": None},
        }
        assert (await s.oauth.status(other))["gmail"]["connected"] is False


# Routes


async def test_routes_need_sign_in(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        assert (await b.get("/api/google/status")).status_code == 401
        assert (await b.get("/api/google/connect?service=gmail")).status_code == 401
        assert (await b.get("/api/google/callback?code=x&state=y")).status_code == 401
        assert (
            await b.post("/api/google/disconnect", json={"service": "gmail"})
        ).status_code == 401


async def test_connect_flow_through_the_routes(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b:
        await s.register(b, "a@example.com")
        status = (await b.get("/api/google/status")).json()
        assert status["configured"] is True
        assert status["gmail"] == {"connected": False, "account": None}
        assert (await b.get("/api/google/connect?service=drive")).status_code == 400
        url = (await b.get("/api/google/connect?service=gmail")).json()["url"]
        s.fake.start_auth(url)
        done = await b.get(f"/api/google/callback?code=good-code&state={state_of(url)}")
        assert done.status_code == 303
        assert done.headers["location"] == "/#google=connected"
        assert (await b.get("/api/google/status")).json()["gmail"]["connected"] is True
        gone = await b.post("/api/google/disconnect", json={"service": "gmail"})
        assert gone.status_code == 200
        assert (await b.get("/api/google/status")).json()["gmail"]["connected"] is False
        assert (await b.post("/api/google/disconnect", json={"service": "x"})).status_code == 400


async def test_callback_errors_redirect_without_echoing_provider_text(tmp_path: Path) -> None:
    async with google_site(tmp_path) as s, s.browser() as b, s.browser() as b2:
        await s.register(b, "a@example.com")
        await s.register(b2, "b@example.com")
        url = (await b.get("/api/google/connect?service=gmail")).json()["url"]
        denied = await b.get(
            f"/api/google/callback?error=access_denied%3Cscript%3E&state={state_of(url)}"
        )
        assert denied.headers["location"] == "/#google=error"
        assert "script" not in denied.text
        url2 = (await b.get("/api/google/connect?service=gmail")).json()["url"]
        wrong_user = await b2.get(f"/api/google/callback?code=good-code&state={state_of(url2)}")
        assert wrong_user.headers["location"] == "/#google=error"
        assert await s.rows() == []


async def test_connect_is_503_when_google_is_not_configured(tmp_path: Path) -> None:
    async with google_site(tmp_path, google=False) as s, s.browser() as b:
        await s.register(b, "a@example.com")
        response = await b.get("/api/google/connect?service=gmail")
        assert response.status_code == 503
        assert "not set up" in response.json()["detail"]
        assert (await b.get("/api/google/status")).json()["configured"] is False


async def test_routes_are_404_when_accounts_are_off(tmp_path: Path) -> None:
    app = create_app(
        settings=settings_for(tmp_path, database=False),
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
    )
    app.include_router(google_router())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as b:
            assert (await b.get("/api/google/status")).status_code == 404
            assert (await b.get("/api/google/connect?service=gmail")).status_code == 404
