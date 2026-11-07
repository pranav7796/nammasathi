"""Gmail, Calendar and WhatsApp through the real web app, against stand-ins for Google and Meta.

This runs the whole chain a person's request takes: sign in, connect with OAuth, ask in plain
words, the planner, the task engine, the real adapters, and the widgets. The only fake parts are
Google's and Meta's servers (tests/fake_google.py, fake_gcal.py, fake_whatsapp.py), built from
their official docs.

It proves our side. It does not prove Google or Meta behave as those stand-ins do: nothing here
has run against the real services.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import Settings
from app.llm.scripted import ScriptedExtractor
from app.personal.dates import IST
from app.web import create_app
from tests.fake_gcal import FakeGCal
from tests.fake_google import FakeGoogle, make_message
from tests.fake_whatsapp import FakeWhatsApp
from tests.test_accounts import BASE, KEY, PASSWORD, RecordingMailer, SilentTTS, Site

APP_SECRET = "app-secret"
VERIFY_TOKEN = "verify-me"


class World:
    """Google, the calendar and WhatsApp, behind one transport that routes by host."""

    def __init__(self) -> None:
        self.google = FakeGoogle()
        self.gcal = FakeGCal()
        self.wa = FakeWhatsApp()
        self.gcal.accept_token = lambda token: token in self.google.valid_access

    def transport(self) -> httpx.MockTransport:
        async def route(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host in ("oauth2.googleapis.com", "gmail.googleapis.com"):
                return await self.google._handle(request)  # pyright: ignore[reportPrivateUsage]
            if host == "www.googleapis.com":
                return self.gcal._handle(request)  # pyright: ignore[reportPrivateUsage]
            if host == "graph.facebook.com":
                return self.wa._handle(request)  # pyright: ignore[reportPrivateUsage]
            return httpx.Response(404, json={"error": "unexpected host"})

        return httpx.MockTransport(route)


@asynccontextmanager
async def live_site(
    tmp_path: Path, world: World, *, google: bool = True, whatsapp: bool = True
) -> AsyncGenerator[Site]:
    settings = Settings(
        demo_understanding="scripted",
        public_base_url=BASE,
        database_url=f"sqlite:///{tmp_path / 'saathi.db'}",
        token_encryption_keys=KEY,
        support_email="help@example.com",
    )
    if google:
        settings = replace(settings, google_client_id="cid", google_client_secret="secret")
    if whatsapp:
        settings = replace(
            settings,
            whatsapp_access_token="wa-token",
            whatsapp_phone_number_id="123456",
            whatsapp_app_secret=APP_SECRET,
            whatsapp_verify_token=VERIFY_TOKEN,
        )
    mailer = RecordingMailer(can_send=True)
    app = create_app(
        settings=settings,
        extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
        mailer=mailer,
    )
    app.state.integration_transport = world.transport()
    async with app.router.lifespan_context(app):
        yield Site(app, mailer)


async def sign_up(client: httpx.AsyncClient, email: str = "asha@example.com") -> None:
    response = await client.post(
        "/api/account/register", json={"email": email, "password": PASSWORD, "consent": True}
    )
    assert response.status_code == 200, response.text


def state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


async def connect(client: httpx.AsyncClient, world: World, service: str) -> httpx.Response:
    got = await client.get(f"/api/google/connect?service={service}")
    assert got.status_code == 200, got.text
    url: str = got.json()["url"]
    world.google.start_auth(url)
    state = state_of(url)
    return await client.get(
        f"/api/google/callback?code=good-code&state={state}", follow_redirects=False
    )


async def start(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/api/sessions", json={"language": "en-IN"})
    return {"X-Session-ID": response.json()["session_id"]}


async def say(client: httpx.AsyncClient, headers: dict[str, str], text: str) -> dict[str, Any]:
    response = await client.post("/api/session/text", json={"text": text}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def settled(client: httpx.AsyncClient) -> dict[str, Any]:
    for _ in range(80):
        data = (await client.get("/api/tasks/current")).json()
        if data["plan"] and not data["plan"]["active"]:
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("the plan never settled")


def widget(data: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(w for w in data["ui"] if w["type"] == kind)


def board_status(data: dict[str, Any]) -> dict[str, str]:
    board = widget(data, "task_board")
    return {t["title"]: t["status"] for t in board["tasks"]}


async def act(
    client: httpx.AsyncClient, action_id: str, values: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = await client.post(
        "/api/tasks/action", json={"action_id": action_id, "values": values or {}}
    )
    assert response.status_code == 200, response.text
    return response.json()


def signed(body: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


# Gmail ------------------------------------------------------------------------------------------


async def test_connecting_gmail_uses_oauth_and_shows_a_real_connection(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        before = (await client.get("/api/google/status")).json()
        assert before["configured"] is True and before["gmail"]["connected"] is False
        done = await connect(client, world, "gmail")
        assert done.status_code == 303 and done.headers["location"].endswith("#google=connected")
        after = (await client.get("/api/google/status")).json()
        assert after["gmail"]["connected"] is True and after["calendar"]["connected"] is False
        scope = world.google.last_scope  # type: ignore[attr-defined]
        assert "gmail.readonly" in scope and "gmail.send" in scope
        assert "calendar" not in scope and "gmail.modify" not in scope  # the minimum


async def test_gmail_requests_use_the_real_inbox_and_say_so(tmp_path: Path) -> None:
    world = World()
    world.google.add(
        make_message(
            "m1",
            subject="Quarterly review",
            sender="Rahul Sharma <rahul@example.com>",
            labels=("INBOX", "IMPORTANT", "UNREAD"),
        )
    )
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        # Before connecting: clearly practice data.
        await say(client, await start(client), "Check my important emails")
        practice = widget(await settled(client), "email_list")
        assert practice["demo"] is True
        await connect(client, world, "gmail")
        await say(client, await start(client), "Check my important emails")
        real = widget(await settled(client), "email_list")
        assert real["demo"] is False
        assert [i["subject"] for i in real["items"]] == ["Quarterly review"]


async def test_a_reply_is_sent_through_gmail_only_after_approval(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        # Rahul is not a saved contact: Saathi must ask, never guess an address.
        await say(client, await start(client), "Reply to Rahul saying I'll attend the meeting")
        data = await settled(client)
        draft = widget(data, "composer")
        assert draft["demo"] is False and draft["state"] == "draft"
        assert board_status(data)["Draft and send an email"] == "waiting_for_input"
        assert world.google.sent == []  # nothing has gone out
        task = draft["send"]["action_id"].split(":")[1]
        refused = await act(client, f"task.approve:{task}", {"body": draft["body"]})
        assert refused["result"] == "needs_input" and world.google.sent == []
        sent = await act(
            client,
            f"task.approve:{task}",
            {"to": "rahul@example.com", "subject": "Re: meeting", "body": "I'll attend."},
        )
        assert sent["result"] == "ok"
        assert len(world.google.sent) == 1
        message = world.google.sent[0]["parsed"]
        assert message["To"] == "rahul@example.com" and message["Subject"] == "Re: meeting"
        final = widget(sent, "composer")
        assert final["state"] == "sent" and final["demo"] is False
        assert "Gmail confirmed" in final["detail"]


async def test_a_saved_contact_fills_in_the_address(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        saved = await client.post(
            "/api/contacts", json={"name": "Rahul", "email": "rahul@example.com"}
        )
        assert saved.status_code in (200, 201), saved.text
        await say(client, await start(client), "Reply to Rahul saying I'll attend the meeting")
        data = await settled(client)
        assert board_status(data)["Draft and send an email"] == "waiting_for_approval"
        assert widget(data, "composer")["to"] == "rahul@example.com"


async def test_a_send_gmail_cannot_confirm_is_reported_as_unconfirmed(tmp_path: Path) -> None:
    world = World()
    world.google.drop_sent_label = True
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        await client.post("/api/contacts", json={"name": "Rahul", "email": "rahul@example.com"})
        await say(client, await start(client), "Reply to Rahul saying yes")
        draft = widget(await settled(client), "composer")
        task = draft["send"]["action_id"].split(":")[1]
        result = await act(client, f"task.approve:{task}", {"body": "yes"})
        board = board_status(result)
        assert board["Draft and send an email"] == "failed"
        assert widget(result, "composer")["state"] == "failed"
        again = await act(client, f"task.retry:{task}")
        assert again["result"] == "check_first"  # never resent blindly
        assert len(world.google.sent) == 1


async def test_an_expired_access_token_is_refreshed_quietly(tmp_path: Path) -> None:
    world = World()
    world.google.add(make_message("m1", labels=("INBOX", "IMPORTANT")))
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        world.google.expire_access()
        await say(client, await start(client), "Check my important emails")
        data = await settled(client)
        assert widget(data, "email_list")["demo"] is False
        grants = [r.get("grant_type") for r in world.google.token_requests]
        assert "refresh_token" in grants


async def test_a_revoked_connection_is_reported_and_removed(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        world.google.refresh_error = "invalid_grant"
        world.google.expire_access()
        await say(client, await start(client), "Check my important emails")
        data = await settled(client)
        status = board_status(data)
        assert status["Find important email"] == "failed"
        board = widget(data, "task_board")
        assert "connect google again" in board["tasks"][0]["detail"].lower()
        assert (await client.get("/api/google/status")).json()["gmail"]["connected"] is False


async def test_disconnecting_revokes_at_google_and_returns_to_practice(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        gone = await client.post("/api/google/disconnect", json={"service": "gmail"})
        assert gone.status_code == 200
        assert world.google.revoked == ["refresh-1"]
        assert (await client.get("/api/google/status")).json()["gmail"]["connected"] is False
        await say(client, await start(client), "Check my important emails")
        assert widget(await settled(client), "email_list")["demo"] is True


async def test_authorization_that_is_denied_does_not_connect(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        got = await client.get("/api/google/connect?service=gmail")
        state = state_of(str(got.json()["url"]))
        denied = await client.get(
            f"/api/google/callback?error=access_denied&state={state}", follow_redirects=False
        )
        assert denied.status_code == 303 and denied.headers["location"].endswith("#google=error")
        assert (await client.get("/api/google/status")).json()["gmail"]["connected"] is False


async def test_without_google_credentials_connect_explains_and_demo_continues(
    tmp_path: Path,
) -> None:
    world = World()
    async with live_site(tmp_path, world, google=False, whatsapp=False) as s, s.browser() as client:
        await sign_up(client)
        status = (await client.get("/api/google/status")).json()
        assert status["configured"] is False
        refused = await client.get("/api/google/connect?service=gmail")
        assert refused.status_code == 503
        assert "isn't set up" in refused.text or "not set up" in refused.text.lower()
        await say(client, await start(client), "Check my important emails")
        assert widget(await settled(client), "email_list")["demo"] is True


async def test_another_person_cannot_use_my_gmail(tmp_path: Path) -> None:
    world = World()
    world.google.add(make_message("m1", subject="Private", labels=("INBOX", "IMPORTANT")))
    async with live_site(tmp_path, world) as s, s.browser() as asha, s.browser() as ravi:
        await sign_up(asha)
        await sign_up(ravi, "ravi@example.com")
        await connect(asha, world, "gmail")
        await say(ravi, await start(ravi), "Check my important emails")
        assert widget(await settled(ravi), "email_list")["demo"] is True  # Ravi is not connected
        assert (await ravi.get("/api/google/status")).json()["gmail"]["connected"] is False


# Calendar ---------------------------------------------------------------------------------------


async def test_a_meeting_is_checked_approved_created_and_read_back(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "calendar")
        await say(client, await start(client), "Schedule a meeting with Rahul tomorrow at 3 PM")
        data = await settled(client)
        preview = widget(data, "event_preview")
        assert preview["demo"] is False and preview["state"] == "draft"
        assert board_status(data)["Check availability"] == "completed"
        assert world.gcal.sent("POST") == []  # nothing created before approval
        task = preview["approve"]["action_id"].split(":")[1]
        done = await act(client, f"task.approve:{task}", preview["fields"])
        assert done["result"] == "ok"
        created = widget(done, "event_preview")
        assert created["state"] == "created" and created["demo"] is False
        assert len(world.gcal.sent("POST")) == 1
        assert any("Rahul" in e.get("summary", "") for e in world.gcal.events.values())
        again = await act(client, f"task.approve:{task}", preview["fields"])
        assert again["result"] == "not_waiting" and len(world.gcal.sent("POST")) == 1


async def test_a_clash_is_shown_before_approval(tmp_path: Path) -> None:
    world = World()
    later = datetime.now(IST) + timedelta(days=3)  # an absolute date, whatever day the test runs
    day = later.date().isoformat()
    world.gcal.add(
        summary="Dentist",
        start={"dateTime": f"{day}T15:00:00+05:30"},
        end={"dateTime": f"{day}T15:30:00+05:30"},
    )
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "calendar")
        spoken = f"{later.day} {later.strftime('%B')}"
        await say(client, await start(client), f"Schedule a meeting with Rahul on {spoken} at 3 PM")
        preview = widget(await settled(client), "event_preview")
        assert [c["title"] for c in preview["conflicts"]] == ["Dentist"]


# WhatsApp ---------------------------------------------------------------------------------------


async def test_a_whatsapp_message_is_sent_after_approval_and_shows_the_real_status(
    tmp_path: Path,
) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await client.post("/api/contacts", json={"name": "Amma", "phone": "+919876543210"})
        await say(client, await start(client), "Send Amma a WhatsApp message saying I'll be late")
        data = await settled(client)
        draft = widget(data, "composer")
        assert draft["demo"] is False and draft["to"] == "+919876543210"
        assert world.wa.requests == []  # nothing sent yet
        task = draft["send"]["action_id"].split(":")[1]
        sent = await act(client, f"task.approve:{task}", {"to": "Amma", "body": "I'll be late"})
        assert sent["result"] == "ok"
        body = world.wa.bodies()[0]
        assert body["text"]["body"] == "I'll be late"  # type: ignore[index]
        assert str(body["to"]).endswith("919876543210")
        final = widget(sent, "composer")
        assert final["state"] == "sent" and final["demo"] is False
        assert "not delivered yet" in final["detail"].lower()  # accepted is not delivered

        payload = json.dumps(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "statuses": [
                                        {
                                            "id": "wamid.TEST1",
                                            "status": "delivered",
                                            "timestamp": "1700000000",
                                            "recipient_id": "919876543210",
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            }
        ).encode()
        bad = await client.post(
            "/api/whatsapp/webhook", content=payload, headers=signed(payload, "wrong-secret")
        )
        assert bad.status_code == 403
        good = await client.post("/api/whatsapp/webhook", content=payload, headers=signed(payload))
        assert good.status_code == 200
        later = await client.get("/api/tasks/current")
        assert widget(later.json(), "composer")["detail"] == "Delivered."


async def test_the_webhook_handshake_checks_the_verify_token(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "42"}
        ok = await client.get("/api/whatsapp/webhook", params=params)
        assert ok.status_code == 200 and ok.text == "42"
        wrong = await client.get(
            "/api/whatsapp/webhook", params={**params, "hub.verify_token": "x"}
        )
        assert wrong.status_code == 403


async def test_whatsapp_errors_are_plain_and_never_a_fake_success(tmp_path: Path) -> None:
    for mode, words in (
        ("code:131047", "24 hours"),
        ("code:131030", "allow list"),
        ("401", "renew"),
    ):
        world = World()
        world.wa.mode = mode
        async with live_site(tmp_path / mode.replace(":", "_"), world) as s, s.browser() as client:
            (tmp_path / mode.replace(":", "_")).mkdir(exist_ok=True)
            await sign_up(client)
            await say(
                client, await start(client), "Send +919876543210 a WhatsApp message saying hi"
            )
            data = await settled(client)
            task = widget(data, "composer")["send"]["action_id"].split(":")[1]
            result = await act(client, f"task.approve:{task}", {"body": "hi"})
            detail = widget(result, "task_board")["tasks"][0]["detail"].lower()
            assert board_status(result)["Send WhatsApp message"] == "failed", mode
            assert words in detail, (mode, detail)
            assert widget(result, "composer")["state"] == "failed"


async def test_without_whatsapp_credentials_messages_are_labelled_practice(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world, whatsapp=False) as s, s.browser() as client:
        await sign_up(client)
        await say(client, await start(client), "Send Amma a WhatsApp message saying I'll be late")
        draft = widget(await settled(client), "composer")
        assert draft["demo"] is True
        assert world.wa.requests == []


# Calendar: moving and cancelling an existing event ---------------------------------------------


def seed_call(
    world: World, when: datetime, title: str = "Call with Rahul", who: str = "rahul@example.com"
) -> str:
    day = when.date().isoformat()
    return world.gcal.add(
        summary=title,
        start={"dateTime": f"{day}T17:00:00+05:30"},
        end={"dateTime": f"{day}T17:30:00+05:30"},
        attendees=[{"email": who}],
    )


async def test_cancelling_an_event_waits_for_approval_then_removes_exactly_that_one(
    tmp_path: Path,
) -> None:
    world = World()
    tomorrow = datetime.now(IST) + timedelta(days=1)
    target = seed_call(world, tomorrow)
    other = seed_call(world, tomorrow, "Lunch with Meera", "meera@example.com")
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "calendar")
        await say(client, await start(client), "Cancel my call with Rahul tomorrow")
        data = await settled(client)
        card = widget(data, "event_preview")
        assert (
            card["title"] == "Cancel this event?" and card["fields"]["title"] == "Call with Rahul"
        )
        assert target in world.gcal.events and not world.gcal.sent("DELETE")  # not yet
        task = card["approve"]["action_id"].split(":")[1]
        done = await act(client, f"task.approve:{task}")
        assert done["result"] == "ok"
        deletes = world.gcal.sent("DELETE")
        assert len(deletes) == 1 and deletes[0].url.path.endswith(f"/{target}")
        assert not any(r.url.path.endswith(f"/{other}") for r in deletes)  # the other is untouched
        assert board_status(done)["Cancel calendar event"] == "completed"


async def test_moving_an_event_shows_where_it_is_now_and_moves_it_once(tmp_path: Path) -> None:
    world = World()
    tomorrow = datetime.now(IST) + timedelta(days=1)
    target = seed_call(world, tomorrow)
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "calendar")
        await say(client, await start(client), "Move my call with Rahul to 6 PM")
        card = widget(await settled(client), "event_preview")
        assert card["title"] == "Move event" and card["fields"]["start"] == "18:00"
        assert "Now" in card["detail"]
        assert world.gcal.events[target]["start"]["dateTime"].endswith("17:00:00+05:30")
        task = card["approve"]["action_id"].split(":")[1]
        done = await act(client, f"task.approve:{task}", card["fields"])
        assert done["result"] == "ok"
        assert world.gcal.events[target]["start"]["dateTime"].startswith(f"{tomorrow.date()}T18:00")
        assert len(world.gcal.sent("PATCH")) == 1
        again = await act(client, f"task.approve:{task}", card["fields"])
        assert again["result"] == "not_waiting" and len(world.gcal.sent("PATCH")) == 1


async def test_no_matching_event_asks_instead_of_guessing(tmp_path: Path) -> None:
    world = World()
    seed_call(world, datetime.now(IST) + timedelta(days=1))
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "calendar")
        await say(client, await start(client), "Cancel my meeting with Zed")
        data = await settled(client)
        assert board_status(data)["Cancel calendar event"] == "waiting_for_input"
        assert "couldn't find" in widget(data, "task_board")["tasks"][0]["detail"]
        assert not world.gcal.sent("DELETE")


async def test_several_matching_events_are_listed_not_guessed(tmp_path: Path) -> None:
    world = World()
    tomorrow = datetime.now(IST) + timedelta(days=1)
    seed_call(world, tomorrow)
    seed_call(world, tomorrow + timedelta(days=1), "Review with Rahul")
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "calendar")
        await say(client, await start(client), "Cancel my meeting with Rahul")
        data = await settled(client)
        assert board_status(data)["Cancel calendar event"] == "waiting_for_input"
        detail = widget(data, "task_board")["tasks"][0]["detail"]
        assert "More than one" in detail and "Call with Rahul" in detail
        assert not world.gcal.sent("DELETE")


async def test_a_missing_event_name_is_asked_for_inside_the_calendar_workflow(
    tmp_path: Path,
) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        headers = await start(client)
        asked = await say(client, headers, "Cancel the meeting")
        assert asked["workflow"] == {"kind": "calendar", "waiting_for": "find"}
        assert "Which event" in asked["assistant_text"]
        answered = await say(client, headers, "Rahul")
        assert answered["workflow"]["kind"] == "calendar" and answered["plan"]["id"]


# Connected apps: accurate states --------------------------------------------------------------


async def apps(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get("/api/integrations")
    assert response.status_code == 200, response.text
    return {a["id"]: a for a in response.json()["apps"]}


async def test_unconfigured_apps_say_what_is_missing_without_revealing_anything(
    tmp_path: Path,
) -> None:
    world = World()
    async with live_site(tmp_path, world, google=False, whatsapp=False) as s, s.browser() as client:
        await sign_up(client)
        cards = await apps(client)
        for name in ("gmail", "calendar"):
            assert cards[name]["state"] == "configuration_required"
            assert cards[name]["setup"] == ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]
            assert cards[name]["can_connect"] is False
        assert cards["whatsapp"]["state"] == "configuration_required"
        assert "WHATSAPP_ACCESS_TOKEN" in cards["whatsapp"]["setup"]
        assert cards["swiggy"]["state"] == "not_connected"
        assert "demo" in cards["swiggy"]["detail"].lower()


async def test_a_configured_but_unconnected_app_offers_connect(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        cards = await apps(client)
        assert cards["gmail"]["state"] == "not_connected" and cards["gmail"]["can_connect"] is True
        assert cards["gmail"]["permissions"], "the permissions are shown before connecting"


async def test_connected_is_shown_only_after_the_connection_is_verified(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        world.google.expire_access()
        before = len(world.google.token_requests)
        card = (await apps(client))["gmail"]
        assert card["state"] == "connected" and card["account"] == "me@example.com"
        assert card["last_connected"] and card["can_disconnect"] is True
        assert len(world.google.token_requests) > before  # it really asked Google
        assert (await apps(client))["calendar"]["state"] == "not_connected"


async def test_a_revoked_connection_is_authorization_failed_not_connected(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        world.google.refresh_error = "invalid_grant"
        world.google.expire_access()
        card = (await apps(client))["gmail"]
        assert card["state"] == "authorization_failed"
        assert card["can_connect"] is True and card["can_disconnect"] is False
        assert "connect again" in card["detail"].lower()
        again = (await apps(client))["gmail"]  # and it stays honest on the next look
        assert again["state"] == "authorization_failed"


async def test_a_declined_authorization_is_reported_with_the_reason(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        got = await client.get("/api/google/connect?service=calendar")
        state = state_of(str(got.json()["url"]))
        await client.get(
            f"/api/google/callback?error=access_denied&state={state}", follow_redirects=False
        )
        card = (await apps(client))["calendar"]
        assert card["state"] == "authorization_failed"
        assert "didn't allow" in card["detail"]
        assert (await apps(client))["gmail"]["state"] == "not_connected"  # only that one


async def test_disconnecting_returns_the_card_to_not_connected(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        await client.post("/api/google/disconnect", json={"service": "gmail"})
        card = (await apps(client))["gmail"]
        assert card["state"] == "not_connected" and card["can_connect"] is True


async def test_whatsapp_is_connected_only_when_meta_accepts_the_token(tmp_path: Path) -> None:
    for mode, expected in (
        ("ok", "connected"),
        ("401", "authorization_failed"),
        ("500", "unavailable"),
    ):
        world = World()
        world.wa.mode = mode
        folder = tmp_path / f"wa_{mode}"
        folder.mkdir()
        async with live_site(folder, world) as s, s.browser() as client:
            await sign_up(client)
            card = (await apps(client))["whatsapp"]
            assert card["state"] == expected, (mode, card)
            if expected == "connected":
                assert card["account"] == "Business number ending 1234"
            assert card["notes"], "the 24-hour and test-number rules are stated"


async def test_the_screen_needs_a_sign_in_and_shows_only_ones_own_connections(
    tmp_path: Path,
) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as asha, s.browser() as ravi:
        assert (await asha.get("/api/integrations")).status_code == 401
        await sign_up(asha)
        await sign_up(ravi, "ravi@example.com")
        await connect(asha, world, "gmail")
        assert (await apps(asha))["gmail"]["state"] == "connected"
        assert (await apps(ravi))["gmail"]["state"] == "not_connected"


async def test_no_secret_ever_appears_in_the_connected_apps_payload(tmp_path: Path) -> None:
    world = World()
    async with live_site(tmp_path, world) as s, s.browser() as client:
        await sign_up(client)
        await connect(client, world, "gmail")
        body = (await client.get("/api/integrations")).text
        for secret in ("secret", "wa-token", APP_SECRET, VERIFY_TOKEN, "refresh-1", "access-"):
            assert secret not in body.replace("client_secret", ""), secret
