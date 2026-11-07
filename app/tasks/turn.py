"""One spoken or typed turn, as far as the task engine is concerned.

`handle` answers only requests that are clearly for the engine (email, calendar, WhatsApp, a day
plan, several tasks at once, a time change to an event waiting for approval, or approving a waiting
task by voice). Everything else returns None and goes to the ordering assistant as before.

Voice approval is two steps on purpose. Saying "send it" makes Saathi read the exact content back
and remember what it read. Only an explicit "confirm" after that sends exactly that content, and
only if it is still what was read out. A bare "yes" never starts it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, cast

from app.genui.models import Widget
from app.observability import log_event
from app.orchestrator.confirmation import explicit_confirmation
from app.tasks import render
from app.tasks.engine import Snapshot, TaskEngine, VoicePending
from app.tasks.planner import PlanExtractor, looks_like_a_plan, make_plan

VOICE_WINDOW_SECONDS = 120.0

_REFINE = re.compile(
    r"\b(make|move|change|shift|push)\b.{0,40}\b(meeting|event|it|call|appointment)\b|"
    r"\binstead\b",
    re.IGNORECASE,
)
_APPROVE_INTENT = re.compile(
    r"\b(send (it|the (email|mail|message))|approve|go ahead|"
    r"confirm (the )?(email|message|meeting|event))\b|"
    r"(भेज दो|भेज दीजिए|मंजूर)|(ಕಳುಹಿಸಿ|ಒಪ್ಪಿಗೆ)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TurnReply:
    message: str
    widgets: list[Widget] = field(default_factory=list[Widget])
    plan_id: str | None = None
    active: bool = False


_SIGN_IN = {
    "en-IN": "Please sign in first. Email, calendar and tasks are saved to your account.",
    "hi-IN": "पहले साइन इन करें। ईमेल, कैलेंडर और टास्क आपके खाते में सेव होते हैं।",
    "kn-IN": "ಮೊದಲು ಸೈನ್ ಇನ್ ಮಾಡಿ. ಇಮೇಲ್, ಕ್ಯಾಲೆಂಡರ್ ಮತ್ತು ಕೆಲಸಗಳು ನಿಮ್ಮ ಖಾತೆಯಲ್ಲಿ ಉಳಿಯುತ್ತವೆ.",
}
_STARTED = {
    "en-IN": "On it: {n} things. Nothing is sent or changed until you approve it.",
    "hi-IN": "शुरू कर दिया: {n} काम। आपके मंज़ूर करने तक कुछ भेजा या बदला नहीं जाएगा।",
    "kn-IN": "ಶುರು ಮಾಡಿದ್ದೇನೆ: {n} ಕೆಲಸಗಳು. ನೀವು ಒಪ್ಪುವವರೆಗೆ ಏನನ್ನೂ ಕಳುಹಿಸುವುದಿಲ್ಲ ಅಥವಾ ಬದಲಾಯಿಸುವುದಿಲ್ಲ.",
}
_STARTED_ONE = {
    "en-IN": "On it: 1 thing. Nothing is sent or changed until you approve it.",
    "hi-IN": "शुरू कर दिया: 1 काम। आपके मंज़ूर करने तक कुछ भेजा या बदला नहीं जाएगा।",
    "kn-IN": "ಶುರು ಮಾಡಿದ್ದೇನೆ: 1 ಕೆಲಸ. ನೀವು ಒಪ್ಪುವವರೆಗೆ ಏನನ್ನೂ ಕಳುಹಿಸುವುದಿಲ್ಲ ಅಥವಾ ಬದಲಾಯಿಸುವುದಿಲ್ಲ.",
}
_BUSY = {
    "en-IN": "That's a lot at once. Finish or cancel some of your open tasks first.",
    "hi-IN": "एक साथ बहुत हो गया। पहले कुछ खुले काम पूरे या रद्द करें।",
    "kn-IN": "ಒಮ್ಮೆಗೆ ತುಂಬಾ ಆಯಿತು. ಮೊದಲು ಕೆಲವು ಕೆಲಸಗಳನ್ನು ಮುಗಿಸಿ ಅಥವಾ ರದ್ದುಮಾಡಿ.",
}
_UPDATED = {
    "en-IN": "Updated. Check the new time, then approve.",
    "hi-IN": "अपडेट कर दिया। नया समय देखकर मंज़ूर करें।",
    "kn-IN": "ನವೀಕರಿಸಿದ್ದೇನೆ. ಹೊಸ ಸಮಯ ನೋಡಿ, ನಂತರ ಒಪ್ಪಿ.",
}
_READBACK = {
    "en-IN": "I'll do this: {what} Say confirm to go ahead, or tap the button.",
    "hi-IN": "मैं यह करूँगा: {what} आगे बढ़ने के लिए कन्फर्म कहें, या बटन दबाएँ।",
    "kn-IN": "ನಾನು ಇದನ್ನು ಮಾಡುತ್ತೇನೆ: {what} ಮುಂದುವರಿಯಲು ದೃಢಪಡಿಸಿ ಎನ್ನಿ, ಅಥವಾ ಬಟನ್ ಒತ್ತಿ.",
}
_DONE = {
    "en-IN": "Done. Check the board for what the service confirmed.",
    "hi-IN": "हो गया। सेवा ने क्या पुष्टि की, बोर्ड पर देखें।",
    "kn-IN": "ಆಯಿತು. ಸೇವೆ ಏನು ದೃಢಪಡಿಸಿತು ಎಂದು ಬೋರ್ಡ್‌ನಲ್ಲಿ ನೋಡಿ.",
}
_NOTHING = {
    "en-IN": "I have nothing waiting for your approval.",
    "hi-IN": "आपकी मंज़ूरी के लिए कुछ नहीं रुका है।",
    "kn-IN": "ನಿಮ್ಮ ಒಪ್ಪಿಗೆಗಾಗಿ ಏನೂ ಕಾಯುತ್ತಿಲ್ಲ.",
}


def _say(table: dict[str, str], language: str, **values: object) -> str:
    return table.get(language, table["en-IN"]).format(**values)


def _what(kind: str, view: dict[str, Any]) -> str:
    """The exact content, in words, for a spoken readback."""
    if view.get("channel") == "email":
        return f"email to {view.get('to')}, subject {view.get('subject')}: {view.get('body')}"
    if view.get("channel") == "message":
        return f"WhatsApp message to {view.get('to_label') or view.get('to')}: {view.get('body')}"
    fields = cast(dict[str, Any] | None, view.get("fields"))
    if isinstance(fields, dict):
        return (
            f"{fields.get('title')} on {fields.get('date')} at {fields.get('start')} "
            f"for {fields.get('minutes')} minutes."
        )
    if "text" in view:
        return f"save the reminder {view.get('text')}."
    return kind


async def _widgets(engine: TaskEngine, user_id: str, snapshot: Snapshot) -> list[Widget]:
    return await render.widgets_for(snapshot, await engine.messaging(user_id))


async def handle(
    engine: TaskEngine,
    user_id: str | None,
    language: str,
    text: str,
    extractor: PlanExtractor | None,
    *,
    order_busy: bool,
) -> TurnReply | None:
    """None means: not for the engine."""
    stripped = text.strip()
    if not stripped:
        return None

    if user_id is not None and not order_busy:
        voice = await _voice_approval(engine, user_id, language, stripped)
        if voice is not None:
            return voice

    if user_id is not None and _REFINE.search(stripped):
        task_id = await engine.refine_time(user_id, stripped)
        if task_id is not None:
            snapshot = await engine.latest(user_id)
            if snapshot is not None:
                return TurnReply(
                    _say(_UPDATED, language),
                    await _widgets(engine, user_id, snapshot),
                    snapshot.plan.id,
                    snapshot.active,
                )

    if not looks_like_a_plan(stripped):
        return None
    if user_id is None:
        return TurnReply(_say(_SIGN_IN, language))
    plan = await make_plan(stripped, language, engine.now(), extractor)
    if not plan.tasks:
        return TurnReply(plan.clarification) if plan.clarification else None
    outcome = await engine.submit(user_id, stripped, language, plan)
    if isinstance(outcome, str):
        return TurnReply(_say(_BUSY, language)) if outcome in ("busy", "too_many") else None
    log_event("tasks", op="turn", outcome="planned", count=len(outcome.tasks))
    return TurnReply(
        _say(_STARTED_ONE if len(outcome.tasks) == 1 else _STARTED, language, n=len(outcome.tasks)),
        await _widgets(engine, user_id, outcome),
        outcome.plan.id,
        True,
    )


async def _voice_approval(
    engine: TaskEngine, user_id: str, language: str, text: str
) -> TurnReply | None:
    pending = engine.voice_pending.get(user_id)
    if pending is not None and time.monotonic() > pending.expires:
        engine.voice_pending.pop(user_id, None)
        pending = None
    if pending is not None and explicit_confirmation(text) is True:
        engine.voice_pending.pop(user_id, None)
        started = await engine.approve(user_id, pending.task_id, shown_hash=pending.hash)
        if started != "started":
            return TurnReply(_say(_NOTHING, language))
        snapshot = await engine.latest(user_id)
        widgets = await _widgets(engine, user_id, snapshot) if snapshot else []
        return TurnReply(
            _say(_DONE, language), widgets, snapshot.plan.id if snapshot else None, False
        )
    if not _APPROVE_INTENT.search(text):
        return None
    snapshot = await engine.latest(user_id)
    waiting = [
        t for t in (snapshot.tasks if snapshot else []) if t.status == "waiting_for_approval"
    ]
    if len(waiting) != 1:
        return TurnReply(_say(_NOTHING, language)) if not waiting else None
    task = waiting[0]
    view = cast(dict[str, Any], task.result.get("view") or {})
    engine.voice_pending[user_id] = VoicePending(
        task.id, str(task.result.get("hash", "")), time.monotonic() + VOICE_WINDOW_SECONDS
    )
    assert snapshot is not None
    return TurnReply(
        _say(_READBACK, language, what=_what(task.kind, view)),
        await _widgets(engine, user_id, snapshot),
        snapshot.plan.id,
        snapshot.active,
    )
