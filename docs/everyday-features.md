# Everyday features: widgets, reminders, notes, waitlist

Added after the ordering flow, for the Gnani challenge. Contract for widgets: [genui-spec.md](genui-spec.md).

## What works end to end (tested)

| Feature | What a person can do | Proof |
|---|---|---|
| **Reminders** | Say or type "remind me to call Amma tomorrow at 7 pm" in English, Hindi or Kannada. It is saved, listed, can be ticked off or deleted. Also a form under **Reminders**. | `tests/test_personal_voice.py`, `tests/test_personal.py`, `tests/test_personal_dates.py`, `scripts/e2e_personal.py` |
| **Notes** | Add and delete notes under **Reminders**. | `tests/test_personal.py`, `scripts/e2e_personal.py` |
| **Waitlist** | Leave an email, with consent, on the home page. | `tests/test_waitlist.py`, `scripts/e2e_personal.py` |
| **Share and calendar** | After a finished demo order or booking: add to calendar (an `.ics` made in the browser), share on WhatsApp, email, copy. | `tests/test_share_ui.py`, `tests/frontend/genui.test.cjs` |
| **Widget engine** | The server builds each widget, validates it, and the browser draws only approved types. | `tests/test_genui.py`, `tests/frontend/genui.test.cjs`, `scripts/genui_browser_check.py` |

Reminders, notes and history need a signed-in account (so `DATABASE_URL` and `TOKEN_ENCRYPTION_KEYS`).
The waitlist needs the database too and is hidden where there is none.

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
