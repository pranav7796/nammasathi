"""Registry of action ids a widget may carry. An action id is only a request: the server re-checks
authorisation, ownership and state before doing anything (docs/genui-spec.md)."""

from __future__ import annotations

import re

ACTION_IDS: frozenset[str] = frozenset(
    {
        "order.confirm",
        "order.reject",
        "cart.set_quantity",
        "reminder.create",
        "reminder.done",
        "reminder.undo",
        "reminder.delete",
        "note.create",
        "note.delete",
        "task.retry",
        "task.cancel",
        "waitlist.join",
        "task.approve",
        "task.edit",
        "task.view",
        "email.open",
        "email.reply",
        "plan.toggle",
        "plan.priority",
        "plan.move",
        "plan.add",
        "memory.forget",
        "memory.save",
        "demo.next",
    }
)

_SUFFIX = re.compile(r"^[a-z0-9_.-]{1,40}$")
# Mail ids come from the mail service as-is (docs/genui-spec.md addendum): mixed case, up to 100.
_EMAIL_SUFFIX = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SUFFIX_BY_BASE: dict[str, re.Pattern[str]] = {
    "email.open": _EMAIL_SUFFIX,
    "email.reply": _EMAIL_SUFFIX,
}


def is_known_action(action_id: str) -> bool:
    """True for "<known id>" or "<known id>:<suffix>" (suffix targets one item)."""
    base, sep, suffix = action_id.partition(":")
    if base not in ACTION_IDS:
        return False
    return not sep or bool(_SUFFIX_BY_BASE.get(base, _SUFFIX).fullmatch(suffix))


def targeted(action_id: str, target: str) -> str:
    """Build "<known id>:<target>"; raises ValueError if either part is not allowed."""
    result = f"{action_id}:{target}"
    if not is_known_action(result) or not target:
        raise ValueError("unknown action or invalid target")
    return result
