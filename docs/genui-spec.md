# Generative UI contract

Namma Saathi shows interactive widgets next to its spoken reply. **The model never writes UI.**
Server code builds each widget from facts it already holds (the cart, a booking, a reminder list),
validates it against this contract, and sends it as data. The browser renders only the component
types below, with `textContent` only: no HTML, no script, no styles from the server.

Fits `AGENTS.md`: the LLM only interprets; deterministic code owns every effect; prices, ids and
totals come from commerce data, never from the client. An `action_id` is a *request*; the server
re-checks authorisation, ownership and state before doing anything.

## Envelope

A reply may carry `"ui": [Widget, ...]`. Every widget has a stable `id` (`^[a-z0-9_.:-]{1,64}$`).
The browser keeps at most one widget per id: a widget arriving with an existing id **replaces it in
place**, so a cart or task board updates instead of duplicating. Limits: at most 12 widgets per
reply, nesting depth at most 3, at most 40 children, text at most 2000 characters. Unknown `type`,
unknown property, or any limit exceeded: the widget is rejected (the server raises `GenUIError`; the
browser skips it and shows nothing for it).

## Common types

- `Status` = `pending | running | waiting_for_input | waiting_for_approval | completed | failed | cancelled`
- `Action` = `{ "label": str(1..60), "action_id": str, "style": "primary"|"secondary"|"danger" (default "secondary"), "disabled": bool (default false) }`
- `Field` = `{ "name": ^[a-z][a-z0-9_]{0,31}$, "label": str, "kind": "text"|"textarea"|"select"|"checkbox"|"date"|"time"|"number", "required": bool, "value": str|number|bool|null, "options": [{"value": str, "label": str}] (select only, 1..30), "min": number|null, "max": number|null, "max_length": int|null (default 500), "help": str|null }`

## Components (`type`)

| type | properties (besides `id`) |
|---|---|
| `heading` | `text`, `level` 1..3 |
| `text` | `text`, `tone`: `normal`\|`muted`\|`warning` |
| `card` | `title`, `body` (opt), `children`: Widget[] (nesting counts), `actions`: Action[] (opt) |
| `list` | `title` (opt), `items`: `{id, title, detail?, badge?}`[] , `empty_text` |
| `action_panel` | `title`, `actions`: Action[] (1..8) |
| `form` | `title`, `fields`: Field[] (1..12), `submit`: Action |
| `task_board` | `title`, `tasks`: `{id, title, status: Status, detail?, retry_action_id?, cancel_action_id?}`[] |
| `progress` | `label`, `value` 0..100 |
| `cart` | `title`, `lines`: `{id, name, quantity: int 0..99, unit_price_inr: str "78.00", detail?}`[], `total_inr: str`, `fees_inr: str\|null`, `editable: bool`, `demo: bool` ; quantity edits post `{action_id: "cart.set_quantity", line_id, quantity}` |
| `table` | `title` (opt), `columns`: str[] (1..8), `rows`: str[][] (cells as text, at most 50 rows) |
| `confirmation` | `title`, `details`: `{label, value}`[], `consequence` (what happens on yes), `confirm`: Action, `reject`: Action, `demo: bool` |
| `composer` | `channel`: `email`\|`message`, `to` (opt), `subject` (opt), `body`, `send`: Action, `copy_enabled: bool` (copy/share fallback) |
| `calendar_view` | `title`, `days`: `{date: "2026-11-09", events: {id, title, start: "HH:MM", end: "HH:MM", detail?}[]}`[] |
| `reminder_list` | `title`, `items`: `{id, text, due: ISO-8601 str\|null, done: bool}`[] |
| `note_list` | `title`, `items`: `{id, body, updated: ISO-8601 str}`[] |
| `share_panel` | `title`, `summary` (text to share), `calendar`: `{title, start: ISO-8601 with offset, end: ISO-8601 with offset, location?, description?}\|null` ; the browser offers "Add to calendar" (an `.ics` made locally), "Share on WhatsApp" (`https://wa.me/?text=`), "Email it" (`mailto:`), "Copy". Nothing is sent by the app. |
| `status` | `kind`: `loading`\|`success`\|`empty`\|`error`, `message`, `retry`: Action\|null |

## Interaction

The browser calls `onAction({ widgetId, actionId, values })`, where `values` holds form field
values or the cart edit. It never computes prices or totals. `onAction` is supplied by the page;
what an `action_id` does is decided server-side.

## Rendering rules

Plain DOM, `textContent` only, no `innerHTML`. Keyboard operable, labelled controls, visible focus,
`aria-live="polite"` for a task board and status. Works at 360px wide. Respects
`prefers-reduced-motion`. The page's existing look (CSS variables in `style.css`) is reused.
