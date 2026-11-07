"""Contacts: validation, caps, uniqueness, resolution, isolation, routes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from app.accounts.db import SqliteDatabase, migrate
from app.accounts.service import AccountService
from app.accounts.store import AccountStore
from app.memory.service import MemoryService, match_contact
from app.memory.store import (
    MAX_CONTACTS,
    DuplicateName,
    InvalidValue,
    LimitReached,
    MemoryStore,
    NotFound,
)
from tests.test_accounts import register
from tests.test_memory import route_site, service_site

type Fix = tuple[SqliteDatabase, AccountStore, str, str]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Fix]:
    database = SqliteDatabase(tmp_path / "c.db")
    migrate(database)
    accounts = AccountStore(database)
    yield (
        database,
        accounts,
        accounts.create_user("a@example.com", "x").id,
        accounts.create_user("b@example.com", "x").id,
    )


def test_create_validates(db: Fix) -> None:
    database, _, a, _ = db
    store = MemoryStore(database)
    c = store.create_contact(a, " Amma ", email=" Amma@Example.COM ", phone="+91 98765-43210")
    assert (c.name, c.email, c.phone) == ("Amma", "amma@example.com", "+919876543210")
    assert len(c.id) == 32
    assert store.create_contact(a, "Only phone", phone="+4412345678").email is None
    assert store.create_contact(a, "Only email", email="x@y.in").phone is None
    for kwargs in (
        {},
        {"email": "  "},
        {"email": "not-an-email"},
        {"email": "a@b"},
        {"phone": "9876543210"},
        {"phone": "+1234567"},
        {"phone": "+1234567890123456"},
        {"phone": "+91abc4567890"},
        {"phone": "+0123456789"},
    ):
        with pytest.raises(InvalidValue):
            store.create_contact(a, "Bad", **kwargs)  # type: ignore[arg-type]
    store.create_contact(a, "Edge", phone="+12345678")
    store.create_contact(a, "Edge2", phone="+123456789012345")
    for name in ("", "   ", "x" * 81, "bad\nname"):
        with pytest.raises(InvalidValue):
            store.create_contact(a, name, phone="+919876543210")
    store.create_contact(a, "n" * 80, phone="+919876543210")


def test_duplicate_names_and_cap(db: Fix) -> None:
    database, _, a, b = db
    store = MemoryStore(database)
    store.create_contact(a, "Ravi", phone="+919876543210")
    with pytest.raises(DuplicateName):
        store.create_contact(a, "rAVI", phone="+919876543211")
    store.create_contact(b, "Ravi", phone="+919876543210")  # other user is fine
    for i in range(MAX_CONTACTS - 1):
        store.create_contact(a, f"c{i}", phone="+919876543210")
    with pytest.raises(LimitReached):
        store.create_contact(a, "one too many", phone="+919876543210")
    assert len(store.list_contacts(a)) == MAX_CONTACTS


def test_update_delete_and_isolation(db: Fix) -> None:
    database, _, a, b = db
    store = MemoryStore(database)
    ravi = store.create_contact(a, "Ravi", phone="+919876543210", email="r@x.in")
    sita = store.create_contact(a, "Sita", phone="+919876543211")
    other = store.create_contact(b, "Other", phone="+919876543212")

    edited = store.update_contact(a, ravi.id, name="Ravi K", change_email=True, email=None)
    assert (edited.name, edited.email, edited.phone) == ("Ravi K", None, "+919876543210")
    assert store.update_contact(a, ravi.id, phone="+44 1234 5678", change_phone=True).phone == (
        "+4412345678"
    )
    with pytest.raises(DuplicateName):
        store.update_contact(a, sita.id, name="ravi k")
    store.update_contact(a, sita.id, name="SITA")  # renaming to itself in other case is fine
    with pytest.raises(InvalidValue):
        store.update_contact(a, sita.id, change_phone=True, phone=None)
    with pytest.raises(InvalidValue):
        store.update_contact(a, sita.id, phone="bad", change_phone=True)

    for call in (
        lambda: store.get_contact(a, other.id),
        lambda: store.update_contact(a, other.id, name="Hacked"),
        lambda: store.delete_contact(a, other.id),
        lambda: store.get_contact(b, ravi.id),
        lambda: store.delete_contact(a, "missing"),
    ):
        with pytest.raises(NotFound):
            call()
    assert store.get_contact(b, other.id).name == "Other"
    assert [c.name for c in store.list_contacts(b)] == ["Other"]
    store.delete_contact(a, ravi.id)
    assert [c.name for c in store.list_contacts(a)] == ["SITA"]


def test_persists_and_cascades(tmp_path: Path, db: Fix) -> None:
    database, accounts, a, b = db
    MemoryStore(database).create_contact(a, "Ravi", phone="+919876543210")
    MemoryStore(database).create_contact(b, "Sita", phone="+919876543210")
    reopened = MemoryStore(SqliteDatabase(tmp_path / "c.db"))
    assert [c.name for c in reopened.list_contacts(a)] == ["Ravi"]
    accounts.delete_user(a)
    assert reopened.list_contacts(a) == []
    assert [c.name for c in reopened.list_contacts(b)] == ["Sita"]
    with database.transaction() as tx:
        assert len(tx.execute("SELECT id FROM contacts")) == 1


def test_resolution(db: Fix) -> None:
    database, _, a, _ = db
    store = MemoryStore(database)
    for name in ("Amma", "Ravi Kumar", "Ravindra Rao", "Sita Devi", "Sit"):
        store.create_contact(a, name, phone="+919876543210")
    contacts = store.list_contacts(a)

    def m(text: str) -> tuple[str, str | None]:
        r = match_contact(contacts, text)
        return r.kind, None if r.contact is None else r.contact.name

    assert m("amma") == ("one", "Amma")
    assert m("my Amma") == ("one", "Amma")
    assert m("  RAVI   kumar ") == ("one", "Ravi Kumar")
    assert m("Sit") == ("one", "Sit")  # exact beats the prefix of "Sita Devi"
    assert m("sita") == ("one", "Sita Devi")  # unique first name
    assert m("sita d") == ("one", "Sita Devi")
    assert m("ravi")[0] == "ambiguous"
    assert m("ravi k") == ("one", "Ravi Kumar")
    assert m("rav")[0] == "ambiguous"
    assert (
        match_contact(contacts, "rav").candidates
        == (
            contacts[1],
            contacts[2],
        )
        or len(match_contact(contacts, "rav").candidates) == 2
    )
    assert match_contact(contacts, "rav").contact is None
    assert m("zed") == ("none", None)
    assert m("") == ("none", None)
    assert m("s")[0] == "none"  # too short to guess
    assert m("ravi kumr") == ("none", None)  # no fuzzy matching
    # Relationship words match only a contact saved under that word.
    assert m("my mother") == ("none", None)
    assert m("mom") == ("none", None)
    store.create_contact(a, "Mom", phone="+919876543211")
    store.create_contact(a, "Mommy Dearest", phone="+919876543212")
    contacts = store.list_contacts(a)
    assert m("mom") == ("one", "Mom")
    assert m("my mom") == ("one", "Mom")
    assert m("mother") == ("none", None)


async def test_service_resolve_is_per_user(tmp_path: Path) -> None:
    async with service_site(tmp_path) as (app, service):
        accounts = cast(AccountService, app.state.accounts)
        a = await accounts.run(accounts.store.create_user, "a@example.com", "x")
        b = await accounts.run(accounts.store.create_user, "b@example.com", "x")
        await service.create_contact(a.id, "Amma", phone="+919876543210")
        assert (await service.resolve_contact(a.id, "amma")).kind == "one"
        assert (await service.resolve_contact(b.id, "amma")).kind == "none"
        match = await service.resolve_contact(a.id, "amma")
        assert match.contact is not None and match.contact.phone == "+919876543210"
        assert isinstance(service, MemoryService)


async def test_contact_routes(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as one, s.browser() as two:
        await register(one)
        await register(two, "other@example.com")
        assert (await one.get("/api/contacts")).json() == {"contacts": []}
        made = await one.post(
            "/api/contacts", json={"name": "Amma", "phone": "+91 98765 43210", "email": "A@B.in"}
        )
        assert made.status_code == 200
        c = made.json()
        assert (c["phone"], c["email"]) == ("+919876543210", "a@b.in")

        assert (
            await one.post("/api/contacts", json={"name": "amma", "email": "q@r.in"})
        ).status_code == 409
        assert (await one.post("/api/contacts", json={"name": "Nobody"})).status_code == 422
        assert (
            await one.post("/api/contacts", json={"name": "N", "phone": "12"})
        ).status_code == 422
        bad = await one.post("/api/contacts", json={"name": "N", "phone": "+9199", "user_id": "u"})
        assert bad.status_code == 422

        # Another person's contact is 404, never 403.
        assert (await two.get("/api/contacts")).json() == {"contacts": []}
        assert (await two.patch(f"/api/contacts/{c['id']}", json={"name": "X"})).status_code == 404
        assert (await two.delete(f"/api/contacts/{c['id']}")).status_code == 404

        patched = await one.patch(f"/api/contacts/{c['id']}", json={"email": None})
        assert patched.status_code == 200 and patched.json()["email"] is None
        name_only = await one.patch(f"/api/contacts/{c['id']}", json={"name": "Amma J"})
        assert name_only.json()["phone"] == "+919876543210"
        both_gone = await one.patch(f"/api/contacts/{c['id']}", json={"phone": None})
        assert both_gone.status_code == 422
        assert (await one.delete(f"/api/contacts/{c['id']}")).status_code == 200
        assert (await one.delete(f"/api/contacts/{c['id']}")).status_code == 404


async def test_contact_routes_signed_out(tmp_path: Path) -> None:
    async with route_site(tmp_path) as s, s.browser() as anon:
        assert (await anon.get("/api/contacts")).status_code == 401
        assert (await anon.post("/api/contacts", json={"name": "A"})).status_code == 401
        assert (await anon.patch("/api/contacts/x", json={"name": "A"})).status_code == 401
        assert (await anon.delete("/api/contacts/x")).status_code == 401
