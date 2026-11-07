"""A question Saathi asked and is waiting to have answered.

Without this, "Who should I message?" was just a string: nothing remembered that a WhatsApp message
was in progress, so the answer ("Rahul") went to the grocery assistant. A `Pending` records which
task will be made, which detail is missing, and what is already known, so the next turn is routed
back to the workflow that asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pending:
    kind: str  # the task kind this becomes once complete, e.g. "whatsapp.send"
    slot: str  # the missing detail: "to", "body", "intent", "when", "text"
    args: dict[str, object] = field(default_factory=dict[str, object])  # what is already known
    question: str = ""  # English; the web layer speaks it in the person's language

    @property
    def workflow(self) -> str:
        """Which workflow owns this question (the page shows that workflow's panel)."""
        return workflow_of(self.kind)


def workflow_of(kind: str) -> str:
    if kind.startswith("whatsapp."):
        return "whatsapp"
    if kind.startswith("gmail."):
        return "email"
    if kind.startswith("calendar."):
        return "calendar"
    if kind == "day.plan":
        return "day"
    if kind.startswith("reminder."):
        return "reminders"
    return "tasks"
