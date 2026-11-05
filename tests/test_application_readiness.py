"""The public demo, the provider chain, scripted understanding, and the hosted Swiggy sign-in."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.config import ConfigurationError, Settings, load_settings
from app.dining.workflow import known_area
from app.llm.chain import ChainedExtractor, ProviderStep, build_extractor
from app.llm.extractor import ClarificationContext, GroceryIntent, IntentExtractionError
from app.llm.scripted import ScriptedExtractor
from app.oauth_web import COOKIE_NAME
from app.timing import begin_turn, stage
from app.web import create_app

BASE = "https://saathi.example"
SCRIPTED = Settings(demo_understanding="scripted")


class SilentTTS:
    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        del text
        destination = Path(output_path)
        destination.write_bytes(b"RIFF")
        return destination


@asynccontextmanager
async def running(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncGenerator[httpx.AsyncClient]:
    app: FastAPI = create_app(
        settings=settings,
        tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
        oauth_transport=transport,
    )
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
        async with client:
            yield client


class Conversation:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.session = ""
        self.last: dict[str, object] = {}

    async def start(self, language: str = "en-IN") -> None:
        response = await self.client.post("/api/sessions", json={"language": language})
        assert response.status_code == 200, response.text
        self.last = response.json()
        self.session = str(self.last["session_id"])

    async def say(self, text: str) -> dict[str, object]:
        response = await self.client.post(
            "/api/session/text", json={"text": text}, headers={"X-Session-ID": self.session}
        )
        assert response.status_code == 200, response.text
        self.last = response.json()
        return self.last

    def task(self, capability: str) -> dict[str, object]:
        tasks = cast(list[dict[str, object]], self.last["tasks"])
        return next(item for item in tasks if item["capability"] == capability)


# Journeys, in every language, through the same route the page uses.

GROCERY = {
    "en-IN": ("Two kilos of rice and one litre of milk", "Yes"),
    "hi-IN": ("दो किलो चावल और एक लीटर दूध", "हाँ"),
    "kn-IN": ("ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು", "ಹೌದು"),
}


@pytest.mark.parametrize("language", GROCERY)
async def test_grocery_journey(language: str) -> None:
    request, yes = GROCERY[language]
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start(language)
        reply = await chat.say(request)
        assert reply["state"] == "READBACK"
        assert reply["capability"] == "grocery"
        assert reply["scripted_understanding"] is True
        reply = await chat.say(yes)
        assert reply["state"] == "DONE"
        assert reply["commerce_mode"] == "mock"


FOOD = {
    "en-IN": ("I want something for dinner", "Chicken biryani", "The second one", "Yes"),
    "hi-IN": ("रात के खाने के लिए कुछ चाहिए", "चिकन बिरयानी", "दूसरा", "हाँ"),
    "kn-IN": ("ರಾತ್ರಿ ಊಟಕ್ಕೆ ಏನಾದರೂ ಬೇಕು", "ಚಿಕನ್ ಬಿರಿಯಾನಿ", "ಎರಡನೆಯದು", "ಹೌದು"),
}


@pytest.mark.parametrize("language", FOOD)
async def test_food_journey_clarifies_then_offers_restaurants(language: str) -> None:
    ask, dish, second, yes = FOOD[language]
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start(language)
        first = await chat.say(ask)
        assert first["capability"] == "food"
        assert first["state"] != "READBACK"
        offered = await chat.say(dish)
        assert offered["options"], offered
        assert (await chat.say(second))["state"] == "READBACK"
        assert (await chat.say(yes))["state"] == "DONE"


TABLE = {
    "en-IN": ("Book a table for two in Indiranagar tomorrow at seven, Italian", "First", "Yes"),
    "hi-IN": ("कल सात बजे इंदिरानगर में दो लोगों के लिए इटालियन टेबल बुक करो", "पहला", "हाँ"),
    "kn-IN": ("ನಾಳೆ ಏಳು ಗಂಟೆಗೆ ಇಂದಿರಾನಗರದಲ್ಲಿ ಇಬ್ಬರಿಗೆ ಇಟಾಲಿಯನ್ ಟೇಬಲ್ ಬುಕ್ ಮಾಡಿ", "ಮೊದಲನೆಯದು", "ಹೌದು"),
}


@pytest.mark.parametrize("language", TABLE)
async def test_table_journey(language: str) -> None:
    ask, first, yes = TABLE[language]
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start(language)
        offered = await chat.say(ask)
        assert offered["capability"] == "dining"
        assert offered["options"]
        assert (await chat.say(first))["state"] == "READBACK"
        assert (await chat.say(yes))["state"] == "DONE"


async def test_groceries_wait_while_a_table_is_booked_and_can_be_resumed() -> None:
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start()
        await chat.say("Two kilos of rice and one litre of milk")
        grocery_total = chat.task("grocery")["total_inr"]

        await chat.say("Book a table for two in Indiranagar tomorrow at seven, Italian")
        waiting = chat.task("grocery")
        assert waiting["open"] is True
        assert waiting["active"] is False
        assert waiting["total_inr"] == grocery_total

        await chat.say("First")
        booked = await chat.say("Yes")
        assert booked["capability"] == "dining"
        assert booked["state"] == "DONE"
        assert chat.task("grocery")["state"] != "DONE", "the yes must only reach the table"

        back = await chat.say("Back to my groceries")
        assert back["capability"] == "grocery"
        assert back["state"] == "READBACK"
        assert "Shall I place this order?" in str(back["assistant_text"])
        assert (await chat.say("Yes"))["state"] == "DONE"


async def test_resume_button_reads_back_afresh_without_ordering() -> None:
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start()
        await chat.say("Two kilos of rice and one litre of milk")
        await chat.say("Book a table for two in Indiranagar tomorrow at seven, Italian")
        response = await client.post(
            "/api/session/action",
            json={"action": "resume", "capability": "grocery"},
            headers={"X-Session-ID": chat.session},
        )
        assert response.status_code == 200
        assert response.json()["state"] == "READBACK"
        assert response.json()["order_ids"] == []


async def test_a_waiting_task_is_not_confirmed_by_resume_words_alone() -> None:
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start()
        reply = await chat.say("Back to my groceries")
        assert reply["order_ids"] == []
        assert reply["tasks"] == []


async def test_typed_turn_reports_each_stage_and_rejects_empty_text() -> None:
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start()
        reply = await chat.say("One litre of milk")
        timings = cast(dict[str, int], reply["timings_ms"])
        assert set(timings) == {
            "stt",
            "understanding",
            "provider",
            "speech",
            "orchestration",
            "total",
        }
        empty = await client.post(
            "/api/session/text", json={"text": "  "}, headers={"X-Session-ID": chat.session}
        )
        assert empty.status_code == 422


def test_timer_counts_time_outside_named_stages_as_orchestration() -> None:
    timer = begin_turn()
    with stage("speech"):
        pass
    report = timer.report()
    assert report["total"] >= report["speech"]
    assert report["orchestration"] >= 0


@pytest.mark.parametrize("spoken", ["Indiranagar", "इंदिरानगर", "ಇಂದಿರಾನಗರ", "ಇಂದಿರಾನಗರದಲ್ಲಿ"])
def test_a_place_is_recognised_in_any_script(spoken: str) -> None:
    assert known_area(spoken) == (12.9784, 77.6408)


def test_an_unknown_place_is_not_guessed() -> None:
    assert known_area("Atlantis") is None


async def test_slot_dates_are_spoken_as_dates() -> None:
    async with running(SCRIPTED) as client:
        chat = Conversation(client)
        await chat.start()
        await chat.say("Book a table for two in Indiranagar tomorrow at nine, Italian")
        reply = await chat.say("First")
        assert not re.search(r"\d{4}-\d{2}-\d{2}", str(reply["assistant_text"]))


# Provider chain.


def _intent() -> GroceryIntent:
    return GroceryIntent(intent="unclear", items=[])


def _failing(name: str, calls: list[str], error: Exception) -> ProviderStep:
    def call(text: str, language: str, context: ClarificationContext | None) -> GroceryIntent:
        del text, language, context
        calls.append(name)
        raise error

    return ProviderStep(name, call)


def _working(name: str, calls: list[str]) -> ProviderStep:
    def call(text: str, language: str, context: ClarificationContext | None) -> GroceryIntent:
        del text, language, context
        calls.append(name)
        return _intent()

    return ProviderStep(name, call)


def test_chain_moves_on_after_any_failure() -> None:
    calls: list[str] = []
    timer = begin_turn()
    chain = ChainedExtractor(
        [
            _failing("groq", calls, IntentExtractionError("rate limited")),
            _failing("gemini", calls, RuntimeError("boom")),
            _working("openrouter", calls),
        ]
    )
    chain.extract("milk", language="en-IN")
    assert calls == ["groq", "gemini", "openrouter"]
    assert timer.understood_by == "openrouter"


def test_chain_reports_failure_when_every_provider_fails() -> None:
    chain = ChainedExtractor([_failing("groq", [], IntentExtractionError("rate limited"))])
    with pytest.raises(IntentExtractionError):
        chain.extract("milk", language="en-IN")


def test_groq_goes_first_by_default_and_scripted_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    both = build_extractor(Settings(groq_api_key="test"))
    assert [step.name for step in both.steps] == ["groq", "gemini"]
    primary = build_extractor(Settings(groq_api_key="test", llm_primary="gemini"))
    assert [step.name for step in primary.steps] == ["gemini", "groq"]
    with_demo = build_extractor(Settings(groq_api_key="test", demo_understanding="fallback"))
    assert [step.name for step in with_demo.steps] == ["groq", "gemini", "scripted"]
    assert [step.name for step in build_extractor(SCRIPTED).steps] == ["scripted"]


def test_openrouter_never_sees_the_public_or_a_real_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    local = build_extractor(Settings(openrouter_api_key="test"))
    assert [step.name for step in local.steps] == ["openrouter"]
    public = Settings(
        groq_api_key="test",
        openrouter_api_key="test",
        public_deployment=True,
        public_base_url=BASE,
    )
    assert [step.name for step in build_extractor(public).steps] == ["groq"]
    live = Settings(
        commerce_backend="swiggy", swiggy_live_access=True, groq_api_key="t", openrouter_api_key="t"
    )
    assert [step.name for step in build_extractor(live).steps] == ["groq"]


def test_no_provider_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(IntentExtractionError):
        build_extractor(Settings())


# Scripted understanding.


def test_scripted_understands_a_full_table_request() -> None:
    intent = ScriptedExtractor().extract(
        "Book a table for two in Indiranagar tomorrow at seven, Italian",
        language="en-IN",
        context=ClarificationContext(today="2026-11-05"),
    )
    assert intent.intent == "book_table"
    assert (intent.query, intent.area, intent.date, intent.time, intent.guests) == (
        "Italian",
        "Indiranagar",
        "2026-11-06",
        "19:00",
        2,
    )


def test_scripted_never_guesses_and_never_confirms() -> None:
    extractor = ScriptedExtractor()
    for text in ("what is the weather", "place the order", "yes please go ahead"):
        assert extractor.extract(text, language="en-IN").intent == "unclear"


# Fail-closed configuration.


@pytest.mark.parametrize(
    "extra",
    [
        {"COMMERCE_BACKEND": "swiggy", "SWIGGY_LIVE_ACCESS": "true"},
        {"PUBLIC_BASE_URL": "http://saathi.example"},
        {"PUBLIC_BASE_URL": "https://saathi.example/app"},
        {"PUBLIC_BASE_URL": ""},
    ],
)
def test_public_deployment_refuses_unsafe_settings(extra: dict[str, str]) -> None:
    values = {"PUBLIC_DEPLOYMENT": "true", "PUBLIC_BASE_URL": BASE, **extra}
    with pytest.raises(ConfigurationError):
        load_settings(values)


def test_scripted_understanding_is_refused_outside_mock() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(
            {
                "COMMERCE_BACKEND": "swiggy",
                "SWIGGY_LIVE_ACCESS": "true",
                "DEMO_UNDERSTANDING": "fallback",
            }
        )


def test_public_deployment_loads_when_it_is_safe() -> None:
    settings = load_settings({"PUBLIC_DEPLOYMENT": "true", "PUBLIC_BASE_URL": BASE})
    assert settings.public_deployment
    assert settings.commerce_backend == "mock"


def test_public_address_can_come_from_the_host() -> None:
    settings = load_settings({"PUBLIC_DEPLOYMENT": "true", "RENDER_EXTERNAL_URL": BASE})
    assert settings.public_base_url == BASE


# The public demo's limits.

PUBLIC = Settings(
    public_deployment=True,
    public_base_url=BASE,
    demo_understanding="scripted",
    public_daily_turn_limit=3,
)


async def test_public_budget_stops_spending_when_used_up() -> None:
    async with running(PUBLIC) as client:
        chat = Conversation(client)
        await chat.start()
        await chat.say("One litre of milk")
        await chat.say("Yes")
        over = await client.post(
            "/api/session/text", json={"text": "Milk"}, headers={"X-Session-ID": chat.session}
        )
        assert over.status_code == 429


async def test_public_session_cap() -> None:
    capped = Settings(
        public_deployment=True,
        public_base_url=BASE,
        demo_understanding="scripted",
        public_max_sessions=1,
    )
    async with running(capped) as client:
        await Conversation(client).start()
        refused = await client.post("/api/sessions", json={"language": "en-IN"})
        assert refused.status_code == 429


async def test_healthz_states_mode_and_the_exact_callback() -> None:
    async with running(PUBLIC) as client:
        body = (await client.get("/healthz")).json()
    assert body == {
        "status": "ok",
        "commerce_mode": "mock",
        "public_deployment": True,
        "oauth_callback": f"{BASE}/auth/callback",
    }


# The hosted Swiggy sign-in, against a stand-in authorization server.

Handler = Callable[[httpx.Request], httpx.Response]


def swiggy(seen: list[httpx.Request], *, register: int = 200, token: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/auth/register":
            return httpx.Response(register, json={"client_id": "client-1"})
        if request.url.path == "/auth/token":
            return httpx.Response(
                token, json={"access_token": "secret-access", "expires_in": 432000}
            )
        return httpx.Response(200, json={})

    return handle


async def _login(client: httpx.AsyncClient) -> str:
    response = await client.get("/auth/login")
    assert response.status_code == 302
    return response.headers["location"]


async def test_login_redirects_with_pkce_and_the_exact_redirect_uri() -> None:
    seen: list[httpx.Request] = []
    async with running(PUBLIC, httpx.MockTransport(swiggy(seen))) as client:
        location = await _login(client)
    parts = urlsplit(location)
    query = parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://mcp.swiggy.com/auth/authorize"
    assert query["redirect_uri"] == [f"{BASE}/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == ["client-1"]
    assert query["code_challenge"][0]
    assert query["state"][0]
    registered = json.loads(seen[0].content)
    assert registered["redirect_uris"] == [f"{BASE}/auth/callback"]


async def test_login_cookie_is_protected() -> None:
    async with running(PUBLIC, httpx.MockTransport(swiggy([]))) as client:
        response = await client.get("/auth/login")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie


async def test_callback_completes_once_and_keeps_tokens_out_of_logs_and_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[httpx.Request] = []
    caplog.set_level(logging.DEBUG)
    async with running(PUBLIC, httpx.MockTransport(swiggy(seen))) as client:
        state = parse_qs(urlsplit(await _login(client)).query)["state"][0]
        done = await client.get("/auth/callback", params={"code": "code-1", "state": state})
        assert done.status_code == 200
        assert "secret-access" not in done.text
        assert "mock shops" in done.text
        assert done.headers["cache-control"] == "no-store"
        assert (await client.get("/auth/status")).json() == {"connected": True}

        replay = await client.get("/auth/callback", params={"code": "code-1", "state": state})
        assert replay.status_code == 400

        await client.post("/auth/logout")
        assert (await client.get("/auth/status")).json() == {"connected": False}

    exchange = next(request for request in seen if request.url.path == "/auth/token")
    form = parse_qs(exchange.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["code-1"]
    assert form["redirect_uri"] == [f"{BASE}/auth/callback"]
    assert form["client_id"] == ["client-1"]
    assert form["code_verifier"][0]
    logout = next(request for request in seen if request.url.path == "/auth/logout")
    assert logout.headers["authorization"] == "Bearer secret-access"
    # The test client logs its own requests; the app's records must not hold either secret.
    app_logs = [record.getMessage() for record in caplog.records if record.name != "httpx"]
    assert not [line for line in app_logs if "secret-access" in line or "code-1" in line]


async def test_callback_refuses_an_unknown_state_or_another_browser() -> None:
    seen: list[httpx.Request] = []
    async with running(PUBLIC, httpx.MockTransport(swiggy(seen))) as client:
        state = parse_qs(urlsplit(await _login(client)).query)["state"][0]
        forged = await client.get("/auth/callback", params={"code": "c", "state": "forged"})
        assert forged.status_code == 400
        client.cookies.delete(COOKIE_NAME)
        other = await client.get("/auth/callback", params={"code": "c", "state": state})
        assert other.status_code == 400
    assert not [request for request in seen if request.url.path == "/auth/token"]


async def test_callback_handles_a_declined_sign_in_without_echoing_it() -> None:
    seen: list[httpx.Request] = []
    async with running(PUBLIC, httpx.MockTransport(swiggy(seen))) as client:
        state = parse_qs(urlsplit(await _login(client)).query)["state"][0]
        declined = await client.get(
            "/auth/callback", params={"error": "<script>bad()</script>", "state": state}
        )
    assert declined.status_code == 400
    assert "<script>" not in declined.text
    assert not [request for request in seen if request.url.path == "/auth/token"]


async def test_failed_token_exchange_does_not_connect() -> None:
    transport = httpx.MockTransport(swiggy([], token=400))
    async with running(PUBLIC, transport) as client:
        state = parse_qs(urlsplit(await _login(client)).query)["state"][0]
        failed = await client.get("/auth/callback", params={"code": "c", "state": state})
        assert failed.status_code == 502
        assert (await client.get("/auth/status")).json() == {"connected": False}


async def test_refused_registration_explains_the_allowlist() -> None:
    transport = httpx.MockTransport(swiggy([], register=403))
    async with running(PUBLIC, transport) as client:
        response = await client.get("/auth/login")
    assert response.status_code == 503
    assert "allowlisted" in response.text


async def test_sign_in_is_off_without_a_public_address() -> None:
    async with running(Settings(demo_understanding="scripted")) as client:
        assert (await client.get("/auth/login")).status_code == 404
        assert (await client.get("/auth/callback")).status_code == 404


async def test_connecting_swiggy_does_not_change_the_demo_to_live() -> None:
    async with running(PUBLIC, httpx.MockTransport(swiggy([]))) as client:
        state = parse_qs(urlsplit(await _login(client)).query)["state"][0]
        await client.get("/auth/callback", params={"code": "c", "state": state})
        addresses = (await client.get("/api/addresses")).json()
    assert addresses["commerce_mode"] == "mock"
