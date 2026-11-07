"""Inbound WhatsApp from Twilio: the signature, the form, the routing to the right person.

No request here reaches Twilio. Twilio's side is played by signed test posts.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from app.config import Settings
from app.llm.scripted import ScriptedExtractor
from app.twilio_whatsapp import expected_signature, normalise_sender, webhook_url
from app.web import create_app
from tests.test_accounts import BASE, KEY, PASSWORD, RecordingMailer, SilentTTS, Site

TOKEN = "tw-auth-token-for-tests"
SID = "ACtestaccountsid"
URL = f"{BASE}/api/twilio/whatsapp"
ASHA, RAVI = "+919876543210", "+919812345678"


def test_the_signature_matches_twilios_own_worked_example() -> None:
    """The example from https://www.twilio.com/docs/usage/security ("Explore the algorithm")."""
    params = {
        "Digits": "1234",
        "To": "+18005551212",
        "From": "+14158675310",
        "Caller": "+14158675310",
        "CallSid": "CA1234567890ABCDE",
    }
    got = expected_signature("12345", "https://example.com/myapp.php?foo=1&bar=2", params)
    assert got == "L/OH5YylLD5NRKLltdqwSvS0BnU="


def test_numbers_and_the_webhook_address() -> None:
    assert normalise_sender("whatsapp:+919876543210") == "+919876543210"
    assert normalise_sender("whatsapp:+91 98765 43210") == "+919876543210"
    assert normalise_sender("+919876543210") is None  # an SMS, not WhatsApp
    assert normalise_sender("whatsapp:12345") is None
    assert webhook_url("https://x.example/") == "https://x.example/api/twilio/whatsapp"


@asynccontextmanager
async def twilio_site(tmp_path: Path, *, configured: bool = True) -> AsyncGenerator[Site]:
    settings = Settings(
        demo_understanding="scripted",
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'saathi.db'}",
        token_encryption_keys=KEY,
        support_email="help@example.com",
    )
    if configured:
        settings = replace(
            settings,
            twilio_account_sid=SID,
            twilio_auth_token=TOKEN,
            twilio_whatsapp_from="whatsapp:+14155238886",
        )
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
        mailer=RecordingMailer(can_send=True),
    )
    async with app.router.lifespan_context(app):
        yield Site(app, RecordingMailer(can_send=True))


async def sign_up(client: httpx.AsyncClient, email: str) -> None:
    done = await client.post(
        "/api/account/register", json={"email": email, "password": PASSWORD, "consent": True}
    )
    assert done.status_code == 200, done.text


async def send(
    client: httpx.AsyncClient,
    sender: str,
    body: str,
    *,
    sid: str | None = None,
    token: str = TOKEN,
    account: str = SID,
    query: str = "",
    extra: dict[str, str] | None = None,
    signed: bool = True,
) -> httpx.Response:
    """What Twilio does: a signed form POST to the webhook."""
    params = {
        "MessageSid": sid or f"SM{abs(hash((sender, body, query))) % 10**12:012d}",
        "AccountSid": account,
        "From": f"whatsapp:{sender}",
        "To": "whatsapp:+14155238886",
        "Body": body,
        "NumMedia": "0",
        **(extra or {}),
    }
    url = URL + (f"?{query}" if query else "")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        headers["X-Twilio-Signature"] = expected_signature(token, url, params)
    return await client.post(
        "/api/twilio/whatsapp" + (f"?{query}" if query else ""),
        content=urlencode(params),
        headers=headers,
    )


def said(response: httpx.Response) -> str:
    match = re.search(r"<Message>(.*)</Message>", response.text, re.S)
    return match.group(1) if match else ""


async def link(client: httpx.AsyncClient, phone: str) -> None:
    """Sign-in side asks for a code; the phone sends it. That is the only way to link."""
    code = (await client.post("/api/twilio/link")).json()["code"]
    reply = await send(client, phone, f"link {code}")
    assert "Linked" in said(reply), reply.text


async def settled(client: httpx.AsyncClient) -> dict[str, object]:
    for _ in range(80):
        data = (await client.get("/api/tasks/current")).json()
        if data["plan"] and not data["plan"]["active"]:
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("the plan never settled")


# The endpoint, the method, the form ---------------------------------------------------------------


async def test_it_accepts_a_signed_form_post_and_answers_with_twiml(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        response = await send(client, ASHA, "hello")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/xml")
        assert response.text.startswith('<?xml version="1.0" encoding="UTF-8"?><Response>')


async def test_only_post_is_accepted(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        assert (await client.get("/api/twilio/whatsapp")).status_code == 405


async def test_the_fields_twilio_sends_are_read(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        reply = await send(
            client, ASHA, "Remind me to call Amma tomorrow at 7 pm", extra={"ProfileName": "Asha"}
        )
        assert "Saved a reminder" in said(reply)  # From, Body and the extra field all parsed


# The signature ---------------------------------------------------------------------------------


async def test_a_missing_or_wrong_signature_is_refused_and_nothing_happens(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        body = "Remind me to pay rent tomorrow at 9 am"
        assert (await send(client, ASHA, body, signed=False)).status_code == 403
        assert (await send(client, ASHA, body, token="another-token")).status_code == 403
        assert (await client.get("/api/personal/reminders")).json()["reminders"] == []


async def test_a_tampered_field_breaks_the_signature(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        params = {
            "MessageSid": "SMtamper",
            "AccountSid": SID,
            "From": f"whatsapp:{ASHA}",
            "Body": "hello",
        }
        good = expected_signature(TOKEN, URL, params)
        params["Body"] = "something else"
        response = await client.post(
            "/api/twilio/whatsapp",
            content=urlencode(params),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": good,
            },
        )
        assert response.status_code == 403


async def test_the_query_string_is_part_of_what_is_signed(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        assert (await send(client, ASHA, "hi", query="a=1")).status_code == 200
        params = {"MessageSid": "SMq", "AccountSid": SID, "From": f"whatsapp:{ASHA}", "Body": "x"}
        signed_without_query = expected_signature(TOKEN, URL, params)
        response = await client.post(
            "/api/twilio/whatsapp?a=1",
            content=urlencode(params),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signed_without_query,
            },
        )
        assert response.status_code == 403


async def test_a_message_for_another_twilio_account_is_refused(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        assert (await send(client, ASHA, "hi", account="ACsomeoneelse")).status_code == 403


async def test_without_an_auth_token_the_endpoint_does_not_exist(tmp_path: Path) -> None:
    async with twilio_site(tmp_path, configured=False) as s, s.browser() as client:
        assert (await send(client, ASHA, "hi")).status_code == 404
        await sign_up(client, "asha@example.com")
        assert (await client.post("/api/twilio/link")).status_code == 503


# Linking: a number belongs to one person, proved by the number itself ----------------------------


async def test_an_unlinked_number_gets_instructions_and_nothing_is_done(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        reply = await send(client, RAVI, "Remind me to buy milk tomorrow at 8 am")
        assert "isn't linked" in said(reply) and "link ABC123" in said(reply)
        assert (await client.get("/api/personal/reminders")).json()["reminders"] == []


async def test_linking_needs_the_code_from_the_signed_in_person(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        assert (await client.post("/api/twilio/link")).status_code == 401  # not signed in
        await sign_up(client, "asha@example.com")
        info = (await client.post("/api/twilio/link")).json()
        assert info["send_to"] == "+14155238886" and info["message"].startswith("link ")
        wrong = await send(client, ASHA, "link ZZZZZZ")
        assert "didn't work" in said(wrong)
        ok = await send(client, ASHA, f"link {info['code']}")
        assert "Linked" in said(ok)
        again = await send(client, RAVI, f"link {info['code']}")
        assert "didn't work" in said(again)  # a code works once
        status = (await client.get("/api/twilio/status")).json()
        assert status["linked"] is True and status["phone"].endswith("3210")
        assert ASHA not in str(status)  # shown masked
        assert status["webhook_url"] == URL


async def test_a_number_already_linked_elsewhere_is_not_taken_over(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as asha, s.browser() as ravi:
        await sign_up(asha, "asha@example.com")
        await sign_up(ravi, "ravi@example.com")
        await link(asha, ASHA)
        code = (await ravi.post("/api/twilio/link")).json()["code"]
        reply = await send(ravi, ASHA, f"link {code}")
        assert "already linked to another account" in said(reply)
        assert (await ravi.get("/api/twilio/status")).json()["linked"] is False


async def test_unlinking_stops_the_number_acting(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        assert (await client.post("/api/twilio/unlink")).status_code == 200
        reply = await send(client, ASHA, "Remind me to pay rent tomorrow at 9 am")
        assert "isn't linked" in said(reply)
        assert (await client.get("/api/personal/reminders")).json()["reminders"] == []


# Routing to the right person ---------------------------------------------------------------------


async def test_each_number_acts_only_on_its_own_persons_account(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as asha, s.browser() as ravi:
        await sign_up(asha, "asha@example.com")
        await sign_up(ravi, "ravi@example.com")
        await link(asha, ASHA)
        await link(ravi, RAVI)
        await send(asha, ASHA, "Remind me to call Amma tomorrow at 7 pm")
        await send(ravi, RAVI, "Remind me to buy milk tomorrow at 8 am")
        mine = [r["text"] for r in (await asha.get("/api/personal/reminders")).json()["reminders"]]
        his = [r["text"] for r in (await ravi.get("/api/personal/reminders")).json()["reminders"]]
        assert mine == ["call Amma"] and his == ["buy milk"]


async def test_a_task_started_on_whatsapp_belongs_to_that_person_only(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as asha, s.browser() as ravi:
        await sign_up(asha, "asha@example.com")
        await sign_up(ravi, "ravi@example.com")
        await link(asha, ASHA)
        await link(ravi, RAVI)
        started = await send(asha, ASHA, "Plan my day")
        assert "On it" in said(started)
        assert (await settled(asha))["plan"] is not None
        assert (await ravi.get("/api/tasks/current")).json() == {"plan": None, "ui": []}


async def test_a_question_and_its_answer_stay_together_across_messages(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        assert "Who should I message" in said(await send(client, ASHA, "Send a WhatsApp message"))
        assert "What should I say to Rahul" in said(await send(client, ASHA, "Rahul"))
        done = await send(client, ASHA, "I'll be late")
        assert "On it" in said(done)


async def test_approving_over_whatsapp_needs_the_readback_and_an_explicit_confirm(
    tmp_path: Path,
) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        await send(client, ASHA, "Send +919000000009 a WhatsApp message saying running late")
        await settled(client)
        bare = await send(client, ASHA, "yes")
        assert "On it" not in said(bare)  # a bare yes starts nothing
        readback = said(await send(client, ASHA, "send it"))
        assert "running late" in readback and "confirm" in readback
        board = await client.get("/api/tasks/current")
        titles = {t["title"]: t["status"] for t in board.json()["ui"][0]["tasks"]}
        assert (
            titles["Send WhatsApp message"] == "waiting_for_approval"
        )  # reading back sent nothing
        assert "Done" in said(await send(client, ASHA, "confirm"))
        later = {
            t["title"]: t["status"]
            for t in (await client.get("/api/tasks/current")).json()["ui"][0]["tasks"]
        }
        assert later["Send WhatsApp message"] == "completed"


async def test_ordering_is_left_to_the_website(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        reply = said(await send(client, ASHA, "Two kilos of rice and one litre of milk"))
        assert "website" in reply and "Saved" not in reply


# Robustness ---------------------------------------------------------------------------------------


async def test_a_retried_message_is_handled_once(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        body = "Remind me to renew the passport tomorrow at 10 am"
        first = await send(client, ASHA, body, sid="SMsame")
        second = await send(client, ASHA, body, sid="SMsame")
        assert "Saved a reminder" in said(first) and said(second) == ""
        assert len((await client.get("/api/personal/reminders")).json()["reminders"]) == 1


async def test_media_only_and_non_whatsapp_messages_are_handled_politely(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        picture = await send(client, ASHA, "", extra={"NumMedia": "1", "MediaUrl0": "https://x/y"})
        assert "text messages only" in said(picture)
        params = {"MessageSid": "SMsms", "AccountSid": SID, "From": ASHA, "Body": "hi"}
        sms = await client.post(
            "/api/twilio/whatsapp",
            content=urlencode(params),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": expected_signature(TOKEN, URL, params),
            },
        )
        assert sms.status_code == 200 and said(sms) == ""


async def test_a_flood_from_one_number_is_cut_off_and_the_body_is_bounded(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        last = ""
        for n in range(45):
            last = said(await send(client, RAVI, f"hello {n}", sid=f"SMflood{n}"))
        assert last == ""  # past the hourly limit nothing is sent back
        huge = await client.post(
            "/api/twilio/whatsapp",
            content="Body=" + "x" * 70_000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert huge.status_code in (403, 413)


async def test_nothing_secret_or_personal_reaches_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        await link(client, ASHA)
        await send(client, ASHA, "Remind me to hide this secret text tomorrow at 7 pm")
        await send(client, ASHA, "x", token="wrong")
    text = caplog.text
    assert TOKEN not in text and ASHA not in text and "hide this secret text" not in text


async def test_the_apps_screen_shows_the_link_state_honestly(tmp_path: Path) -> None:
    async with twilio_site(tmp_path) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        cards = {a["id"]: a for a in (await client.get("/api/integrations")).json()["apps"]}
        assert cards["twilio"]["state"] == "not_connected" and cards["twilio"]["can_connect"]
        await link(client, ASHA)
        cards = {a["id"]: a for a in (await client.get("/api/integrations")).json()["apps"]}
        assert cards["twilio"]["state"] == "connected"
        assert cards["twilio"]["account"].endswith("3210") and ASHA not in str(cards)


async def test_without_twilio_details_the_card_says_what_is_missing(tmp_path: Path) -> None:
    async with twilio_site(tmp_path, configured=False) as s, s.browser() as client:
        await sign_up(client, "asha@example.com")
        cards = {a["id"]: a for a in (await client.get("/api/integrations")).json()["apps"]}
        assert cards["twilio"]["state"] == "configuration_required"
        assert cards["twilio"]["setup"] == ["TWILIO_AUTH_TOKEN"]
