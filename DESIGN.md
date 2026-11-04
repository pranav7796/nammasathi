# DESIGN.md --- Ajji Mart

**Project:** Voice-first grocery assistant for elderly users\
**Competition:** Gnani AI Great Indian AI Internship Challenge\
**Stated deadline:** 10 November 2026, 11:59 PM IST\
**Current delivery target:** A reliable local end-to-end demo using real
Gnani speech APIs where integrated and mock commerce checkout.

------------------------------------------------------------------------

## 1. Problem and product goal

Ajji Mart helps an elderly user place a grocery request by speaking
naturally in Kannada, Hindi, or Hinglish. The system converts speech to
text, extracts the requested items, searches a commerce catalogue,
confirms the exact cart aloud, and completes a simulated order in the
local MVP.

The product must prioritize correctness and user trust over speed or
autonomy. A wrong grocery order can waste money and create real
inconvenience.

### Primary user journey

An elderly user says something like:

> "Ondu litre haalu, eradu kilo akki kodi."

The assistant: 1. understands the utterance; 2. identifies the requested
products and quantities; 3. searches the available catalogue; 4. asks a
single clarification if needed; 5. reads the exact cart back; 6. waits
for explicit confirmation; 7. completes the mock order; 8. reports the
result in the user's language.

### Product principles

-   Voice-first, with short and clear spoken turns.
-   Kannada/Hindi/Hinglish code-mixing.
-   One question at a time.
-   No silent substitutions or pack-size increases.
-   Exact-cart readback before any order attempt.
-   Deterministic control flow; the LLM is not trusted with side
    effects.
-   Local demo must work without Swiggy staging or production checkout.

------------------------------------------------------------------------

## 2. Scope and milestones

### 2.1 Tonight's MVP

The immediate objective is one complete, repeatable local journey.

**In scope:** - A working deterministic dialog state machine. -
Structured intent extraction behind an interface. - Mock product search
and cart operations. - Cart readback with product, variant/pack size,
quantity, price, and total. - Explicit yes/no/unclear confirmation
handling. - Mock checkout and a clear simulated order result. - Real
Gnani Prisma STT and Gnani Timbre TTS if their documented APIs can be
integrated within the available time. - A simple local interface for
testing audio or text turns. - Tests for happy path and safety-critical
transitions.

**Fallback order for delivery:** 1. Working text-driven state machine +
mock commerce. 2. Turn-based real STT → intent extraction → state
machine → mock commerce → TTS. 3. A polished browser microphone
experience only after the backend journey is stable.

A text-driven demo is preferable to a voice demo that cannot reliably
complete an order.

### 2.2 Explicitly postponed

Do not implement these before the core journey works: - Full-duplex
streaming and barge-in. - Streaming LLM tokens. - Telephony
integration. - Multi-user concurrent calling. - Redis/PostgreSQL
infrastructure unless existing code makes it essential. - Production
checkout. - UPI and family payment handoff. - Order cancellation. -
Advanced analytics and dashboards. - Large-scale deployment and
enterprise authentication.

These may become later milestones, not prerequisites for the first demo.

### 2.3 Later integration milestones

**Milestone A --- Local mock MVP:** complete the journey with
`MockCommerce`.

**Milestone B --- Gnani voice:** integrate documented Prisma STT and
Timbre TTS APIs, initially turn-based.

**Milestone C --- Swiggy staging:** only after Swiggy grants staging
access and the endpoint/OAuth setup is verified. Use seeded staging
data.

**Milestone D --- Production:** only after production access and any
required review/authorization. Production checkout is out of scope for
the local demo.

------------------------------------------------------------------------

## 3. Requirements

  -----------------------------------------------------------------------
  ID                      Requirement             Acceptance evidence
  ----------------------- ----------------------- -----------------------
  R1                      The user can make a     Recorded/manual test
                          grocery request in      utterances produce the
                          Kannada, Hindi, or      expected intent.
                          Hinglish.               

  R2                      Every item and quantity Unit tests cover valid,
                          is validated before it  invalid, and ambiguous
                          enters the cart.        item requests.

  R3                      The assistant asks at   State-machine tests
                          most one clarification  verify clarification
                          question at a time.     behavior.

  R4                      Product names,          No model-generated
                          variants, pack sizes,   product facts reach the
                          prices, and             cart.
                          availability come from  
                          the commerce adapter.   

  R5                      The assistant reads     Readback tests assert
                          back the exact cart and all cart lines and
                          total before            totals.
                          confirmation.           

  R6                      A cart edit invalidates Tests prove a changed
                          previous confirmation.  cart requires a fresh
                                                  readback and yes.

  R7                      Only deterministic      LLM has no checkout
                          application code can    capability; tests
                          trigger checkout.       verify effect gating.

  R8                      The local MVP cannot    Default backend is
                          place a real order.     mock; production
                                                  checkout is disabled
                                                  and unavailable in the
                                                  MVP.

  R9                      External API failures   Mock failure-injection
                          are handled safely and  tests cover expected
                          explained to the user.  failures.

  R10                     The system records      Tests verify event
                          useful stage latency    recording and
                          without exposing        redaction.
                          sensitive data.         
  -----------------------------------------------------------------------

The earlier design's approximately one-second perceived latency goal is
a later optimization target, not a condition for the first working demo.
Measure latency before attempting to optimize it.

------------------------------------------------------------------------

## 4. Architecture

``` mermaid
flowchart LR
    U[User: browser mic or text] --> G[Local API / turn handler]
    G --> S[Speech-to-Text interface]
    S --> P[Gnani Prisma adapter]
    G --> I[Intent extraction interface]
    I --> L[Hosted LLM adapter]
    I --> F[Validated Intent]
    F --> D[Deterministic Dialog State Machine]
    D --> C[Commerce Adapter]
    C --> M[MockCommerce: default]
    C -. later, after approval .-> W[Swiggy staging adapter]
    D --> T[Text response]
    T --> V[Text-to-Speech interface]
    V --> N[Gnani Timbre adapter]
    N --> U
```

### Component responsibilities

  -----------------------------------------------------------------------
  Component               Owns                    Must not do
  ----------------------- ----------------------- -----------------------
  API / turn handler      Session lifecycle,      Make dialog or checkout
                          request validation,     decisions
                          invoking components     

  Prisma adapter          Gnani STT               Decide what the
                          request/response        transcript means
                          handling                

  Intent extractor        Convert                 Call commerce tools
                          utterance/context into  
                          validated intent        

  State machine           State transitions,      Network or filesystem
                          guards, and effect      I/O
                          decisions               

  Commerce adapter        Catalogue search, cart, Decide what the user
                          and mock checkout       said

  Timbre adapter          Convert approved        Invent response content
                          response text to audio  

  Persistence (if         Store                   Override state-machine
  present)                sessions/events/order   safety rules
                          attempts                
  -----------------------------------------------------------------------

### Repository layout

Use the existing repository convention. `pyproject.toml` currently
configures Pyright for `app` and `tests`, so `app/` is the canonical
application directory.

``` text
ajji-mart/
├── AGENTS.md
├── DESIGN.md
├── README.md
├── pyproject.toml
├── .env.example
├── app/
│   ├── main.py
│   ├── config.py
│   ├── orchestrator/
│   │   ├── models.py
│   │   └── state_machine.py
│   ├── commerce/
│   │   ├── base.py
│   │   └── mock.py
│   ├── llm/
│   │   ├── base.py
│   │   └── extractor.py
│   └── speech/
│       ├── stt.py
│       └── tts.py
└── tests/
    ├── test_state_machine.py
    ├── test_commerce_mock.py
    └── test_order_flow.py
```

This is a target shape, not permission to recreate the repository.
Inspect existing files and preserve working structure where possible.

------------------------------------------------------------------------

## 5. Dialog state machine

### 5.1 Ownership

The state machine is the single authority for dialog progression and
side-effect decisions. It must be pure and deterministic.

Suggested states:

-   `GREETING`
-   `COLLECTING`
-   `CLARIFYING`
-   `READBACK`
-   `CONFIRMED`
-   `PLACING`
-   `DONE`
-   `FAILED`

Suggested events:

-   `START`
-   `USER_UTTERANCE`
-   `INTENT_EXTRACTED`
-   `PRODUCTS_FOUND`
-   `CLARIFICATION_ANSWERED`
-   `CART_UPDATED`
-   `READBACK_REQUESTED`
-   `USER_CONFIRMED`
-   `USER_REJECTED`
-   `CHECKOUT_SUCCEEDED`
-   `CHECKOUT_FAILED`

Exact model names should follow existing code if equivalent types
already exist.

### 5.2 State flow

``` mermaid
stateDiagram-v2
    [*] --> GREETING
    GREETING --> COLLECTING: session initialized
    COLLECTING --> CLARIFYING: ambiguous item / pack / unavailable item
    CLARIFYING --> COLLECTING: clarification resolved
    COLLECTING --> READBACK: user says done
    READBACK --> COLLECTING: user edits cart
    READBACK --> CONFIRMED: explicit yes for current cart
    READBACK --> COLLECTING: user says no / wants changes
    CONFIRMED --> PLACING: mock checkout effect
    PLACING --> DONE: mock checkout succeeds
    PLACING --> FAILED: mock checkout fails
    FAILED --> READBACK: safe recovery after state is known
    DONE --> [*]
```

### 5.3 Safety invariants

1.  `PlaceOrder` is not an LLM tool. The LLM cannot emit it.
2.  In the MVP, `PlaceOrder` can only target `MockCommerce`.
3.  A confirmation is bound to the current cart version or deterministic
    `cart_hash`.
4.  Any cart mutation invalidates confirmation and returns the flow to
    `READBACK` or `COLLECTING`.
5.  An unclear answer is never treated as yes.
6.  A negative answer never triggers checkout.
7.  Product IDs, variant IDs, prices, and availability must be
    adapter-sourced.
8.  An invalid event or impossible transition fails closed and emits no
    commerce side effect.
9.  Checkout is emitted at most once for a given mock order attempt.
10. The state machine performs no I/O, reads no environment variables,
    and does not access a clock.

### 5.4 State behavior

**GREETING** - Initialize or load the session. - For the MVP, do not
require caller-phone lookup or user onboarding unless already
implemented.

**COLLECTING** - Accept a validated intent containing one or more
requested items. - Search the mock catalogue through the commerce
adapter. - Add only products/variants returned by the adapter. - If an
item is ambiguous or unavailable, move to `CLARIFYING`.

**CLARIFYING** - Ask one question. - Do not silently choose between
materially different products. - Do not silently increase the requested
quantity or pack size. - Return to `COLLECTING` after a valid
clarification.

**READBACK** - Read every cart line with product name, variant/pack
size, quantity, and price. - Read the total. - State the payment
assumption clearly. For the mock MVP, label it as simulated COD only if
that is what the mock represents. - Ask whether the user wants to
proceed. - A change requires a new readback.

**CONFIRMED** - Enter only after an explicit yes for the current cart
version/hash. - "Maybe", "hmm", silence, or ambiguous speech is not
confirmation.

**PLACING** - Execute only the mock checkout effect in the local MVP. -
Do not accept new order-changing input during the mock checkout
operation.

**DONE** - Report that the simulated order succeeded. - Clearly identify
it as a demo/mock order, not a real Swiggy order.

**FAILED** - Report failure without claiming an order was placed. -
Never retry an ambiguous real checkout. The local mock can be reset
safely according to its test contract.

------------------------------------------------------------------------

## 6. Intent extraction

The LLM converts the user's utterance into a strict structured
representation. A conceptual example:

``` json
{
  "intent": "add_items",
  "items": [
    {
      "name": "milk",
      "quantity": 1,
      "unit": "litre"
    },
    {
      "name": "rice",
      "quantity": 2,
      "unit": "kilogram"
    }
  ],
  "needs_clarification": false
}
```

This is an application-level example, not a claim about any provider's
API schema.

Rules: - Validate output with Pydantic or an equivalent strict schema. -
Reject unknown fields or invalid quantities where practical. - Do not
accept prices, totals, product IDs, or availability from the LLM as
authoritative. - The LLM may identify ambiguity; application code
decides the next state. - If output is invalid, allow at most one safe
retry, then ask the user to repeat or clarify. - Keep the extraction
prompt narrow and include only the context needed for the current turn.

------------------------------------------------------------------------

## 7. Catalogue, variants, and substitutions

The mock catalogue should include: - Common grocery items. -
Kannada/Hindi/English aliases where useful. - Multiple pack sizes for at
least a few products. - Prices and availability. - At least one
out-of-stock item to test clarification.

Rules: - The adapter is the source of truth for product and variant
identifiers. - If the user requests 2 kg and only 1 kg packs are
available, the application may propose two 1 kg packs, but must say so
and receive confirmation. - If only a larger pack is available than
requested, ask before selecting it. - Suggest no more than two
substitutes at a time. - Never silently substitute an item.

------------------------------------------------------------------------

## 8. Commerce adapter and environment policy

### 8.1 Interface

The domain-level adapter should expose only the operations required by
the MVP, for example:

``` python
class CommerceAdapter(Protocol):
    async def search(self, query: str) -> list[Product]: ...

    async def get_cart(self) -> Cart: ...

    async def set_cart(self, items: list[CartLine]) -> Cart: ...

    async def checkout(self) -> OrderResult: ...
```

Adapt this to existing repository models rather than copying it blindly.

### 8.2 Environments

  -----------------------------------------------------------------------
  Environment             Purpose                 Current status
  ----------------------- ----------------------- -----------------------
  `mock`                  Local development,      Available / default
                          demo, checkout, failure target
                          tests                   

  `swiggy_staging`        Seeded non-production   Not yet granted
                          integration testing     

  `swiggy_prod`           Authorized production   No production checkout
                          operations              in MVP
  -----------------------------------------------------------------------

**Current known access status:** Pranav has permission to build locally.
Staging and production access will be requested after the demo. Do not
assume staging allowlisting has been granted.

Important: - `localhost` describes where the application runs, not which
upstream Swiggy environment it contacts. - The existing design notes
that localhost OAuth may connect to Swiggy production and that checkout
there can create real orders. Treat any production operation as real. -
Do not call production checkout. - Keep `ALLOW_REAL_CHECKOUT=false`. -
Use `MockCommerce` for the entire local demo, including checkout. - A
later Swiggy integration must use the exact environment, OAuth setup,
and operations confirmed by Swiggy's official docs and the user's access
approval. - Do not invent a staging endpoint, OAuth callback, scope, or
tool contract. - Before enabling any non-mock adapter, verify the
configured host and fail closed if it is unexpected.

Official documentation starting points: -
https://mcp.swiggy.com/builders/docs/ -
https://mcp.swiggy.com/builders/llms.txt - https://docs.gnani.ai/

This document records the project's current access assumptions; it does
not replace provider documentation or access approval.

------------------------------------------------------------------------

## 9. Gnani speech integration

The intended speech components are: - **Prisma STT** for speech
recognition. - **Timbre TTS** for spoken responses.

Implementation order: 1. First establish a working text-turn pipeline
and mock commerce. 2. Add turn-based STT using the documented Gnani API.
3. Add turn-based TTS using the documented Gnani API. 4. Verify language
handling and audio formats with actual test recordings. 5. Consider
streaming only after the turn-based path is reliable.

Do not invent endpoints, authentication headers, payload fields, sample
rates, voice identifiers, or response formats. Verify each against the
current Gnani documentation.

Secrets: - Store keys in local environment variables. - Keep `.env` out
of version control. - Commit only placeholder variable names in
`.env.example`. - Never paste keys into Codex prompts, source files,
logs, screenshots, or chat.

------------------------------------------------------------------------

## 10. Error handling

The MVP should cover at least: - No matching product. - Ambiguous
product. - Out-of-stock product. - Invalid LLM JSON/schema. - Empty
cart. - User rejects readback. - User edits cart after readback. - Mock
checkout failure. - STT/TTS provider failure.

For each error: - Preserve a valid session state. - Do not claim an
operation succeeded unless the adapter confirms it. - Do not trigger
checkout as a recovery action. - Give a short, understandable spoken
response. - Log a redacted diagnostic event.

Real Swiggy error mapping and retry behavior must be implemented only
after the relevant official API contracts are verified.

------------------------------------------------------------------------

## 11. Persistence and observability

For the first MVP, in-memory session state is acceptable if the demo is
single-process and the limitation is documented.

Record useful events such as: - `stt_final` - `intent_extracted` -
`state_changed` - `product_search` - `cart_updated` -
`readback_generated` - `user_confirmation` - `mock_checkout` - `error`

Measure stage latency when practical. Do not delay the first end-to-end
demo to build a complete analytics system.

Never log: - API keys or access tokens. - Full phone numbers. - Full
delivery addresses. - Unnecessary raw personal conversation content.

------------------------------------------------------------------------

## 12. Testing strategy

### State machine

-   Test every supported state/event pair.
-   Test invalid transitions.
-   Test explicit yes, explicit no, and unclear confirmation.
-   Test cart edit after readback.
-   Test that only the current cart version can be confirmed.
-   Test that no LLM result can directly trigger checkout.

### Mock commerce

-   Search returns deterministic products.
-   Unknown product is handled.
-   Out-of-stock product triggers clarification.
-   Cart total is calculated from adapter-sourced prices.
-   Mock checkout succeeds and fails deterministically.

### End-to-end

-   Happy path: request → search → cart → readback → yes → mock order.
-   User says no at readback.
-   User changes quantity after readback.
-   Ambiguous item requires one clarification.
-   Invalid model output does not mutate cart.
-   Provider failure does not create a false success.

### Required repository checks

``` bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Run the commands supported by the actual repository. Report any
pre-existing failures separately.

------------------------------------------------------------------------

## 13. Demo acceptance checklist

The local MVP is demo-ready only when:

-   [ ] The project starts from documented commands.
-   [ ] One grocery request can be processed end to end.
-   [ ] Intent output is schema-validated.
-   [ ] Product/variant/price data comes from MockCommerce.
-   [ ] The cart is read back accurately.
-   [ ] A clear yes is required to proceed.
-   [ ] A no or unclear response does not place an order.
-   [ ] Editing the cart requires a fresh readback.
-   [ ] Mock checkout returns a simulated order result.
-   [ ] The UI/voice response clearly says this is a demo order.
-   [ ] Tests cover the safety-critical paths.
-   [ ] No secrets are committed.
-   [ ] The README accurately describes what works and what remains
    incomplete.

A real Swiggy order is not part of this acceptance checklist.

------------------------------------------------------------------------

## 14. Delivery sequence

Work in this order, adjusting only when repository inspection shows a
dependency:

1.  Inspect repository and current tests.
2.  Implement/fix domain models and deterministic state machine.
3.  Add state-machine tests.
4.  Verify or complete MockCommerce.
5.  Implement one text-driven end-to-end order flow.
6.  Add structured LLM extraction.
7.  Integrate Gnani Prisma STT.
8.  Integrate Gnani Timbre TTS.
9.  Record a stable local demo.
10. Apply for Swiggy staging/production access with the demo.
11. Integrate staging only after approval.

Do not spend the first evening on streaming, telephony, databases, or
production checkout. A small, correct, demonstrable flow is the goal.
