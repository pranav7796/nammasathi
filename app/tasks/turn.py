"""One spoken or typed turn, as far as the task engine is concerned.

`handle` answers only what is clearly for the engine: an answer to a question Saathi just asked
(a missing recipient, message, or time), email, calendar, WhatsApp, a day plan, several tasks at
once, a time change to an event waiting for approval, or approving a waiting task by voice.
Everything else returns None and goes to the ordering assistant as before.

Every reply says which workflow it belongs to, so the page shows that workflow's panel and never
another's. A question that is waiting for its answer is remembered (`Pending`), so "Who should I
message?" followed by "Rahul" continues the WhatsApp message instead of reaching the grocery
assistant, which was the original bug.

Voice approval is two steps on purpose. Saying "send it" makes Saathi read the exact content back
and remember what it read. Only an explicit "confirm" after that sends exactly that content, and
only if it is still what was read out. A bare "yes" never starts it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, cast

from app.assistant.router import keyword_domain
from app.genui.models import Widget
from app.observability import log_event
from app.orchestrator.confirmation import explicit_confirmation
from app.personal.dates import parse_when
from app.tasks import kinds as K
from app.tasks import render, rules
from app.tasks.engine import PendingState, Snapshot, TaskEngine, VoicePending
from app.tasks.pending import Pending, workflow_of
from app.tasks.planner import (
    Plan,
    PlanExtractor,
    PlannedTask,
    complete,
    looks_like_a_plan,
    make_plan,
)

VOICE_WINDOW_SECONDS = 120.0
QUESTION_WINDOW_SECONDS = 600.0
MAX_TRIES = 2

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
_CANCEL = re.compile(
    r"^\W*(?:cancel|never\s*mind|forget\s+it|leave\s+it|stop|no\s+thanks?|नहीं|रहने\s+दो|छोड़ो|"
    r"ಬೇಡ|ಬಿಡಿ)\W*$",
    re.IGNORECASE,
)
_LEAD_IN = re.compile(
    r"^\W*(?:it'?s|it\s+is|to|send(?:\s+it)?\s+to|tell|say|saying|that|the\s+message\s+is|"
    r"message\s*:|message|the\s+email\s+should\s+say|email\s*:)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TurnReply:
    message: str
    widgets: list[Widget] = field(default_factory=list[Widget])
    plan_id: str | None = None
    active: bool = False
    workflow: str = "tasks"  # which panel the page shows: whatsapp, email, calendar, ...
    waiting_for: str | None = None  # the missing detail, when a question is open


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
_DROPPED = {
    "en-IN": "Okay, I've dropped that.",
    "hi-IN": "ठीक है, मैंने उसे छोड़ दिया।",
    "kn-IN": "ಸರಿ, ಅದನ್ನು ಬಿಟ್ಟಿದ್ದೇನೆ.",
}
_ORDER_WAITING = {
    "en-IN": "Your order is still waiting. To place it, say back to my groceries first, "
    "then confirm there.",
    "hi-IN": "आपका ऑर्डर अभी रुका है। उसे देने के लिए पहले मेरे किराने पर वापस कहें, फिर वहीं कन्फर्म करें।",
    "kn-IN": "ನಿಮ್ಮ ಆರ್ಡರ್ ಇನ್ನೂ ಕಾಯುತ್ತಿದೆ. ಅದನ್ನು ಮಾಡಲು ಮೊದಲು ನನ್ನ ದಿನಸಿಗೆ ವಾಪಸ್ ಎನ್ನಿ, ನಂತರ ಅಲ್ಲಿ ದೃಢಪಡಿಸಿ.",
}
# The question, in the person's language. Keys: (task kind, missing detail).
_ASK: dict[tuple[str, str], dict[str, str]] = {
    ("whatsapp.send", "to"): {
        "en-IN": "Who should I message? Say a name, or type a number like +91 98765 43210.",
        "hi-IN": "किसे मैसेज भेजूँ? नाम बोलें, या +91 98765 43210 जैसा नंबर लिखें।",
        "kn-IN": "ಯಾರಿಗೆ ಸಂದೇಶ ಕಳುಹಿಸಲಿ? ಹೆಸರು ಹೇಳಿ, ಅಥವಾ +91 98765 43210 ಎಂದು ಸಂಖ್ಯೆ ಬರೆಯಿರಿ.",
    },
    ("whatsapp.send", "body"): {
        "en-IN": "What should I say to {to}?",
        "hi-IN": "{to} को क्या लिखूँ?",
        "kn-IN": "{to} ಅವರಿಗೆ ಏನು ಹೇಳಲಿ?",
    },
    ("gmail.draft_reply", "to"): {
        "en-IN": "Who should I write the email to? Say a contact name, or type their address.",
        "hi-IN": "ईमेल किसे लिखूँ? संपर्क का नाम बोलें, या पता लिखें।",
        "kn-IN": "ಇಮೇಲ್ ಯಾರಿಗೆ ಬರೆಯಲಿ? ಸಂಪರ್ಕದ ಹೆಸರು ಹೇಳಿ, ಅಥವಾ ವಿಳಾಸ ಬರೆಯಿರಿ.",
    },
    ("gmail.draft_reply", "intent"): {
        "en-IN": "What should the email to {to} say?",
        "hi-IN": "{to} को ईमेल में क्या लिखूँ?",
        "kn-IN": "{to} ಅವರಿಗೆ ಇಮೇಲ್‌ನಲ್ಲಿ ಏನು ಬರೆಯಲಿ?",
    },
    ("calendar.create", "when"): {
        "en-IN": "When should I schedule it? For example, tomorrow at 3 PM.",
        "hi-IN": "कब रखूँ? जैसे, कल दोपहर 3 बजे।",
        "kn-IN": "ಯಾವಾಗ ಇಡಲಿ? ಉದಾಹರಣೆಗೆ, ನಾಳೆ ಮಧ್ಯಾಹ್ನ 3 ಗಂಟೆಗೆ.",
    },
    ("calendar.availability", "when"): {
        "en-IN": "Which day and time should I check?",
        "hi-IN": "कौन सा दिन और समय देखूँ?",
        "kn-IN": "ಯಾವ ದಿನ ಮತ್ತು ಸಮಯ ನೋಡಲಿ?",
    },
    ("reminder.create", "text"): {
        "en-IN": "What should I remind you about?",
        "hi-IN": "किस बारे में याद दिलाऊँ?",
        "kn-IN": "ಯಾವುದರ ಬಗ್ಗೆ ನೆನಪಿಸಲಿ?",
    },
}
_RETRY = {
    "en-IN": "I didn't catch that. {question}",
    "hi-IN": "मैं समझ नहीं पाया। {question}",
    "kn-IN": "ನನಗೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. {question}",
}


def _say(table: dict[str, str], language: str, **values: object) -> str:
    return table.get(language, table["en-IN"]).format(**values)


def _question(pending: Pending, language: str) -> str:
    table = _ASK.get((pending.kind, pending.slot))
    if table is None:
        return pending.question
    return _say(table, language, to=str(pending.args.get("to", "")))


def workflow_for_kinds(kinds: list[str]) -> str:
    """One workflow if every task belongs to the same one, otherwise the mixed 'tasks' board."""
    names = {workflow_of(kind) for kind in kinds}
    return names.pop() if len(names) == 1 else "tasks"


def stray_confirmation_reply(language: str) -> str:
    """A bare 'yes' while another workflow is on screen must not confirm a paused order."""
    return _say(_ORDER_WAITING, language)


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


def _is_new_request(text: str) -> bool:
    """True when an answer-shaped reply is really a different request (so don't swallow it)."""
    return looks_like_a_plan(text) or keyword_domain(text) is not None


def _strip_lead_in(text: str) -> str:
    cleaned = text.strip()
    while True:
        shorter = _LEAD_IN.sub("", cleaned, count=1)
        if shorter == cleaned:
            break
        cleaned = shorter
    return cleaned.strip(" \t\n.,;:!?\"'।")


@dataclass(frozen=True)
class _Filled:
    task: PlannedTask


@dataclass(frozen=True)
class _Still:
    pending: Pending


@dataclass(frozen=True)
class _Retry:
    pass


@dataclass(frozen=True)
class _Moved:
    pass


def _fill(pending: Pending, text: str, now: Any) -> _Filled | _Still | _Retry | _Moved:
    """Apply an answer to the question that was asked."""
    answer = _strip_lead_in(text)
    known = dict(pending.args)
    kind, slot = pending.kind, pending.slot
    if slot == "to":
        if explicit_confirmation(text) is True or _is_new_request(text):
            return _Moved()
        squeezed = answer.replace(" ", "").replace("-", "")
        target: str | None = None
        if kind == "whatsapp.send" and K.is_phone(squeezed):
            target = squeezed
        elif kind == "gmail.draft_reply" and K.EMAIL_PATTERN.match(answer):
            target = answer.lower()
        elif len(answer.split()) <= 4:
            target = rules.good_name(answer, allow_phone=False)
        if not target:
            return _Retry()
        if kind == "whatsapp.send":
            if known.get("body"):
                return _Filled(PlannedTask(kind, {"to": target, "body": known["body"]}))
            return _Still(Pending(kind, "body", {"to": target}, ""))
        if known.get("intent"):
            return _Filled(PlannedTask(kind, {"to": target, "intent": known["intent"]}))
        return _Still(Pending(kind, "intent", {"to": target}, ""))
    if slot in ("body", "intent", "text"):
        if not answer:
            return _Retry()
        if kind == "whatsapp.send":
            return _Filled(PlannedTask(kind, {"to": known["to"], "body": answer[:1000]}))
        if kind == "gmail.draft_reply":
            return _Filled(PlannedTask(kind, {"to": known["to"], "intent": answer[:500]}))
        return _Filled(PlannedTask(kind, {"text": answer[:500], "when_text": None}))
    if slot == "when":
        parsed = parse_when(text, now)
        if parsed is None:
            return _Moved() if _is_new_request(text) else _Retry()
        known["date"] = parsed.due.date().isoformat()
        known["start"] = parsed.due.strftime("%H:%M")
        return _Filled(PlannedTask(kind, known))
    return _Moved()


async def _answer_question(
    engine: TaskEngine, user_id: str, language: str, text: str
) -> TurnReply | None:
    state = engine.questions.get(user_id)
    if state is None:
        return None
    if time.monotonic() > state.expires:
        engine.questions.pop(user_id, None)
        return None
    pending = state.pending
    if _CANCEL.match(text):
        engine.questions.pop(user_id, None)
        return TurnReply(_say(_DROPPED, language), workflow=pending.workflow)
    outcome = _fill(pending, text, engine.now())
    if isinstance(outcome, _Moved):
        engine.questions.pop(user_id, None)  # they moved on: route the new request normally
        return None
    if isinstance(outcome, _Retry):
        state.tries += 1
        if state.tries >= MAX_TRIES:
            engine.questions.pop(user_id, None)
            return TurnReply(_say(_DROPPED, language), workflow=pending.workflow)
        return TurnReply(
            _say(_RETRY, language, question=_question(pending, language)),
            workflow=pending.workflow,
            waiting_for=pending.slot,
        )
    if isinstance(outcome, _Still):
        engine.questions[user_id] = PendingState(
            outcome.pending, time.monotonic() + QUESTION_WINDOW_SECONDS
        )
        return TurnReply(
            _question(outcome.pending, language),
            workflow=outcome.pending.workflow,
            waiting_for=outcome.pending.slot,
        )
    engine.questions.pop(user_id, None)
    plan = complete([outcome.task])
    return await _start(engine, user_id, language, text, plan.tasks)


async def _start(
    engine: TaskEngine, user_id: str, language: str, text: str, tasks: tuple[PlannedTask, ...]
) -> TurnReply | None:
    outcome = await engine.submit(user_id, text, language, Plan(tasks))
    if isinstance(outcome, str):
        return TurnReply(_say(_BUSY, language)) if outcome in ("busy", "too_many") else None
    log_event("tasks", op="turn", outcome="planned", count=len(outcome.tasks))
    return TurnReply(
        _say(_STARTED_ONE if len(outcome.tasks) == 1 else _STARTED, language, n=len(outcome.tasks)),
        await _widgets(engine, user_id, outcome),
        outcome.plan.id,
        True,
        workflow_for_kinds([t.kind for t in outcome.tasks]),
    )


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

    if user_id is not None:
        answered = await _answer_question(engine, user_id, language, stripped)
        if answered is not None:
            return answered

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
                    "calendar",
                )

    if not looks_like_a_plan(stripped):
        return None
    if user_id is None:
        return TurnReply(_say(_SIGN_IN, language))
    plan = await make_plan(stripped, language, engine.now(), extractor)
    if not plan.tasks:
        if plan.pending is not None:
            engine.questions[user_id] = PendingState(
                plan.pending, time.monotonic() + QUESTION_WINDOW_SECONDS
            )
            return TurnReply(
                _question(plan.pending, language),
                workflow=plan.pending.workflow,
                waiting_for=plan.pending.slot,
            )
        return TurnReply(plan.clarification) if plan.clarification else None
    engine.questions.pop(user_id, None)  # a new request replaces any open question
    return await _start(engine, user_id, language, stripped, plan.tasks)


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
            _say(_DONE, language),
            widgets,
            snapshot.plan.id if snapshot else None,
            False,
            workflow_for_kinds([t.kind for t in snapshot.tasks]) if snapshot else "tasks",
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
        workflow_for_kinds([t.kind for t in snapshot.tasks]),
    )
