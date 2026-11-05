"""Live-path behaviour that does not need Swiggy's approval to build: tool discovery, forbidden
responses, keeping a login fresh, and resting a failing language provider. Stand-in Swiggy only."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.commerce.base import CommerceNotAttempted
from app.commerce.order_journal import AttemptStatus
from app.llm.chain import ChainedExtractor, ProviderStep
from app.llm.extractor import ClarificationContext, GroceryIntent, IntentExtractionError
from tests import fake_swiggy
from tests.fake_swiggy import FakeSwiggy
from tests.test_multi_user_live import (
    CALLBACK,
    database_url,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    live_settings,
    world,
)


async def test_a_tool_swiggy_does_not_offer_is_never_called(
    database_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = dict(fake_swiggy._TOOLS)  # pyright: ignore[reportPrivateUsage]
    tools["im"] = tuple(tool for tool in tools["im"] if tool != "checkout")
    monkeypatch.setattr(fake_swiggy, "_TOOLS", tools)
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        reply = await asha.speak_until_readback("One litre of milk")
        assert reply["state"] == "READBACK"
        reply = await asha.say("Yes")
        assert reply["state"] != "DONE"
        assert "checkout" not in w.fake.tools_called(), "never sent"
        assert w.fake.accounts["asha"].orders == []
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        statuses = {a.status for a in w.service.journal(user.id).attempts()}
        assert statuses <= {AttemptStatus.NOT_PLACED}, "an unsent order cannot be uncertain"


@pytest.mark.parametrize("mode", ["http_403", "rpc_forbidden"])
async def test_forbidden_is_reported_as_not_attempted(
    database_url: str,  # noqa: F811
    mode: str,
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        adapters = w.service.adapters(user.id)
        await adapters.grocery.get_addresses()
        w.fake.fail_next("get_cart", mode)
        with pytest.raises(CommerceNotAttempted):
            await adapters.grocery.get_cart()
        cart = await adapters.grocery.get_cart()  # the next call is unaffected
        assert cart.items == []
        await adapters.aclose()


async def test_a_forbidden_checkout_leaves_no_uncertain_order(
    database_url: str,  # noqa: F811
) -> None:
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.start()
        await asha.speak_until_readback("One litre of milk")
        w.fake.fail_next("checkout", "http_403")
        reply = await asha.say("Yes")
        assert reply["state"] != "DONE"
        assert w.fake.accounts["asha"].orders == []
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        statuses = [a.status for a in w.service.journal(user.id).attempts()]
        assert statuses == [AttemptStatus.NOT_PLACED]
        again = await asha.start()
        assert again["state"] != "ORDER_UNKNOWN", "a refused order must not block the person"


async def test_a_regular_visitors_login_is_kept_fresh(database_url: str) -> None:  # noqa: F811
    fake = FakeSwiggy({CALLBACK}, access_lifetime=3600)  # inside the two-day window
    async with world(live_settings(database_url), fake) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        assert fake.refreshes == 0
        me = (await asha.http.get("/api/account/me")).json()
        assert me["live"]["connected"] is True
        assert fake.refreshes == 1


async def test_a_fresh_login_is_left_alone(database_url: str) -> None:  # noqa: F811
    async with world(live_settings(database_url)) as w:  # five-day token
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        await asha.http.get("/api/account/me")
        assert w.fake.refreshes == 0


def test_a_failing_language_provider_rests_while_the_next_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.llm.chain.time.monotonic", lambda: clock["now"])

    def failing(text: str, language: str, context: ClarificationContext | None) -> GroceryIntent:
        del text, language, context
        calls.append("groq")
        raise IntentExtractionError("Groq request failed (rate_limit)")

    def working(text: str, language: str, context: ClarificationContext | None) -> GroceryIntent:
        del text, language, context
        calls.append("gemini")
        return cast(GroceryIntent, cast(Any, object()))

    chain = ChainedExtractor([ProviderStep("groq", failing), ProviderStep("gemini", working)])
    chain.extract("rice")
    chain.extract("milk")
    assert calls == ["groq", "gemini", "gemini"], "groq rests after failing"
    clock["now"] += 31
    chain.extract("rice")
    assert calls[-2:] == ["groq", "gemini"], "and is tried again later"


def test_the_last_provider_is_always_tried() -> None:
    calls: list[str] = []

    def failing(text: str, language: str, context: ClarificationContext | None) -> GroceryIntent:
        del text, language, context
        calls.append("only")
        raise IntentExtractionError("down")

    chain = ChainedExtractor([ProviderStep("only", failing)])
    for _ in range(2):
        with pytest.raises(IntentExtractionError):
            chain.extract("rice")
    assert calls == ["only", "only"]
