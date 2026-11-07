"""A small database layer: Postgres in production, SQLite on a developer's machine.

Only short, explicit transactions. The order journal depends on `lock_user`: it serialises the
"check limits, then record the attempt" step per user, across processes, on both databases.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol, cast

Row = dict[str, object]
Params = Sequence[object]

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            email_verified_at BIGINT,
            live_access INTEGER NOT NULL DEFAULT 0,
            consented_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL
        )""",
        """CREATE TABLE auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL
        )""",
        "CREATE INDEX auth_sessions_user ON auth_sessions(user_id)",
        """CREATE TABLE one_time_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL,
            expires_at BIGINT NOT NULL
        )""",
        """CREATE TABLE swiggy_tokens (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            issuer TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            expires_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            PRIMARY KEY (user_id, issuer)
        )""",
        """CREATE TABLE oauth_clients (
            issuer TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            client_id TEXT NOT NULL,
            PRIMARY KEY (issuer, redirect_uri)
        )""",
        """CREATE TABLE order_attempts (
            attempt_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            record TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX order_attempts_user ON order_attempts(user_id, created_at)",
    ),
    (
        """CREATE TABLE user_preferences (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            updated_at BIGINT NOT NULL
        )""",
        # Demo mode only: what was said and answered, so a person can pick up where they left off.
        """CREATE TABLE conversation_turns (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at BIGINT NOT NULL,
            language TEXT NOT NULL,
            capability TEXT NOT NULL,
            said TEXT NOT NULL,
            reply TEXT NOT NULL
        )""",
        "CREATE INDEX conversation_turns_user ON conversation_turns(user_id, created_at)",
        # Finished demo orders and bookings. Never real, and kept apart from order_attempts.
        """CREATE TABLE demo_orders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at BIGINT NOT NULL,
            capability TEXT NOT NULL,
            reference TEXT NOT NULL,
            summary TEXT NOT NULL,
            total_inr TEXT NOT NULL
        )""",
        "CREATE INDEX demo_orders_user ON demo_orders(user_id, created_at)",
    ),
    (
        # People who asked to be told when live ordering opens. No account needed. Email is stored
        # lower-cased and unique, so a repeat sign-up is recognised, not duplicated.
        """CREATE TABLE waitlist_entries (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            interests TEXT NOT NULL,
            language TEXT NOT NULL,
            consent_text TEXT NOT NULL,
            created_at BIGINT NOT NULL
        )""",
        # A person's own reminders and notes. due_at and done_at are epoch milliseconds.
        """CREATE TABLE reminders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            due_at BIGINT,
            done_at BIGINT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )""",
        "CREATE INDEX reminders_user ON reminders(user_id, due_at)",
        """CREATE TABLE notes (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )""",
        "CREATE INDEX notes_user ON notes(user_id, updated_at)",
    ),
)


class Transaction(Protocol):
    def execute(self, sql: str, params: Params = ()) -> list[Row]: ...

    def lock_user(self, user_id: str) -> None:
        """Hold other writers for this user until the transaction ends."""
        ...


class Database(Protocol):
    @property
    def dialect(self) -> str: ...

    def transaction(self) -> AbstractContextManager[Transaction]: ...

    def ping(self) -> None: ...

    def close(self) -> None: ...


class _SqliteTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Params = ()) -> list[Row]:
        cursor = self._connection.execute(sql, tuple(params))
        if cursor.description is None:
            return []
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def lock_user(self, user_id: str) -> None:
        del user_id  # BEGIN IMMEDIATE already holds the only write lock


class SqliteDatabase:
    """One connection per transaction, so it is safe from any thread."""

    dialect = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def transaction(self) -> Generator[Transaction]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteTransaction(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def ping(self) -> None:
        with self.transaction() as tx:
            tx.execute("SELECT 1")

    def close(self) -> None:
        return None


class _PostgresTransaction:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Params = ()) -> list[Row]:
        cursor = self._connection.execute(  # type: ignore[attr-defined]  # pyright: ignore
            sql.replace("?", "%s"), tuple(params)
        )
        if cursor.description is None:  # pyright: ignore
            return []
        return cast(list[Row], cursor.fetchall())  # pyright: ignore

    def lock_user(self, user_id: str) -> None:
        self.execute("SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,))


class PostgresDatabase:
    dialect = "postgres"

    def __init__(self, url: str) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # check= replaces connections a hosted database closed while it slept. Statements are
        # never prepared server-side, so a transaction pooler such as Neon's works too. Opening
        # does not wait: an unreachable database must not stop the server from starting.
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=False,
            timeout=8,
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 8},
            check=ConnectionPool.check_connection,
        )
        self._pool.open(wait=False)

    @contextmanager
    def transaction(self) -> Generator[Transaction]:
        with self._pool.connection() as connection, connection.transaction():
            yield _PostgresTransaction(connection)

    def ping(self) -> None:
        with self.transaction() as tx:
            tx.execute("SELECT 1")

    def close(self) -> None:
        self._pool.close(timeout=2)


def connect(url: str) -> Database:
    if url.startswith("sqlite:///"):
        return cast(Database, SqliteDatabase(Path(url.removeprefix("sqlite:///"))))
    if url.startswith(("postgresql://", "postgres://")):
        return cast(Database, PostgresDatabase(url))
    raise ValueError("DATABASE_URL must be postgresql://... or sqlite:///path")


SCHEMA_VERSION = len(_MIGRATIONS)


def migrate(database: Database) -> None:
    """Apply what is missing, once, even if two processes start together."""
    with database.transaction() as tx:
        if database.dialect == "postgres":
            tx.execute("SELECT pg_advisory_xact_lock(7283746501)")
        tx.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        rows = tx.execute("SELECT version FROM schema_version")
        current = cast(int, rows[0]["version"]) if rows else 0
        for statements in _MIGRATIONS[current:]:
            for statement in statements:
                tx.execute(statement)
        if not rows:
            tx.execute("INSERT INTO schema_version (version) VALUES (?)", (len(_MIGRATIONS),))
        elif current != len(_MIGRATIONS):
            tx.execute("UPDATE schema_version SET version = ?", (len(_MIGRATIONS),))
