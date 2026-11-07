"""Sending through Twilio's Messages API, delivery status, and answering with results.

Mocked tests: Twilio is tests/fake_twilio.py. Nothing reaches the real Twilio.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import pytest

from app.accounts.service import AccountService
from app.integrations.base import (
    IntegrationError,
    OutgoingMessage,
    RateLimited,
    ReauthRequired,
)
from app.twilio_messaging import TwilioMessaging
from app.twilio_whatsapp import expected_signature
from tests.fake_twilio import FakeTwilio
from tests.test_accounts import BASE
from tests.test_twilio_whatsapp import (
    ASHA,
    SID,
    TOKEN,
    link,
    said,
    send,
    settled,
    sign_up,
    twilio_site,
)

MESSAGE = OutgoingMessage("+919876543210", "Amma", "Running 10 minutes late")


def adapter(site_app: Any, fake: FakeTwilio, **settings: Any) -> TwilioMessaging:
    service = cast(AccountService, site_app.state.accounts)
    chosen = replace(service.settings, **settings) if settings else service.settings
    return TwilioMessaging(chosen, service, transport=fake.transport)


async def user_id(client: Any, s: Any) -> str:
    me = (await client.get("/api/account/me")).json()
    store = cast(AccountService, s.app.state.accounts).store
    found = store.user_by_email(me["user"]["email"])
    assert found is not None
    return found.id


# Formatting and success ---------------------------------------------------------------------------


async def test_a_message_is_sent_in_twilios_whatsapp_format(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        sent = await adapter(s.app, fake).send(uid, MESSAGE, "task-1")
        assert sent.status == "accepted" and sent.id == "SMfake0001"  # queued is not delivered
        form = fake.sent[0]
        assert form["To"] == "whatsapp:+919876543210"
        assert form["From"] == "whatsapp:+14155238886"
        assert form["Body"] == "Running 10 minutes late"
        assert form["StatusCallback"] == f"{BASE}/api/twilio/status"
        assert fake.auth == [SID]  # Account SID and Auth Token when no API key pair is set


async def test_an_api_key_pair_is_used_when_configured(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        chosen = adapter(s.app, fake, twilio_api_key_sid="SKkey", twilio_api_key_secret="sec")
        await chosen.send(uid, MESSAGE, "task-1")
        assert fake.auth == ["SKkey"]


async def test_the_same_send_twice_goes_out_once(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        twilio = adapter(s.app, fake)
        first = await twilio.send(uid, MESSAGE, "task-1")
        second = await twilio.send(uid, MESSAGE, "task-1")
        assert first == second and len(fake.sent) == 1


async def test_a_bad_number_is_refused_before_any_request(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        with pytest.raises(IntegrationError):
            await adapter(s.app, fake).send(uid, OutgoingMessage("98765", "x", "hi"), "k")
        assert fake.sent == []


# Failures -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "error", "words"),
    [
        ("401", ReauthRequired, "refused this site's credentials"),
        ("code:63016", IntegrationError, "24 hours"),
        ("code:63015", IntegrationError, "joined"),
        ("code:21211", IntegrationError, "valid phone number"),
        ("429", RateLimited, "busy"),
    ],
)
async def test_twilio_refusals_are_plain_and_nothing_is_recorded_as_sent(
    tmp_path: Path, mode: str, error: type[Exception], words: str
) -> None:
    fake = FakeTwilio(mode)
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        with pytest.raises(error) as raised:
            await adapter(s.app, fake).send(uid, MESSAGE, "k")
        assert words in str(raised.value)


@pytest.mark.parametrize("mode", ["500", "timeout"])
async def test_an_unclear_outcome_is_reported_and_never_retried(tmp_path: Path, mode: str) -> None:
    fake = FakeTwilio(mode)
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        twilio = adapter(s.app, fake)
        with pytest.raises(IntegrationError) as raised:
            await twilio.send(uid, MESSAGE, "k")
        assert "Outcome unknown" in str(raised.value) and raised.value.retryable is False
        assert len(fake.auth) == 1  # one attempt only


async def test_twilio_unreachable_says_nothing_was_sent(tmp_path: Path) -> None:
    fake = FakeTwilio("connect")
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        with pytest.raises(IntegrationError) as raised:
            await adapter(s.app, fake).send(uid, MESSAGE, "k")
        assert "Nothing was sent" in str(raised.value) and raised.value.retryable is True


# Delivery status ----------------------------------------------------------------------------------


def callback(params: dict[str, str], token: str = TOKEN) -> dict[str, Any]:
    url = f"{BASE}/api/twilio/status"
    return {
        "content": urlencode(params),
        "headers": {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": expected_signature(token, url, params),
        },
    }


async def test_status_callbacks_move_the_status_forward_only(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        twilio = adapter(s.app, fake)
        sent = await twilio.send(uid, MESSAGE, "k")
        for status in ("sent", "delivered", "sent"):  # the late "sent" must not undo "delivered"
            params = {"MessageSid": sent.id, "MessageStatus": status, "AccountSid": SID}
            response = await client.post("/api/twilio/status", **callback(params))
            assert response.status_code == 204
        assert (await twilio.status(uid, sent.id)).status == "delivered"


async def test_a_failed_delivery_is_recorded_with_its_error(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        twilio = adapter(s.app, fake)
        sent = await twilio.send(uid, MESSAGE, "k")
        params = {"MessageSid": sent.id, "MessageStatus": "undelivered", "ErrorCode": "63016"}
        await client.post("/api/twilio/status", **callback(params))
        row = await twilio.status(uid, sent.id)
        assert row.status == "failed" and "63016" in row.detail


async def test_an_unsigned_status_callback_changes_nothing(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        uid = await user_id(client, s)
        twilio = adapter(s.app, fake)
        sent = await twilio.send(uid, MESSAGE, "k")
        params = {"MessageSid": sent.id, "MessageStatus": "delivered"}
        forged = await client.post("/api/twilio/status", **callback(params, token="guess"))
        assert forged.status_code == 403
        assert (await twilio.status(uid, sent.id)).status == "accepted"


# Over WhatsApp: approval, then a real send through Twilio ----------------------------------------


async def test_a_message_asked_for_on_whatsapp_goes_out_only_after_confirm(tmp_path: Path) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        reply = said(await send(client, ASHA, "Send +919000000009 a WhatsApp saying on my way"))
        assert "Reply" in reply and "send it" in reply  # it tells you how to approve
        await settled(client)
        assert fake.sent == []  # nothing before approval
        assert "on my way" in said(await send(client, ASHA, "send it"))
        assert fake.sent == []  # reading it back sends nothing
        await send(client, ASHA, "confirm")
        assert len(fake.sent) == 1
        assert fake.sent[0]["To"] == "whatsapp:+919000000009"
        assert fake.sent[0]["Body"] == "on my way"


async def test_the_reply_carries_the_results_not_just_a_promise(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        reply = said(
            await send(client, ASHA, "Check my important emails and what's on my calendar tomorrow")
        )
        assert "Email (demo data):" in reply  # Gmail isn't connected here, and it says so
        assert "Calendar (demo data):" in reply
        assert "Rahul" in reply  # an actual result, from the practice inbox


# The Apps card: connected only after Twilio confirms ----------------------------------------------


async def test_the_card_is_connected_only_after_twilio_accepts_the_credentials(
    tmp_path: Path,
) -> None:
    fake = FakeTwilio()
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        cards = {a["id"]: a for a in (await client.get("/api/integrations")).json()["apps"]}
        assert cards["twilio"]["state"] == "connected"
        assert any(user == SID for user in fake.auth)  # it really asked Twilio


async def test_refused_credentials_show_authorization_failed(tmp_path: Path) -> None:
    fake = FakeTwilio("401")
    async with twilio_site(tmp_path, twilio=fake) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        cards = {a["id"]: a for a in (await client.get("/api/integrations")).json()["apps"]}
        assert cards["twilio"]["state"] == "authorization_failed"
