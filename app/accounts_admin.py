"""Operator commands for accounts. Live access is granted one person at a time.

    uv run python -m app.accounts_admin list
    uv run python -m app.accounts_admin verify someone@example.com
    uv run python -m app.accounts_admin grant someone@example.com
    uv run python -m app.accounts_admin revoke someone@example.com
    uv run python -m app.accounts_admin reset-link someone@example.com
    uv run python -m app.accounts_admin generate-key

`grant` is how a rollout ramps, as Swiggy's go-live checklist asks. It needs a verified email, so a
typo at sign-up can't connect a stranger. `reset-link` is for when the site has no email service.
"""

from __future__ import annotations

import argparse
import base64
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
