"""Inspect and settle checkout attempts recorded in the order journal.

An unresolved attempt blocks all further checkout. Only a person who has checked
the Swiggy app can say what really happened, so settling one is a deliberate act:

    uv run python -m app.order_admin status
    uv run python -m app.order_admin resolve <attempt_id> placed --order-id <id>
    uv run python -m app.order_admin resolve <attempt_id> not-placed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.commerce.order_journal import (
    AttemptStatus,
    FileOrderJournal,
    resolve_attempt,
)
from app.config import load_settings


def _journal() -> FileOrderJournal:
    load_dotenv()
    return FileOrderJournal(Path(load_settings().order_journal_path))


def status() -> int:
    attempts = _journal().attempts()
    if not attempts:
        print("No checkout attempts recorded.")
        return 0
    for attempt in attempts:
        print(
            f"{attempt.attempt_id}  {attempt.status.value:<10}  ₹{attempt.total_inr:<8}  "
            f"{attempt.created_at}  items={len(attempt.item_names)}  "
            f"orders={','.join(attempt.order_ids) or '-'}  {attempt.note}"
        )
    return 0


def resolve(attempt_id: str, outcome: str, order_id: str | None) -> int:
    journal = _journal()
    attempt = next((item for item in journal.attempts() if item.attempt_id == attempt_id), None)
    if attempt is None:
        print("No such attempt.")
        return 1
    if outcome == "placed" and not order_id:
        print("Give the order ID shown in the Swiggy app with --order-id.")
        return 1
    resolve_attempt(
        journal,
        attempt,
        AttemptStatus.PLACED if outcome == "placed" else AttemptStatus.NOT_PLACED,
        order_ids=(order_id,) if order_id else (),
        note="settled manually after checking the Swiggy app",
    )
    print(f"Attempt {attempt_id} recorded as {outcome}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Ajji Mart order attempt journal")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    resolver = commands.add_parser("resolve")
    resolver.add_argument("attempt_id")
    resolver.add_argument("outcome", choices=("placed", "not-placed"))
    resolver.add_argument("--order-id")
    args = parser.parse_args()
    if args.command == "status":
        sys.exit(status())
    sys.exit(resolve(args.attempt_id, args.outcome, args.order_id))


if __name__ == "__main__":
    main()
