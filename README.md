# Ajji Mart

A voice-first grocery assistant that helps elderly users order from Swiggy Instamart
in Kannada, Hindi, or English.

Prisma STT → LLM intent extraction → deterministic state machine → commerce adapter →
Timbre TTS.

The LLM only understands language. It has no tools and cannot touch the cart or place
an order. Deterministic code owns every decision.

## Status

The full journey works by voice in all three languages against the mock shop:
request, product choice, cart, readback, spoken confirmation, order, verification,
spoken result. Read-only access to the real Swiggy account has been verified.

**No real order has been placed.** Writing to the real cart and real checkout have
never been exercised live. Both are off by default, and the procedure is in
[docs/first-real-order.md](docs/first-real-order.md).

## Setup

```bash
cp .env.example .env
uv sync
uv run pytest
```

## Run it

```bash
uv run uvicorn app.web:app --reload     # mock mode only; never --reload with a real account
```

Open <http://localhost:8000>. Choose a language and address, press **Tap to speak**, and
finish with **Finish speaking**. Needs `GNANI_API_KEY` and `GEMINI_API_KEY`.

Without a browser: `uv run python -m app.text_demo` (typed), or
`uv run python -m app.voice_demo --language hi-IN request.wav` (audio files). Both use
the mock shop.

## Configuration

Everything real is off unless switched on, and each switch requires the one before it.

| Setting | Default | Effect |
|---|---|---|
| `COMMERCE_BACKEND` | `mock` | `mock` never constructs a Swiggy client |
| `SWIGGY_LIVE_ACCESS` | `false` | Read your real account |
| `ALLOW_SWIGGY_CART_WRITES` | `false` | Change your real cart |
| `ALLOW_REAL_CHECKOUT` | `false` | Place real orders; needs the above and `MAX_REAL_ORDER_INR` |
| `MAX_REAL_ORDER_INR` | none | Refuse any order above this total |
| `REAL_ORDER_LIMIT` | `1` | Orders the journal may record before refusing more |
| `ORDER_JOURNAL_PATH` | `var/order_attempts.jsonl` | Durable checkout attempt record |
| `GNANI_TTS_VOICE` | per language | Saanvi, Nalini, or Kaveri unless overridden |

See `.env.example` for the LLM and speech settings. Gemini is primary, with Groq and
OpenRouter as fallbacks. OpenRouter's free model may log requests, so it is disabled
when a real account is configured.

## Tools

```bash
uv run python -m app.swiggy_auth        # sign in to Swiggy (OAuth + PKCE; token in OS keyring)
uv run python -m app.swiggy_preflight   # read-only readiness report; prints no personal data
uv run python -m app.order_admin status # checkout attempts in the journal
```

## How an order stays safe

- **Nothing incompatible is chosen silently.** A product is chosen without asking only
  if exactly one in-stock variant, from a matching product, makes exactly the requested
  amount. Otherwise up to three options are spoken and shown.
- **Questions keep your place.** Resolved items and the rest of the list wait while one
  question is answered, and the cart is written once.
- **Approval is bound to what you heard.** A yes covers one exact readback: items, prices,
  total, address, and payment. Any change needs a new readback. The LLM's opinion never
  counts as a yes.
- **Your existing cart is protected.** It is read first and kept, and an edit made in the
  Swiggy app between turns is never overwritten. Cancel restores what you started with.
- **Checkout cannot run twice by accident.** Each attempt is written to disk before the
  call. An unclear result is checked against order history by looking for a new order ID.
  What cannot be proven becomes `ORDER_UNKNOWN`, which blocks checkout across sessions and
  restarts until a person settles it.
- **Payment is verified.** Cash on delivery must be reported available for the current cart
  before you are asked to confirm.
- **The result is verified and worded truthfully.** Orders are checked in order history or
  tracking, and a real order is never called a demo, nor a demo real.

See [DESIGN.md](DESIGN.md) for the design, [AGENTS.md](AGENTS.md) for rules for coding
agents, and [docs/swiggy-integration-notes.md](docs/swiggy-integration-notes.md) for
verified Swiggy behaviour.

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
node --test tests/frontend/ajji-mart.test.cjs
```

Tests cannot reach the network or the OS credential store: `tests/conftest.py` makes any
attempt fail.

## Known limitations

- Real `update_cart`, `checkout`, and a populated `get_orders` have never been exercised
  live, because they need real writes. See the notes file for each unobserved detail.
- Kannada and Hindi phrases need review by native speakers, ideally with elderly users.
- Tested with synthesized speech through the API, not yet with a person at a microphone.
- Cash on delivery only. Sessions are in memory, so a restart loses the conversation but
  not the order journal. One user, one process, on this machine.
- A turn can take 5 to 20 seconds, mostly in the LLM, and an unclear checkout up to about
  30 seconds more.
- Hinglish is handled through `hi-IN`; Prisma v3 multi-language hints are not used yet.
