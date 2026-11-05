"""Every read and write of account data. Every query that touches user data names the user."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import cast
from uuid import uuid4

from app.accounts.crypto import token_hash
from app.accounts.db import Database, Row

SESSION_LIFETIME_SECONDS = 14 * 24 * 60 * 60
HISTORY_KEPT_TURNS = 200
HISTORY_KEPT_SECONDS = 90 * 24 * 60 * 60
DEMO_ORDERS_KEPT = 100
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class EmailTaken(Exception):
    pass


class InvalidEmail(ValueError):
    pass


def normalize_email(email: str) -> str:
    cleaned = email.strip().casefold()
    if len(cleaned) > 254 or not _EMAIL.fullmatch(cleaned):
        raise InvalidEmail("Enter a valid email address.")
    return cleaned


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    email_verified: bool
    live_access: bool
    created_at: int


@dataclass(frozen=True)
class Turn:
    created_at: int  # milliseconds
    language: str
    capability: str
    said: str
    reply: str


@dataclass(frozen=True)
class DemoOrder:
    created_at: int  # milliseconds
    capability: str
    reference: str
    summary: str
    total_inr: str


@dataclass(frozen=True)
class StoredCiphertext:
    ciphertext: str
    expires_at: int


def _user(row: Row) -> User:
    return User(
        id=cast(str, row["id"]),
        email=cast(str, row["email"]),
        password_hash=cast(str, row["password_hash"]),
        email_verified=row["email_verified_at"] is not None,
        live_access=bool(row["live_access"]),
        created_at=cast(int, row["created_at"]),
    )


class AccountStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    # Users

    def create_user(self, email: str, password_hash: str) -> User:
        address = normalize_email(email)
        now = int(time.time())
        user_id = uuid4().hex
        with self.database.transaction() as tx:
            if tx.execute("SELECT id FROM users WHERE email = ?", (address,)):
                raise EmailTaken
            tx.execute(
                "INSERT INTO users (id, email, password_hash, consented_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, address, password_hash, now, now),
            )
            rows = tx.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user(rows[0])

    def user_by_id(self, user_id: str) -> User | None:
        with self.database.transaction() as tx:
            rows = tx.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user(rows[0]) if rows else None

    def user_by_email(self, email: str) -> User | None:
        try:
            address = normalize_email(email)
        except InvalidEmail:
            return None
        with self.database.transaction() as tx:
            rows = tx.execute("SELECT * FROM users WHERE email = ?", (address,))
        return _user(rows[0]) if rows else None

    def set_password(self, user_id: str, password_hash: str) -> None:
        """Changing a password signs out every session and voids outstanding reset links."""
        with self.database.transaction() as tx:
            tx.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
            tx.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            tx.execute("DELETE FROM one_time_tokens WHERE user_id = ?", (user_id,))

    def mark_verified(self, user_id: str) -> None:
        with self.database.transaction() as tx:
            tx.execute(
                "UPDATE users SET email_verified_at = ? WHERE id = ?",
                (int(time.time()), user_id),
            )

    def set_live_access(self, user_id: str, allowed: bool) -> None:
        with self.database.transaction() as tx:
            tx.execute("UPDATE users SET live_access = ? WHERE id = ?", (int(allowed), user_id))

    def delete_user(self, user_id: str) -> None:
        """Foreign keys remove this user's sessions, tokens, and order records with it."""
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # Signed-in browsers

    def create_session(self, user_id: str) -> str:
        token = token_urlsafe(32)
        now = int(time.time())
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            tx.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash(token), user_id, now, now + SESSION_LIFETIME_SECONDS),
            )
        return token

    def session_user(self, token: str) -> User | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ?",
                (token_hash(token), int(time.time())),
            )
        return _user(rows[0]) if rows else None

    def end_session(self, token: str) -> None:
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash(token),))

    def end_all_sessions(self, user_id: str) -> None:
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    # One-time links: email verification and password reset

    def issue_one_time(self, user_id: str, purpose: str, lifetime_seconds: int) -> str:
        token = token_urlsafe(32)
        now = int(time.time())
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM one_time_tokens WHERE expires_at <= ?", (now,))
            tx.execute(
                "DELETE FROM one_time_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose)
            )
            tx.execute(
                "INSERT INTO one_time_tokens (token_hash, user_id, purpose, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash(token), user_id, purpose, now + lifetime_seconds),
            )
        return token

    def consume_one_time(self, token: str, purpose: str) -> str | None:
        """The user this link belongs to, once. A second use finds nothing."""
        digest = token_hash(token)
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT user_id FROM one_time_tokens "
                "WHERE token_hash = ? AND purpose = ? AND expires_at > ?",
                (digest, purpose, int(time.time())),
            )
            tx.execute("DELETE FROM one_time_tokens WHERE token_hash = ?", (digest,))
        return cast(str, rows[0]["user_id"]) if rows else None

    # Swiggy tokens: ciphertext only. See app/accounts/swiggy_tokens.py.

    def get_token(self, user_id: str, issuer: str) -> StoredCiphertext | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT ciphertext, expires_at FROM swiggy_tokens WHERE user_id = ? AND issuer = ?",
                (user_id, issuer),
            )
        if not rows:
            return None
        return StoredCiphertext(cast(str, rows[0]["ciphertext"]), cast(int, rows[0]["expires_at"]))

    def put_token(self, user_id: str, issuer: str, ciphertext: str, expires_at: int) -> None:
        with self.database.transaction() as tx:
            tx.execute(
                "DELETE FROM swiggy_tokens WHERE user_id = ? AND issuer = ?", (user_id, issuer)
            )
            tx.execute(
                "INSERT INTO swiggy_tokens (user_id, issuer, ciphertext, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, issuer, ciphertext, expires_at, int(time.time())),
            )

    def delete_token(self, user_id: str, issuer: str) -> None:
        with self.database.transaction() as tx:
            tx.execute(
                "DELETE FROM swiggy_tokens WHERE user_id = ? AND issuer = ?", (user_id, issuer)
            )

    # Preferences, history, and demo orders: one person's own, demo mode only

    def set_language(self, user_id: str, language: str) -> None:
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
            tx.execute(
                "INSERT INTO user_preferences (user_id, language, updated_at) VALUES (?, ?, ?)",
                (user_id, language, int(time.time())),
            )

    def language(self, user_id: str) -> str | None:
        with self.database.transaction() as tx:
            rows = tx.execute("SELECT language FROM user_preferences WHERE user_id = ?", (user_id,))
        return cast(str, rows[0]["language"]) if rows else None

    def add_turn(self, user_id: str, turn: Turn) -> None:
        """Keeps the newest turns only, and nothing older than the retention period."""
        with self.database.transaction() as tx:
            tx.execute(
                "INSERT INTO conversation_turns "
                "(id, user_id, created_at, language, capability, said, reply) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    user_id,
                    turn.created_at,
                    turn.language,
                    turn.capability,
                    turn.said,
                    turn.reply,
                ),
            )
            cutoff = turn.created_at - HISTORY_KEPT_SECONDS * 1000
            tx.execute(
                "DELETE FROM conversation_turns WHERE user_id = ? AND created_at < ?",
                (user_id, cutoff),
            )
            tx.execute(
                "DELETE FROM conversation_turns WHERE user_id = ? AND id NOT IN ("
                "SELECT id FROM conversation_turns WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?)",
                (user_id, user_id, HISTORY_KEPT_TURNS),
            )

    def turns(self, user_id: str, limit: int = 50) -> list[Turn]:
        """Newest last."""
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT created_at, language, capability, said, reply FROM conversation_turns "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        return [
            Turn(
                cast(int, row["created_at"]),
                cast(str, row["language"]),
                cast(str, row["capability"]),
                cast(str, row["said"]),
                cast(str, row["reply"]),
            )
            for row in reversed(rows)
        ]

    def clear_turns(self, user_id: str) -> None:
        with self.database.transaction() as tx:
            tx.execute("DELETE FROM conversation_turns WHERE user_id = ?", (user_id,))

    def add_demo_order(self, user_id: str, order: DemoOrder) -> None:
        with self.database.transaction() as tx:
            tx.execute(
                "INSERT INTO demo_orders "
                "(id, user_id, created_at, capability, reference, summary, total_inr) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    user_id,
                    order.created_at,
                    order.capability,
                    order.reference,
                    order.summary,
                    order.total_inr,
                ),
            )
            tx.execute(
                "DELETE FROM demo_orders WHERE user_id = ? AND id NOT IN ("
                "SELECT id FROM demo_orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?)",
                (user_id, user_id, DEMO_ORDERS_KEPT),
            )

    def demo_orders(self, user_id: str, limit: int = 50) -> list[DemoOrder]:
        """Newest first."""
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT created_at, capability, reference, summary, total_inr FROM demo_orders "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        return [
            DemoOrder(
                cast(int, row["created_at"]),
                cast(str, row["capability"]),
                cast(str, row["reference"]),
                cast(str, row["summary"]),
                cast(str, row["total_inr"]),
            )
            for row in rows
        ]

    # Dynamic Client Registration, kept so a restart does not register a new client

    def oauth_client(self, issuer: str, redirect_uri: str) -> str | None:
        with self.database.transaction() as tx:
            rows = tx.execute(
                "SELECT client_id FROM oauth_clients WHERE issuer = ? AND redirect_uri = ?",
                (issuer, redirect_uri),
            )
        return cast(str, rows[0]["client_id"]) if rows else None

    def save_oauth_client(self, issuer: str, redirect_uri: str, client_id: str) -> None:
        with self.database.transaction() as tx:
            tx.execute(
                "DELETE FROM oauth_clients WHERE issuer = ? AND redirect_uri = ?",
                (issuer, redirect_uri),
            )
            tx.execute(
                "INSERT INTO oauth_clients (issuer, redirect_uri, client_id) VALUES (?, ?, ?)",
                (issuer, redirect_uri, client_id),
            )

    def forget_oauth_client(self, issuer: str, redirect_uri: str) -> None:
        with self.database.transaction() as tx:
            tx.execute(
                "DELETE FROM oauth_clients WHERE issuer = ? AND redirect_uri = ?",
                (issuer, redirect_uri),
            )
