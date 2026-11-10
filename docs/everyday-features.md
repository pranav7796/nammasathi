# Everyday features: widgets, reminders, notes, waitlist

Added after the ordering flow, for the Gnani challenge. Gmail, Calendar and WhatsApp setup: [integrations-setup.md](integrations-setup.md). Contract for widgets: [genui-spec.md](genui-spec.md).

## What works end to end (tested)

| Feature | What a person can do | Proof |
|---|---|---|
| **Reminders** | Say or type "remind me to call Amma tomorrow at 7 pm" in English, Hindi or Kannada. It is saved, listed, can be ticked off or deleted. Also a form under **Reminders**. | `tests/test_personal_voice.py`, `tests/test_personal.py`, `tests/test_personal_dates.py`, `scripts/e2e_personal.py` |
| **Notes** | Add and delete notes under **Reminders**. | `tests/test_personal.py`, `scripts/e2e_personal.py` |
| **Waitlist** | Leave an email, with consent, on the home page, without signing in. A button in the hero leads to the form. | `tests/test_waitlist.py`, `scripts/e2e_personal.py` |
| **Share and calendar** | After a finished demo order or booking: add to calendar (an `.ics` made in the browser), share on WhatsApp, email, copy. | `tests/test_share_ui.py`, `tests/frontend/genui.test.cjs` |
| **Widget engine** | The server builds each widget, validates it, and the browser draws only approved types. | `tests/test_genui.py`, `tests/frontend/genui.test.cjs`, `scripts/genui_browser_check.py` |

Reminders, notes and history need a signed-in account (so `DATABASE_URL` and `TOKEN_ENCRYPTION_KEYS`).
The waitlist needs the database too, and both the form and the hero button are hidden where there is none.
It allows 20 attempts an hour from one address and 1000 for the whole site, refuses cross-site
requests, and silently discards a submission that fills the hidden `website` field. Nothing is
emailed on sign-up, and the page says so.

## What it honestly does not do

- **Reminders do not alert anyone.** There is no push, email or SMS. A reminder is a list entry, and
  every spoken reply says "Saathi can't alert you yet".
- **Nothing is sent for you.** Share, WhatsApp and email open the person's own app with the text
  filled in. They press Send. There is no Gmail, Google Calendar or WhatsApp Business integration,
  because none could be tested without credentials. The widget catalogue has a `composer` and a
  `calendar_view`, but nothing in the server builds them yet.
- **Several requests in one sentence are not split up.** "Check my email, book a table, and remind
  me" is not decomposed into parallel tasks. Each sentence goes to one capability.
- Date parsing is rule-based: clear phrases work, ambiguous ones are saved without a time.
- Reminder and write limits, and the waitlist limiter, are in memory and reset on restart.

## How the pieces fit

```
speech or text -> reminder_request()  -> PersonalService -> database -> reply + reminder_list widget
               -> (otherwise) assistant -> workflow -> reply + share_panel widget when DONE
browser: saathiGenUI.render(widgets) -> action id -> REST call (checked again on the server)
```

The model never writes UI and has no tools (`AGENTS.md`). Widgets are built by `app/genui/builders.py`
from data the server already holds, validated by `app/genui/models.py`, and drawn with `textContent`
only by `app/static/genui.js`.

## Running it

```bash
KEY=$(uv run python -m app.accounts_admin generate-key | tail -1)
DATABASE_URL=sqlite:///var/dev.db TOKEN_ENCRYPTION_KEYS="$KEY" uv run uvicorn app.web:app --port 8123
```

Read the waitlist: `uv run python -m app.accounts_admin waitlist [--csv]`, remove an entry with
`waitlist-remove EMAIL`. Browser checks: `uv run --with playwright python scripts/e2e_personal.py
http://localhost:8123 OUT_DIR` (needs `chromium` on the path).

## Not in production until you deploy

These are on the feature branch only. Deploying runs migration 3 on the production database (it adds
three tables and changes nothing else).

---

# Update: the task board, Gmail, Calendar, WhatsApp, My day, memory

**Implemented and tested against stand-ins. Not live-verified** (no real Google or Meta credentials were
available). Without credentials each service shows labelled **Demo** data. See
[integrations-setup.md](integrations-setup.md) to connect the real ones.

## What happens to one sentence

"Check my important emails, schedule a meeting with Rahul tomorrow afternoon, remind me to buy groceries,
and send my mother a WhatsApp message saying I'll be late" becomes five tasks on a board.

```
text or speech -> looks_like_a_plan() -> make_plan()   (a model proposes, rules are the fallback)
              -> validate every task against the closed catalogue (app/tasks/kinds.py)
              -> TaskEngine: reads run together; anything that changes something stops at
                 "waiting for approval" with the exact content
              -> the person approves (or edits) -> one compare-and-set -> the effect runs once
              -> the result is read back before it is called done
```

| Promise | Where it is enforced | Test |
|---|---|---|
| Independent reads run at the same time, and the board says so only from recorded timestamps | `engine.overlapping` | `test_task_engine.py` |
| Nothing external runs before approval | `engine.approve` | `test_nothing_that_changes_something_runs_before_approval` |
| Approving twice acts once | database compare-and-set | `test_approving_twice_at_once_acts_once` |
| What is sent is what was approved; any edit changes the hash | `executors.args_hash` | `test_what_is_sent_is_exactly_what_was_approved_after_editing` |
| An unconfirmed effect is never called a success and is never retried blindly | `engine.approve`, `recover_interrupted` | `test_an_unconfirmed_send_...`, `test_an_interrupted_effect_...` |
| One person cannot touch another's tasks | every query filters by user | `test_one_person_cannot_touch_anothers_tasks` |
| Text inside an email is data, never an instruction | no planner call on email content | `test_instructions_inside_an_email_are_inert_data` |
| A made-up demo contact is never used on a real channel | `DemoContacts` | `test_a_demo_contact_is_never_used_for_a_real_channel` |
| Spoken approval needs a readback and an explicit confirm | `turn._voice_approval` | `test_voice_approval_...` |
| "Make it 4 PM instead" updates the same widget | widget id `event.<task id>` | `test_changing_the_time_...` |

## What works with no credentials

Planning in English, Hindi and Kannada, the task board, the day planner, reminders, notes, memory,
contacts, share and calendar-file buttons, the guided demo, and every Demo-labelled email, event and
message. Nothing leaves the site.

## Limits

- Live Gmail, Calendar and WhatsApp are untested against the real services.
- WhatsApp sends free text only (no templates), so it works only inside Meta's 24-hour window.
- Rescheduling and cancelling an existing event is in the engine but not reachable by speech, because
  the planner cannot yet name an event. Changing the time of an event you are about to create is.
- Task and approval wording on the board is English; spoken replies are in the chosen language.
- Rate limits and the idempotency memory of adapters are per process.
- Web research, PDF summaries, translation, expense tracking and local discovery are not built.
