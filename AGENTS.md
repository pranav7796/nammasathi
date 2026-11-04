# AGENTS.md --- Ajji Mart

## 1. Project mission

Build Ajji Mart: a voice-first grocery-ordering assistant for elderly
users. The target interaction is Kannada, Hindi, and Hinglish. The
intended pipeline is:

**Gnani Prisma STT → LLM intent extraction → deterministic state machine
→ commerce adapter → Gnani Timbre TTS**

The project is being built for the Gnani AI Great Indian AI Internship
Challenge. The stated submission deadline is **10 November 2026, 11:59
PM IST**.

The immediate milestone is a reliable, demonstrable local MVP. Do not
confuse a working prototype with authorization to place real orders.

## 2. Read before working

Read these files before planning or editing:

1.  `DESIGN.md` --- product scope, state machine, safety invariants,
    architecture, and acceptance criteria.
2.  `README.md` --- setup and current project status.
3.  `pyproject.toml` --- dependencies, Python version, linting, typing,
    and test configuration.
4.  Any existing Swiggy or Gnani setup notes in the repository, if
    present.

If a referenced file does not exist, report that fact. Do not invent its
contents.

## 3. Source-of-truth rules

-   Inspect the actual repository before proposing changes.
-   Existing code and tests are evidence of implementation status;
    documentation claims are not proof that a feature works.
-   Do not invent API endpoints, headers, OAuth scopes, MCP tool names,
    request fields, response fields, or error codes.
-   For Swiggy, use the official documentation:
    https://mcp.swiggy.com/builders/docs/ and its linked reference
    pages.
-   For Gnani, use https://docs.gnani.ai/ and the relevant API
    reference.
-   Record the exact documentation page supporting any integration
    contract in code comments or a project setup note.
-   If documentation or access is insufficient, isolate the unknown
    behind an interface and report the blocker. Do not guess.

## 4. Current delivery objective

Build and verify one complete local order journey:

1.  User supplies a grocery request (typed transcript is acceptable for
    the first backend milestone).
2.  Gnani Prisma converts audio to text when the speech integration
    milestone is reached.
3.  An LLM extracts a validated structured intent and item list.
4.  Deterministic application code validates and updates the
    session/cart.
5.  A commerce adapter searches the local mock catalogue and updates a
    mock cart.
6.  The application reads back exact products, variants, quantities,
    pack sizes, prices, total, address/payment assumptions.
7.  The user explicitly confirms the current cart.
8.  The mock adapter completes the simulated order.
9.  Gnani Timbre speaks the response when the TTS milestone is reached.

The first end-to-end demo must not require Swiggy staging or production
checkout.

## 5. Commerce environments and safety

Support these logical configurations:

-   `mock`: local catalogue, cart, and simulated checkout. **Default.**
    Used for development, demos, and automated tests.
-   `swiggy_staging`: only after Swiggy has granted staging access and
    supplied/approved the required OAuth configuration.
-   `swiggy_prod`: only for the exact operations permitted by the user's
    Swiggy access and configuration.

Important distinctions:

-   Running the application on `localhost` does not itself make an
    upstream service a sandbox.
-   Permission to build locally is not permission to place real orders.
-   Do not infer staging access from local-development permission.
-   Do not switch environments or endpoints without documenting the
    change and asking Pranav when authorization is unclear.
-   Never use production checkout for development, tests, demos, or
    failure injection.
-   Keep `ALLOW_REAL_CHECKOUT=false` by default. Do not change it,
    enable it, or bypass it.
-   If production read-only access is later configured, explicitly
    disable checkout in the adapter as well as through configuration. Do
    not rely on a UI or prompt to enforce this.
-   Staging checkout may be used only after staging access and the exact
    staging endpoint have been verified. Until then, use mock checkout.
-   Any future real production order requires a separate, explicit
    decision by Pranav and a fresh confirmation of the exact cart,
    total, delivery address, and payment method. This is outside the
    current MVP.

Never blindly retry a checkout after a timeout, connection loss, or
ambiguous response. Reconcile order status first. Never assume a
cancellation capability exists.

## 6. Non-negotiable application invariants

1.  **The LLM cannot place orders.** It has no checkout tool, no direct
    commerce credentials, and no ability to emit executable side
    effects.
2.  **The state machine owns every transition.** Only deterministic
    application code may emit commerce effects.
3.  **Checkout is gated.** For the current MVP, checkout is mock-only.
    In any later authorized environment, checkout can only be emitted
    after explicit confirmation of the exact current cart.
4.  **Cart edits invalidate confirmation.** Any item, quantity, variant,
    price, address, or payment change requires a fresh readback and
    confirmation.
5.  **Never silently substitute.** Ask before choosing a substitute or a
    larger pack.
6.  **Prices and product identifiers come from commerce data**, never
    from LLM-generated text.
7.  **Never blindly retry checkout.** A timeout can mean the order
    succeeded upstream.
8.  **No secrets in source control, logs, prompts, screenshots, or
    generated artifacts.**
9.  **Minimize personal data.** Do not log phone numbers or full
    addresses in plaintext. Do not send unnecessary personal data to the
    LLM.
10. **Fail closed.** Invalid model output, unknown state, missing cart
    data, unknown environment, or failed validation must not trigger a
    commerce side effect.

## 7. State machine implementation rules

-   Keep `app/orchestrator/state_machine.py` pure: no network,
    filesystem, database, clock, randomness, environment access, or
    logging.
-   It should accept explicit current state/session data and an event,
    then return a new state/session and a list of typed effects.
-   Runtime code executes effects after the transition has been
    validated.
-   Keep state transitions explicit and exhaustive. Unknown events must
    be rejected safely.
-   Use typed domain models and enums; avoid unstructured dictionaries
    where a model is practical.
-   Keep cart identity/version or a deterministic cart hash so
    confirmation is bound to the exact cart.
-   Never interpret an unclear response as confirmation.
-   Keep language-specific spoken phrases outside core transition logic
    where practical.

## 8. LLM contract

The LLM may: - Extract user intent, item names, quantities, units, and
clarification needs into a schema. - Classify a response as
yes/no/unclear if deterministic phrase matching is insufficient. -
Phrase a response using application-provided facts and templates.

The LLM may not: - Search or mutate the cart directly. - Select product
IDs, variants, or prices without adapter evidence. - Decide that
checkout is authorized. - Call checkout, confirm an order, retry
checkout, or change environment settings. - Invent availability, pack
sizes, delivery fees, totals, addresses, or order IDs.

Validate every model response against a strict schema. On invalid
output, retry at most once if safe; otherwise ask the user to repeat or
clarify.

## 9. Adapter boundaries

Keep external API payloads inside their adapter modules.

Suggested interfaces: - `CommerceAdapter`: search products, get/update
cart, and (for mock/staging only when configured) checkout. -
`SpeechToText`: transcribe an audio turn. - `TextToSpeech`: synthesize a
response. - `IntentExtractor`: convert utterance and context into a
validated intent.

Do not leak Swiggy MCP or Gnani-specific JSON shapes into the state
machine or domain models.

Use `MockCommerce` for local end-to-end work. It should have a small
deterministic catalogue and support controlled failure scenarios.

## 10. Incremental implementation workflow

For each milestone:

1.  Inspect the relevant code and tests.
2.  State the goal, files expected to change, design invariants
    involved, and test plan.
3.  Implement the smallest coherent vertical slice.
4.  Run focused tests, then the full required checks when appropriate.
5.  Inspect the actual diff.
6.  Explain:
    -   what changed;
    -   why it changed;
    -   which invariant it protects;
    -   how failures are handled;
    -   what remains incomplete.
7.  Stop if an external API contract or permission is unknown. Report
    the precise blocker instead of fabricating a workaround.

Do not modify unrelated files. Do not rewrite working code just to match
a preferred style.

## 11. Required checks

Use Python 3.12 and the repository's `uv` workflow.

``` bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

If a command fails: - include the exact command and relevant error; -
determine whether the failure is pre-existing or caused by the change; -
fix the cause rather than suppressing the check; - never weaken or
delete tests merely to make the suite pass.

## 12. Agent roles

Choose the relevant role for each task:

### Planner

-   Inspect first.
-   Produce a short plan with files, invariants, tests, risks, and
    dependencies.
-   Do not edit until the user approves the plan when the user
    explicitly requests plan-first work.

### Implementer

-   Make the smallest coherent change.
-   Follow existing project conventions and interfaces.
-   Do not add speculative features or dependencies.

### Tester

-   Add or update tests for behavior and failure cases.
-   Prefer deterministic unit tests and mock adapters.
-   Never call real checkout from tests.

### Reviewer

-   Inspect the diff for safety, correctness, unnecessary complexity,
    and consistency with `DESIGN.md`.
-   Check that cart changes invalidate confirmation and that the LLM
    cannot trigger side effects.

## 13. Repository layout

The current `pyproject.toml` configures Pyright to include `app` and
`tests`. Therefore use **`app/` as the canonical application package for
this repository**, unless the user explicitly approves a deliberate
migration.

Do not introduce a parallel `src/ajji/` tree. If existing code
contradicts this, report the discrepancy and propose the smallest safe
resolution before moving files.
You have access to Swiggy Builders Club docs - the authoritative source
for Swiggy MCP (Food, Instamart, Dineout, Scenes). Always consult these before
writing Swiggy code:
 
- Index:      https://mcp.swiggy.com/builders/llms.txt
- Full text:  https://mcp.swiggy.com/builders/llms-full.txt
- Per-page:   append `.md` to any https://mcp.swiggy.com/builders/docs/... URL
 
Tool schemas live under `/docs/reference/{food,instamart,dineout,scenes}`.
Error codes live at `/docs/reference/errors`. Auth flow is at
`/docs/start/authenticate`.
 
Rules:
1. Before recommending a tool name, parameter, error code, rate limit,
   or auth flow, fetch the relevant doc and verify.
2. Never invent tool names or parameters. If the docs don't cover it,
   say so and ask.
3. Prefer `.md` page fetches over `llms-full.txt` when you know the
   exact area - it's cheaper on context.
 
Smoke test: fetch llms.txt and tell me how many tools the Food server
exposes. (Answer: 20.)