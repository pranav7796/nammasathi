"""Validation entry points. A failed widget never yields partial output."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.genui.models import MAX_WIDGETS, Widget

_ADAPTER: TypeAdapter[Widget] = TypeAdapter(Widget)


class GenUIError(ValueError):
    """A widget or envelope broke the contract in docs/genui-spec.md."""


def validate_widget(data: dict[str, Any]) -> Widget:
    if not isinstance(data, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise GenUIError("widget must be an object")
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise GenUIError(f"invalid widget at {where}: {first['msg']}") from exc


def validate_envelope(data: list[dict[str, Any]]) -> list[Widget]:
    if not isinstance(data, list):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise GenUIError("envelope must be a list")
    if len(data) > MAX_WIDGETS:
        raise GenUIError(f"more than {MAX_WIDGETS} widgets")
    widgets = [validate_widget(item) for item in data]
    ids = [w.id for w in widgets]
    if len(set(ids)) != len(ids):
        raise GenUIError("duplicate widget id in envelope")
    return widgets


def to_wire(widgets: Iterable[Widget]) -> list[dict[str, Any]]:
    """Plain JSON-safe dicts, None omitted. Re-validated so only contract-valid data leaves."""
    items = list(widgets)
    if len(items) > MAX_WIDGETS:
        raise GenUIError(f"more than {MAX_WIDGETS} widgets")
    out = [w.model_dump(mode="json", exclude_none=True) for w in items]
    validate_envelope(out)
    return out
