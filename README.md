# Namma Saathi

**Your personal AI for everyday life.** Tell Namma Saathi what you need, by voice, in Kannada,
Hindi, or English, and it helps get it done.

Today it can do three things through one conversation: order groceries from Instamart, order a
meal from Swiggy Food, and reserve a table on Swiggy Dineout.

The LLM only understands language. It has no tools and can't touch a cart or place an order.
Deterministic code owns every decision. Namma Saathi grew out of Ajji Mart, a grocery assistant.

## Status

**No real order or booking has been placed.** All three journeys work on built-in mock services.

| | Mock journey | Live reads | Live cart change | Live order or booking |
|---|---|---|---|---|
| Instamart | Works | Verified | Worked once in a rehearsal | Never run |
| Swiggy Food | Works | Search and order history verified | Never run | Never run |
| Swiggy Dineout | Works | Search and slots verified | Not applicable | Never run |

Swiggy has not confirmed that a personal app may place real orders. See
[docs/first-real-order.md](docs/first-real-order.md) and
[docs/swiggy-application.md](docs/swiggy-application.md).

## Run it

```bash
cp .env.example .env     # add GNANI_API_KEY and GEMINI_API_KEY
uv sync
uv run uvicorn app.web:app
```

Open <http://localhost:8000>. See [docs/demo-script.md](docs/demo-script.md) for what to say.
Without a browser, `uv run python -m app.text_demo` and `app.voice_demo` run groceries on the mock.

## Configuration

Everything real is off unless switched on.

| Setting | Default | Effect |
|---|---|---|
| `COMMERCE_BACKEND` | `mock` | `mock` never constructs a Swiggy client |
| `SWIGGY_LIVE_ACCESS` | `false` | Read your real account |
| `ALLOW_SWIGGY_CART_WRITES` | `false` | Change your real Instamart and Food carts |
| `ALLOW_REAL_CHECKOUT` | `false` | Place real orders; needs the above and `MAX_REAL_ORDER_INR` |
| `ALLOW_REAL_BOOKING` | `false` | Book real free tables |
| `MAX_REAL_ORDER_INR` | none | Refuse any order above this total |
| `REAL_ORDER_LIMIT` | `1` | Real orders and bookings allowed before refusing more |
| `ORDER_JOURNAL_PATH` | `var/order_attempts.jsonl` | Durable record of every attempt |
| `GNANI_TTS_VOICE` | per language | Saanvi, Nalini, or Kaveri unless overridden |

Gemini is the primary LLM, with Groq and then OpenRouter as fallbacks. OpenRouter's free model may
log requests, so it is disabled when a real account is configured.

## Tools

```bash
uv run python -m app.swiggy_auth        # sign in to Swiggy; the token stays in the OS keyring
uv run python -m app.swiggy_preflight   # read-only readiness report for Instamart
uv run python -m app.order_admin status # every attempt in the journal, and how to settle one
```

## How real actions stay safe

- **Nothing incompatible is chosen silently.** A product is chosen without asking only if exactly
  one in-stock option makes exactly what was asked. Otherwise up to three are read out.
- **Approval is bound to what you heard.** A yes covers one exact readback, for the current task
  only. Any change needs a new readback, and the LLM's opinion never counts as a yes.
- **Your existing carts are protected.** They are read first and kept, and edits made in the Swiggy
  app are not overwritten. A Food cart never silently switches restaurant.
- **Nothing runs twice by accident.** Every attempt is saved before the call. An unclear result is
  checked against order history. Anything unproven blocks all real actions, across tasks and
  restarts, until a person settles it.
- **Results are verified and worded truthfully.** A real order is never called a demo, nor a demo
  real, and an unverified order is never called verified.

See [DESIGN.md](DESIGN.md) §16 for the architecture and
[docs/swiggy-integration-notes.md](docs/swiggy-integration-notes.md) for verified Swiggy behaviour.

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
node --test tests/frontend/namma-saathi.test.cjs
```

Tests can't reach the network or the OS credential store: `tests/conftest.py` makes any attempt fail.

## Known limitations

- **Food:** optional add-ons are not offered, and dishes that require add-ons or several separate
  choices are left out. There is no "your usual". One restaurant per cart.
- **Dineout:** free reservations only, since paid deals need UPI. Search works only for places with
  documented coordinates: Bengaluru, Koramangala, Indiranagar, Mumbai, and Delhi. Its live
  responses are text, so a format change would stop bookings rather than risk a wrong one. There is
  no booking history to check, so a lost reply stays unknown until a person checks the app.
- **Payment:** cash on delivery only.
- **Voice:** tested with synthesized speech, not yet with people at a microphone. Kannada and Hindi
  phrases need review by native speakers. Hinglish goes through `hi-IN`.
- **Sessions:** in memory, so a restart loses the conversation but not the journal. One user, one
  process, on this machine.
- **Speed:** a turn can take 5 to 40 seconds, and an unclear checkout about 30 seconds more.
