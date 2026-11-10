"""The public waitlist: validation, duplicates, limits, admin commands, and no email in logs."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.accounts.service import UNAVAILABLE_MESSAGE, AccountService
from app.accounts_admin import run
from app.config import Settings
from app.waitlist import CONSENT_TEXT, PER_ADDRESS_PER_HOUR, waitlist_router
from tests.test_accounts import Site
from tests.test_accounts import site as account_site

pytestmark = pytest.mark.asyncio

JOIN = {"email": "asha@example.com", "interests": ["food"], "language": "en-IN", "consent": True}


@asynccontextmanager
async def site(tmp_path: Path) -> AsyncGenerator[Site]:
    async with account_site(tmp_path) as s:
        s.app.include_router(waitlist_router(s.service))  # no-op if web.py already mounts it
        yield s


def _rows(service: AccountService) -> list[dict[str, object]]:
    with service.database.transaction() as tx:
        return tx.execute("SELECT * FROM waitlist_entries ORDER BY created_at")


async def test_valid_join_stores_the_entry_and_consent_text(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        body = {**JOIN, "interests": ["food", "food", "groceries"], "language": "kn-IN"}
        response = await browser.post("/api/waitlist", json=body)
        assert response.status_code == 200
        assert response.json() == {"status": "joined"}
        (row,) = _rows(s.service)
        assert row["email"] == "asha@example.com"
        assert json.loads(str(row["interests"])) == ["food", "groceries"]
        assert row["language"] == "kn-IN"
        assert row["consent_text"] == CONSENT_TEXT


@pytest.mark.parametrize(
    "email",
    ["", "nope", "a@b", "a b@example.com", "a@exa mple.com", "a@@example.com", "x" * 250 + "@a.co"],
)
async def test_bad_emails_are_refused(tmp_path: Path, email: str) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        response = await browser.post("/api/waitlist", json={**JOIN, "email": email})
        assert response.status_code in (400, 422)
        assert _rows(s.service) == []


async def test_bad_interest_or_language_is_refused(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        bad = await browser.post("/api/waitlist", json={**JOIN, "interests": ["spam"]})
        assert bad.status_code == 422
        bad = await browser.post("/api/waitlist", json={**JOIN, "language": "fr-FR"})
        assert bad.status_code == 422


async def test_consent_is_required(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        for body in ({**JOIN, "consent": False}, {k: v for k, v in JOIN.items() if k != "consent"}):
            assert (await browser.post("/api/waitlist", json=body)).status_code == 400
        assert _rows(s.service) == []


async def test_duplicates_and_case_are_one_row(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        first = await browser.post("/api/waitlist", json={**JOIN, "email": " Foo@X.com "})
        again = await browser.post("/api/waitlist", json={**JOIN, "email": "foo@x.COM"})
        assert first.json() == {"status": "joined"}
        assert again.status_code == 200
        assert again.json() == {"status": "already_listed"}
        assert [r["email"] for r in _rows(s.service)] == ["foo@x.com"]


async def test_rate_limit_per_address(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        codes = [
            (
                await browser.post("/api/waitlist", json={**JOIN, "email": f"p{n}@example.com"})
            ).status_code
            for n in range(PER_ADDRESS_PER_HOUR + 1)
        ]
        assert codes == [200] * PER_ADDRESS_PER_HOUR + [429]
        assert len(_rows(s.service)) == PER_ADDRESS_PER_HOUR


async def test_unavailable_database_is_a_503(tmp_path: Path) -> None:
    async with site(tmp_path) as s, s.browser() as browser:

        def broken(*_args: object) -> None:
            raise RuntimeError("down")

        s.service.database.transaction = broken  # type: ignore[method-assign,assignment]
        response = await browser.post("/api/waitlist", json=JOIN)
        assert response.status_code == 503
        assert response.json()["detail"] == UNAVAILABLE_MESSAGE


async def test_no_email_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with site(tmp_path) as s, s.browser() as browser:
        await browser.post("/api/waitlist", json={**JOIN, "email": "secret.person@example.com"})
        await browser.post("/api/waitlist", json={**JOIN, "email": "secret.person@example.com"})
    assert "waitlist" in caplog.text
    assert "secret.person" not in caplog.text


async def test_admin_list_csv_and_remove(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with site(tmp_path) as s, s.browser() as browser:
        await browser.post("/api/waitlist", json=JOIN)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'saathi.db'}")

    def command(name: str, **extra: object) -> int:
        return run(argparse.Namespace(command=name, **extra), settings)

    assert command("waitlist", csv=False) == 0
    out = capsys.readouterr().out
    assert "1 on the waitlist" in out and "asha@example.com" in out and "food" in out
    assert command("waitlist", csv=True) == 0
    assert capsys.readouterr().out.splitlines()[0] == "email,interests,language,date"
    assert command("waitlist-remove", email="ASHA@example.com") == 0
    assert command("waitlist-remove", email="asha@example.com") == 1
    capsys.readouterr()
    command("waitlist", csv=False)
    assert "0 on the waitlist" in capsys.readouterr().out


async def test_a_filled_hidden_field_stores_nothing(tmp_path: Path) -> None:
    """A script that fills every field is told it worked, and nothing is kept."""
    async with site(tmp_path) as s, s.browser() as browser:
        response = await browser.post("/api/waitlist", json={**JOIN, "website": "http://spam.test"})
        assert response.status_code == 200
        assert response.json() == {"status": "joined"}
        assert _rows(s.service) == []
        real = await browser.post("/api/waitlist", json={**JOIN, "website": ""})
        assert real.json() == {"status": "joined"}
        assert len(_rows(s.service)) == 1


async def test_the_page_carries_a_link_preview_and_a_way_to_the_form(tmp_path: Path) -> None:
    """LinkedIn reads these tags without running scripts, and needs absolute addresses."""
    async with site(tmp_path) as s, s.browser() as browser:
        response = await browser.get("/")
        assert response.status_code == 200
        page = response.text
        assert "{{base_url}}" not in page
        base = str(browser.base_url).rstrip("/")
        assert f'<meta property="og:image" content="{base}/static/og.png">' in page
        assert f'<meta property="og:url" content="{base}/">' in page
        assert '<meta property="og:title" content="Namma Saathi' in page
        assert '<meta name="description" content="' in page
        assert page.count("<h1") == 1
        assert 'href="#waitlist"' in page and 'id="waitlist-mount"' in page
        image = await browser.get("/static/og.png")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_the_form_script_only_uses_text_content() -> None:
    script = Path("app/static/waitlist.js").read_text()
    assert "innerHTML" not in script
    assert CONSENT_TEXT in script
