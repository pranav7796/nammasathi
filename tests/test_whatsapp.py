"""WhatsApp adapter: send, status, webhook. Everything runs against fakes, never the network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.accounts.service import AccountService
from app.config import Settings
from app.integrations.base import (
    IntegrationError,
    NotConnected,
    OutgoingMessage,
    RateLimited,
    ReauthRequired,
)
from app.llm.scripted import ScriptedExtractor
from app.web import create_app
from app.whatsapp import store
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.routes import whatsapp_router
from app.whatsapp.webhook import count_inbound, parse_statuses, verify_signature
from tests.fake_whatsapp import FakeWhatsApp

BASE = "https://saathi.example"
SECRET = "app-secret-for-tests"
VERIFY = "verify-token-for-tests"
PHONE = "+919876543210"
BODY_TEXT = "Your order is ready, Asha"
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()


def settings_for(tmp_path: Path, *, configured: bool = True) -> Settings:
    base = Settings(
        demo_understanding="scripted",
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'saathi.db'}",
        token_encryption_keys=KEY,
        open_registration=True,
        support_email="help@example.com",
    )
    if not configured:
        return base
    return replace(
        base,
        whatsapp_access_token="tok-secret",
        whatsapp_phone_number_id="123456",
        whatsapp_app_secret=SECRET,
        whatsapp_verify_token=VERIFY,
    )


class _TTS:
    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        del text
        Path(output_path).write_bytes(b"RIFF")
        return Path(output_path)


@dataclass
class Site:
    app: FastAPI
    settings: Settings

    @property
    def service(self) -> AccountService:
        return cast(AccountService, self.app.state.accounts)

    def browser(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    def client(self, fake: FakeWhatsApp) -> WhatsAppClient:
        return WhatsAppClient(self.settings, self.service, fake.transport)

    def user(self, n: int) -> str:
        """Create a real user row (foreign key) and return its id."""
        uid = f"user-{n}"
        with self.service.database.transaction() as tx:
            tx.execute(
                "INSERT INTO users (id, email, password_hash, live_access, consented_at, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, f"u{n}@example.com", "x", 0, 0, 0),
            )
        return uid


@asynccontextmanager
async def site(tmp_path: Path, *, configured: bool = True) -> AsyncGenerator[Site]:
    settings = settings_for(tmp_path, configured=configured)
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: _TTS(),  # type: ignore[arg-type,return-value]
    )
    app.include_router(whatsapp_router())
    async with app.router.lifespan_context(app):
        yield Site(app, settings)


def message(**changes: str) -> OutgoingMessage:
    base = OutgoingMessage(to_phone=PHONE, recipient_label="Amma", body=BODY_TEXT)
    return replace(base, **changes)


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def status_payload(*items: tuple[str, str]) -> bytes:
    statuses = [
        {"id": mid, "status": st, "timestamp": "1700000000", "recipient_id": "919876543210"}
        for mid, st in items
    ]
    payload = {"entry": [{"changes": [{"value": {"statuses": statuses}}]}]}
    return json.dumps(payload).encode()


def rows(s: Site) -> list[dict[str, object]]:
    with s.service.database.transaction() as tx:
        return tx.execute("SELECT * FROM whatsapp_messages")


# Sending


async def test_send_records_accepted_and_returns_id(tmp_path: Path) -> None:
    fake = FakeWhatsApp()
    async with site(tmp_path) as s:
        uid = s.user(1)
        sent = await s.client(fake).send(uid, message(), "k1")
        assert sent.id == "wamid.TEST1"
        assert sent.status == "accepted"
        request = fake.requests[0]
        assert str(request.url) == "https://graph.facebook.com/v21.0/123456/messages"
        assert request.headers["authorization"] == "Bearer tok-secret"
        body = fake.bodies()[0]
        assert body["messaging_product"] == "whatsapp" and body["type"] == "text"
        assert body["to"] == PHONE
        stored = rows(s)
        assert [(r["wa_message_id"], r["status"], r["recipient_label"]) for r in stored] == [
            ("wamid.TEST1", "accepted", "Amma")
        ]


@pytest.mark.parametrize(
    "bad",
    [
        {"to_phone": "919876543210"},
        {"to_phone": "+12345"},
        {"to_phone": "+1234567890123456"},
        {"to_phone": "+91 98765 43210"},
        {"body": ""},
        {"body": "   "},
        {"body": "x" * 1001},
        {"body": "hi\x00there"},
        {"body": "hi\x1b[0m"},
    ],
)
async def test_validation_happens_before_any_request(tmp_path: Path, bad: dict[str, str]) -> None:
    fake = FakeWhatsApp()
    async with site(tmp_path) as s:
        with pytest.raises(IntegrationError) as caught:
            await s.client(fake).send(s.user(1), message(**bad), "k")
        assert not caught.value.retryable
        assert fake.requests == []
        assert rows(s) == []


@pytest.mark.parametrize(
    ("mode", "kind", "retryable", "words"),
    [
        ("code:190", ReauthRequired, False, "token expired"),
        ("401", ReauthRequired, False, "token expired"),
        ("code:131047", IntegrationError, False, "24 hours"),
        ("code:131030", IntegrationError, False, "allow list"),
        ("code:131026", IntegrationError, False, "could not deliver"),
        ("code:130429", RateLimited, True, "limiting"),
        ("429", RateLimited, True, "limiting"),
        ("connect", IntegrationError, True, "could not reach"),
        ("timeout", IntegrationError, False, "outcome unknown"),
        ("500", IntegrationError, False, "outcome unknown"),
        ("no_id", IntegrationError, False, "outcome unknown"),
        ("code:100", IntegrationError, False, "error 100"),
    ],
)
async def test_errors_are_mapped(
    tmp_path: Path, mode: str, kind: type[IntegrationError], retryable: bool, words: str
) -> None:
    fake = FakeWhatsApp(mode)
    async with site(tmp_path) as s:
        with pytest.raises(kind) as caught:
            await s.client(fake).send(s.user(1), message(), "k")
        assert caught.value.retryable is retryable
        assert words in str(caught.value)
        assert len(fake.requests) == 1  # never retried automatically
        assert rows(s) == []


async def test_idempotency_key_sends_once(tmp_path: Path) -> None:
    fake = FakeWhatsApp()
    async with site(tmp_path) as s:
        uid = s.user(1)
        client = s.client(fake)
        first = await client.send(uid, message(), "same")
        again = await client.send(uid, message(), "same")
        assert first == again
        assert len(fake.requests) == 1
        await client.send(uid, message(), "other")
        assert len(fake.requests) == 2


async def test_not_configured_raises_not_connected(tmp_path: Path) -> None:
    fake = FakeWhatsApp()
    async with site(tmp_path, configured=False) as s:
        with pytest.raises(NotConnected, match="not configured"):
            await s.client(fake).send(s.user(1), message(), "k")
        assert fake.requests == []


# Status store


async def test_status_is_monotonic_and_failed_is_terminal(tmp_path: Path) -> None:
    fake = FakeWhatsApp()
    async with site(tmp_path) as s:
        uid = s.user(1)
        client = s.client(fake)
        sent = await client.send(uid, message(), "k")
        db = s.service.database
        assert store.apply_status(db, sent.id, "read") is True
        assert store.apply_status(db, sent.id, "delivered") is False  # out of order
        assert store.apply_status(db, sent.id, "sent") is False
        assert store.apply_status(db, sent.id, "read") is False  # duplicate
        assert (await client.status(uid, sent.id)).status == "read"

        other = await client.send(uid, message(), "k2")
        assert store.apply_status(db, other.id, "failed", "error 131026: x") is True
        assert store.apply_status(db, other.id, "sent") is False
        assert store.apply_status(db, other.id, "read") is False
        got = await client.status(uid, other.id)
        assert got.status == "failed" and "131026" in got.detail


async def test_accepted_until_a_webhook_says_otherwise(tmp_path: Path) -> None:
    async with site(tmp_path) as s:
        uid = s.user(1)
        client = s.client(FakeWhatsApp())
        sent = await client.send(uid, message(), "k")
        assert (await client.status(uid, sent.id)).status == "accepted"


async def test_unknown_id_is_ignored(tmp_path: Path) -> None:
    async with site(tmp_path) as s:
        assert store.apply_status(s.service.database, "wamid.NOPE", "sent") is False
        assert rows(s) == []


async def test_user_isolation(tmp_path: Path) -> None:
    async with site(tmp_path) as s:
        a, b = s.user(1), s.user(2)
        client = s.client(FakeWhatsApp())
        sent = await client.send(a, message(), "k")
        with pytest.raises(IntegrationError, match="not found"):
            await client.status(b, sent.id)
        assert (await client.status(a, sent.id)).id == sent.id


# Webhook helpers


def test_signature_cases() -> None:
    body = b'{"a":1}'
    good = sign(body)
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, good.removeprefix("sha256="))
    assert not verify_signature(SECRET, body, "sha1=" + good[7:])
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature(SECRET, body, sign(body, "other"))
    assert not verify_signature(None, body, good)
    assert not verify_signature(SECRET, body, "sha256=zz")


def test_parse_statuses_and_inbound_count() -> None:
    payload = json.loads(status_payload(("a", "sent"), ("b", "bogus"), ("c", "failed")))
    payload["entry"][0]["changes"][0]["value"]["messages"] = [{"text": {"body": "secret"}}]
    parsed = parse_statuses(payload)
    assert [(u.message_id, u.status) for u in parsed] == [("a", "sent"), ("c", "failed")]
    assert count_inbound(payload) == 1
    assert parse_statuses("junk") == [] and count_inbound(None) == 0


# Routes


async def test_handshake(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as http:
        ok = await http.get(
            "/api/whatsapp/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1234"},
        )
        assert ok.status_code == 200 and ok.text == "1234"
        wrong = await http.get(
            "/api/whatsapp/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
        )
        assert wrong.status_code == 403
        mode = await http.get(
            "/api/whatsapp/webhook",
            params={"hub.mode": "x", "hub.verify_token": VERIFY, "hub.challenge": "1"},
        )
        assert mode.status_code == 403


async def test_routes_404_when_not_configured(tmp_path: Path) -> None:
    async with site(tmp_path, configured=False) as s, s.browser() as http:
        got = await http.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe"})
        assert got.status_code == 404
        posted = await http.post("/api/whatsapp/webhook", content=b"{}")
        assert posted.status_code == 404


async def test_webhook_signature_required_and_statuses_applied(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as http:
        uid = s.user(1)
        sent = await s.client(FakeWhatsApp()).send(uid, message(), "k")
        body = status_payload((sent.id, "delivered"), ("wamid.unknown", "read"))
        url = "/api/whatsapp/webhook"
        assert (await http.post(url, content=body)).status_code == 403
        bad = {"x-hub-signature-256": sign(body, "other")}
        assert (await http.post(url, content=body, headers=bad)).status_code == 403
        tampered = {"x-hub-signature-256": sign(body)}
        assert (await http.post(url, content=body + b" ", headers=tampered)).status_code == 403
        assert rows(s)[0]["status"] == "accepted"

        good = await http.post(url, content=body, headers={"x-hub-signature-256": sign(body)})
        assert good.status_code == 200
        assert rows(s)[0]["status"] == "delivered"
        # Replay / out of order is harmless.
        old = status_payload((sent.id, "sent"))
        again = await http.post(url, content=old, headers={"x-hub-signature-256": sign(old)})
        assert again.status_code == 200
        assert rows(s)[0]["status"] == "delivered"


async def test_webhook_caps_body_and_survives_junk(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as http:
        big = b"x" * (256 * 1024 + 1)
        sig = {"x-hub-signature-256": sign(big)}
        assert (
            await http.post("/api/whatsapp/webhook", content=big, headers=sig)
        ).status_code == 413
        junk = b"not json"
        sig = {"x-hub-signature-256": sign(junk)}
        assert (
            await http.post("/api/whatsapp/webhook", content=junk, headers=sig)
        ).status_code == 200


async def test_status_route_needs_sign_in(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as http:
        assert (await http.get("/api/whatsapp/status")).status_code == 401
        reg = await http.post(
            "/api/account/register",
            json={
                "email": "asha@example.com",
                "password": "correct horse battery",
                "consent": True,
            },
        )
        assert reg.status_code == 200
        got = await http.get("/api/whatsapp/status")
        assert got.status_code == 200 and got.json() == {"configured": True}


# Privacy


async def test_no_text_or_number_in_db_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with site(tmp_path) as s, s.browser() as http:
        uid = s.user(1)
        sent = await s.client(FakeWhatsApp()).send(uid, message(), "k")
        inbound = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"from": "919876543210", "text": {"body": "SECRETTEXT"}}
                                ],
                                "statuses": [
                                    {
                                        "id": sent.id,
                                        "status": "sent",
                                        "recipient_id": "919876543210",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        raw = json.dumps(inbound).encode()
        done = await http.post(
            "/api/whatsapp/webhook", content=raw, headers={"x-hub-signature-256": sign(raw)}
        )
        assert done.status_code == 200
        dump = json.dumps(rows(s))
        for forbidden in (BODY_TEXT, "SECRETTEXT", "9876543210", "tok-secret", SECRET):
            assert forbidden not in dump
            assert forbidden not in caplog.text
