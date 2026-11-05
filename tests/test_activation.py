"""The activation-day tools, against the stand-in Swiggy."""

from __future__ import annotations

import argparse
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.accounts.db import connect, migrate
from app.accounts.journal import DatabaseOrderJournal
from app.accounts_admin import generate_key, run
from app.activation_check import check_config, check_reads, staging_journey
from app.commerce.order_journal import AttemptStatus, new_attempt
from app.commerce.swiggy import SWIGGY_MCP_URL
from app.config import load_settings
from app.orchestrator.checkout import AttemptRefused, CheckoutPolicy, attempt_guard
from tests.test_multi_user_live import (
    CALLBACK,
    database_url,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    live_settings,
    world,
)


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, bool, str]] = []

    def __call__(self, name: str, passed: bool, detail: str = "") -> None:
        self.lines.append((name, passed, detail))

    @property
    def failed(self) -> list[str]:
        return [name for name, passed, _ in self.lines if not passed]


async def test_reads_pass_and_change_nothing(database_url: str) -> None:  # noqa: F811
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        report = Report()
        await check_reads(w.service, user, report)
        assert report.failed == []
        assert {"Instamart: search", "Food: search", "Dineout: search"} <= {
            name for name, _, _ in report.lines
        }
        written = {"update_cart", "clear_cart", "checkout", "update_food_cart", "book_table"}
        assert not written & set(w.fake.tools_called())
        # Only counts and names, so the output is safe to paste into a ticket.
        assert "CMH" not in str(report.lines)


async def test_reads_say_when_the_person_has_not_connected(database_url: str) -> None:  # noqa: F811
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.register("asha@example.com")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        report = Report()
        await check_reads(w.service, user, report)
        assert report.failed == ["this person has connected Swiggy"]
        assert w.fake.calls == []


async def test_a_missing_tool_is_reported(database_url: str) -> None:  # noqa: F811
    from tests import fake_swiggy

    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        original = fake_swiggy._TOOLS["im"]  # pyright: ignore[reportPrivateUsage]
        fake_swiggy._TOOLS["im"] = tuple(  # pyright: ignore[reportPrivateUsage]
            tool for tool in original if tool != "checkout"
        )
        try:
            report = Report()
            await check_reads(w.service, user, report)
        finally:
            fake_swiggy._TOOLS["im"] = original  # pyright: ignore[reportPrivateUsage]
        assert report.failed == ["Instamart: has every tool Saathi uses"]
        assert ("Instamart: has every tool Saathi uses", False, "checkout") in report.lines


async def test_staging_journey_runs_all_three_and_journals(database_url: str) -> None:  # noqa: F811
    async with world(live_settings(database_url)) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        report = Report()
        await staging_journey(w.service, user, report)
        assert report.failed == []
        account = w.fake.accounts["asha"]
        assert len(account.orders) == len(account.food_orders) == len(account.bookings) == 1
        attempts = w.service.journal(user.id).attempts()
        assert {a.domain for a in attempts} == {"grocery", "food", "dining"}
        assert all(a.status is AttemptStatus.PLACED for a in attempts)


async def test_staging_journey_refuses_production(database_url: str) -> None:  # noqa: F811
    settings = live_settings(database_url, swiggy_mcp_url=SWIGGY_MCP_URL)
    async with world(settings) as w:
        asha = w.person()
        await asha.onboard("asha@example.com", "asha")
        user = w.service.store.user_by_email("asha@example.com")
        assert user
        report = Report()
        await staging_journey(w.service, user, report)
        assert report.failed == ["this is the staging server"]
        assert w.fake.calls == []


async def test_config_check_names_the_exact_redirect(database_url: str) -> None:  # noqa: F811
    async with world(live_settings(database_url, groq_api_key="x", gnani_api_key="y")) as w:
        report = Report()
        check_config(w.service.settings, w.service, report)
        assert ("redirect URI to allowlist", True, CALLBACK) in report.lines
        assert ("real checkout", True, "on") in report.lines


def test_admin_grants_only_a_verified_person(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = generate_key()
    settings = load_settings(
        {
            "DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite'}",
            "TOKEN_ENCRYPTION_KEYS": key,
            "PUBLIC_BASE_URL": "https://saathi.example",
        }
    )
    database = connect(settings.database_url or "")
    migrate(database)
    from app.accounts.store import AccountStore

    store = AccountStore(database)
    store.create_user("asha@example.com", "x")

    def admin(command: str) -> int:
        return run(argparse.Namespace(command=command, email="asha@example.com"), settings)

    assert admin("grant") == 1
    user = store.user_by_email("asha@example.com")
    assert user and not user.live_access
    assert admin("verify") == 0
    assert admin("grant") == 0
    user = store.user_by_email("asha@example.com")
    assert user and user.live_access
    assert admin("revoke") == 0
    user = store.user_by_email("asha@example.com")
    assert user and not user.live_access
    capsys.readouterr()
    assert admin("reset-link") == 0
    assert "https://saathi.example/#reset=" in capsys.readouterr().out
    assert run(argparse.Namespace(command="list"), settings) == 0
    assert "1 accounts" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run(argparse.Namespace(command="grant", email="nobody@example.com"), settings)


def test_two_servers_cannot_both_pass_the_order_limit(database_url: str) -> None:  # noqa: F811
    """The check and the record happen under one lock, even from separate connections."""
    database = connect(database_url)
    migrate(database)
    from app.accounts.store import AccountStore

    user = AccountStore(database).create_user("race@example.com", "x")
    guard = attempt_guard(CheckoutPolicy(), Decimal(1))
    barrier = threading.Barrier(8)
    won: list[int] = []
    refused: list[int] = []

    def attempt(number: int) -> None:
        journal = DatabaseOrderJournal(database, user.id)
        barrier.wait()
        try:
            journal.begin(new_attempt(f"hash{number}", "1", (), ()), guard)
            won.append(number)
        except AttemptRefused:
            refused.append(number)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    database.close()
    # A pending attempt blocks every later one, so exactly one may begin.
    assert len(won) == 1
    assert len(refused) == 7
