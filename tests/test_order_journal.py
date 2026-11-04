from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from app.commerce.order_journal import (
    AttemptStatus,
    FileOrderJournal,
    JournalError,
    new_attempt,
    resolve_attempt,
    unresolved_attempts,
)
from app.order_admin import main as admin_main


def _attempt():
    return new_attempt("hash", "150", ("old-1",), ("Amul Milk",))


def _allow(_: object) -> None:
    return None


def test_attempt_survives_a_new_journal_instance(tmp_path: Path) -> None:
    path = tmp_path / "var" / "attempts.jsonl"
    attempt = _attempt()
    FileOrderJournal(path).begin(attempt, _allow)

    restored = FileOrderJournal(path).attempts()

    assert restored == [attempt]
    assert restored[0].status is AttemptStatus.PENDING
    assert unresolved_attempts(FileOrderJournal(path)) == [attempt]


def test_latest_record_of_an_attempt_wins(tmp_path: Path) -> None:
    journal = FileOrderJournal(tmp_path / "attempts.jsonl")
    attempt = _attempt()
    journal.begin(attempt, _allow)
    resolve_attempt(journal, attempt, AttemptStatus.PLACED, order_ids=("new-1",))

    [latest] = journal.attempts()

    assert latest.status is AttemptStatus.PLACED
    assert latest.order_ids == ("new-1",)
    assert unresolved_attempts(journal) == []


def test_guard_refusal_records_nothing(tmp_path: Path) -> None:
    journal = FileOrderJournal(tmp_path / "attempts.jsonl")

    def refuse(_: object) -> None:
        raise RuntimeError("blocked")

    with pytest.raises(RuntimeError):
        journal.begin(_attempt(), refuse)
    assert journal.attempts() == []


def test_corrupted_journal_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    path.write_text("{not json\n")
    with pytest.raises(JournalError):
        FileOrderJournal(path).attempts()


def test_unusable_location_fails_closed(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(JournalError):
        FileOrderJournal(blocker / "attempts.jsonl").attempts()


def test_journal_file_is_private_and_holds_only_allowed_fields(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    FileOrderJournal(path).begin(_attempt(), _allow)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert set(record) == {
        "attempt_id",
        "created_at",
        "status",
        "cart_hash",
        "total_inr",
        "baseline_order_ids",
        "item_names",
        "order_ids",
        "order_status",
        "note",
        "domain",
    }


def _run_admin(monkeypatch: pytest.MonkeyPatch, path: Path, *args: str) -> int:
    monkeypatch.setenv("ORDER_JOURNAL_PATH", str(path))
    monkeypatch.setattr("app.order_admin.load_dotenv", lambda: None)
    monkeypatch.setattr("sys.argv", ["order_admin", *args])
    with pytest.raises(SystemExit) as exit_info:
        admin_main()
    return int(exit_info.value.code or 0)


def test_admin_settles_an_attempt_only_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "attempts.jsonl"
    attempt = _attempt()
    FileOrderJournal(path).begin(attempt, _allow)

    assert _run_admin(monkeypatch, path, "status") == 0
    assert attempt.attempt_id in capsys.readouterr().out
    assert _run_admin(monkeypatch, path, "resolve", attempt.attempt_id, "placed") == 1
    assert unresolved_attempts(FileOrderJournal(path))

    assert (
        _run_admin(monkeypatch, path, "resolve", attempt.attempt_id, "placed", "--order-id", "O1")
        == 0
    )
    [settled] = FileOrderJournal(path).attempts()
    assert settled.status is AttemptStatus.PLACED
    assert settled.order_ids == ("O1",)
    assert _run_admin(monkeypatch, path, "resolve", "missing", "not-placed") == 1
