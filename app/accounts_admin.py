"""Operator commands for accounts. Live access is granted one person at a time.

    uv run python -m app.accounts_admin list
    uv run python -m app.accounts_admin verify someone@example.com
    uv run python -m app.accounts_admin grant someone@example.com
    uv run python -m app.accounts_admin revoke someone@example.com
    uv run python -m app.accounts_admin reset-link someone@example.com
    uv run python -m app.accounts_admin generate-key
    uv run python -m app.accounts_admin waitlist [--csv]
    uv run python -m app.accounts_admin waitlist-remove someone@example.com

`grant` is how a rollout ramps, as Swiggy's go-live checklist asks. It needs a verified email, so a
typo at sign-up can't connect a stranger. `reset-link` is for when the site has no email service.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv

from app.accounts.db import connect, migrate
from app.accounts.routes import RESET_LINK_SECONDS
from app.accounts.store import AccountStore, User
from app.config import Settings, load_settings


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _store(settings: Settings) -> AccountStore:
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set.")
    database = connect(settings.database_url)
    migrate(database)
    return AccountStore(database)


def _find(store: AccountStore, email: str) -> User:
    user = store.user_by_email(email)
    if user is None:
        raise SystemExit("No account with that email.")
    return user


def waitlist(store: AccountStore, as_csv: bool) -> None:
    with store.database.transaction() as tx:
        rows = tx.execute(
            "SELECT email, interests, language, created_at FROM waitlist_entries "
            "ORDER BY created_at"
        )
    table: list[list[str]] = []
    for row in rows:
        when = datetime.fromtimestamp(int(str(row["created_at"])) / 1000, UTC).date()
        interests = ",".join(str(i) for i in json.loads(str(row["interests"])))
        table.append([str(row["email"]), interests, str(row["language"]), str(when)])
    if as_csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["email", "interests", "language", "date"])
        writer.writerows(table)
        return
    print(f"{len(table)} on the waitlist")
    for email, interests, language, when in table:
        print(f"{email}  interests={interests or '-'}  language={language}  joined={when}")


def waitlist_remove(store: AccountStore, email: str) -> int:
    with store.database.transaction() as tx:
        found = tx.execute(
            "SELECT 1 AS found FROM waitlist_entries WHERE email = ?", (email.strip().lower(),)
        )
        tx.execute("DELETE FROM waitlist_entries WHERE email = ?", (email.strip().lower(),))
    if not found:
        print("That email is not on the waitlist.")
        return 1
    print("Removed from the waitlist.")
    return 0


def run(args: argparse.Namespace, settings: Settings) -> int:
    store = _store(settings)
    try:
        if args.command == "list":
            with store.database.transaction() as tx:
                rows = tx.execute(
                    "SELECT email, email_verified_at, live_access, created_at FROM users "
                    "ORDER BY created_at"
                )
            for row in rows:
                created = datetime.fromtimestamp(int(str(row["created_at"])), UTC).date()
                print(
                    f"{row['email']}  verified={row['email_verified_at'] is not None}  "
                    f"live={bool(row['live_access'])}  joined={created}"
                )
            print(f"{len(rows)} accounts")
            return 0
        if args.command == "waitlist":
            waitlist(store, args.csv)
            return 0
        if args.command == "waitlist-remove":
            return waitlist_remove(store, args.email)
        user = _find(store, args.email)
        if args.command == "verify":
            store.mark_verified(user.id)
            print("Email marked verified. Check you really know this is their address.")
        elif args.command == "grant":
            if not user.email_verified:
                print("Refused: verify this person's email first.")
                return 1
            store.set_live_access(user.id, True)
            print("Live access granted. They can now connect their Swiggy account.")
        elif args.command == "revoke":
            store.set_live_access(user.id, False)
            print("Live access removed. A running conversation ends on its next turn.")
        elif args.command == "reset-link":
            token = store.issue_one_time(user.id, "reset", RESET_LINK_SECONDS)
            print(f"{settings.public_base_url.rstrip('/')}/#reset={token}")
            print("Works once, for one hour. Give it only to this person, privately.")
        return 0
    finally:
        store.database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("generate-key")
    commands.add_parser("waitlist").add_argument("--csv", action="store_true")
    commands.add_parser("waitlist-remove").add_argument("email")
    for name in ("verify", "grant", "revoke", "reset-link"):
        commands.add_parser(name).add_argument("email")
    args = parser.parse_args()
    if args.command == "generate-key":
        print(generate_key())
        return
    load_dotenv()
    sys.exit(run(args, load_settings()))


if __name__ == "__main__":
    main()
