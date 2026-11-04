# ajji-mart

Voice agent that helps elderly users order groceries from Swiggy Instamart in Kannada / Hinglish
(Gnani Prisma STT → LLM → state machine → Swiggy MCP → Gnani Timbre TTS).

See `DESIGN.md` for the design and `AGENTS.md` for rules for coding agents.

```bash
cp .env.example .env    # keep ALLOW_REAL_CHECKOUT=false for local work
uv sync
uv run pytest
```

## Browser push-to-talk demo

The browser and API are served from one local FastAPI process. Configure the
existing Gemini/OpenRouter and Gnani credentials in `.env`, then start it with:

```bash
uv run uvicorn app.web:app --reload
```

Open <http://127.0.0.1:8000>. Choose Kannada, Hindi, or English, start a
conversation, and press **Speak** to record and **Finish** to upload. The page
keeps the conversation in this browser tab, shows the transcript and mock cart,
and plays Ajji's synthesized response. Confirm and cancel buttons send explicit
events through the existing deterministic state machine. Confirmation includes
the readback's cart version and hash; a stale page receives a fresh readback
instead of placing an order.

The browser encodes microphone PCM as WAV because Prisma's REST adapter accepts
WAV. Recordings stop at 30 seconds and uploads are limited to 12 MiB. Sessions
are in memory and expire after 30 minutes of inactivity or a server restart.
The language selected when a conversation starts is stored with that session
and configures Prisma STT, intent clarification instructions, deterministic
response phrases, and Timbre TTS for every turn. `GNANI_TTS_VOICE` selects a
multilingual Timbre v2.5 voice; it does not select the conversation language.
Use localhost or HTTPS for microphone access. This browser app refuses startup
unless commerce is mock-only and real checkout is disabled.

Run the mocked browser API tests with `uv run pytest -q tests/test_web.py`.

Run the Gemini text demo with a local key:

```bash
GEMINI_API_KEY=your-key uv run python -m app.text_demo
```

Run the turn-based Gnani voice demo with one or more initial audio files:

```bash
uv run python -m app.voice_demo --language hi-IN request.wav
```

The demo asks for another audio path after processing the initial files. It
keeps one conversation in memory and writes synthesized replies under
`voice-output/`. The `--language` option is required and is used for both STT
and TTS. The selected Timbre v2.5 voice remains the same across languages.

Prisma REST documents a maximum recording length of 30 seconds. The adapter
checks WAV duration locally and relies on Prisma to enforce the limit for other
documented formats.

Gemini requests have a 60-second per-attempt timeout and at most two total
attempts per candidate, with a short exponential backoff and jitter. The
extractor discovers compatible models at startup, prefers `GEMINI_MODEL`, and
tries at most two fallback candidates. Use `refresh_models()` to refresh the
process cache without restarting.

Gemini remains the primary intent provider. Gemini quota exhaustion
(`429 RESOURCE_EXHAUSTED`) immediately skips remaining Gemini models and
tries Groq with `GROQ_API_KEY` and `GROQ_MODEL` (default
`openai/gpt-oss-20b`). If Groq is unavailable or returns invalid output, the
extractor can use OpenRouter with `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` (default
`nvidia/nemotron-3-ultra-550b-a55b:free`). The fallback requests JSON using the
existing intent schema and validates it with Pydantic; neither fallback sends
tools or has cart or checkout capabilities. Groq uses the OpenAI-compatible
Chat Completions API with strict JSON schema output and retries without that
option only when the endpoint rejects it. OpenRouter does not use
`response_format`. The OpenRouter request follows its [Chat
Completions API](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request?explorer=true)
and [Nemotron model page](https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b%3Afree).
OpenRouter's free Nemotron endpoint may log requests and should not receive
confidential or personal data.

Example: enter `2 kg rice and 1 litre milk`, then answer the readback with
`yes`. Gemini extracts only the item names, quantities, and units; catalogue
matching, prices, cart readback, confirmation, and checkout are handled by
the deterministic runner and `MockInstamart`.

Status: the deterministic state machine, `MockInstamart`, and a text-turn
mock order runner are implemented and tested. `SwiggyInstamart` uses the
official Instamart MCP endpoint with OAuth 2.1 + PKCE and stores credentials in
the operating system credential store. Its read, cart, and checkout operations
are available behind an explicit local-development switch; checkout also
requires `ALLOW_REAL_CHECKOUT=true` and an explicit confirmation of the current
cart.

Gemini intent extraction is implemented with strict Pydantic validation and
mocked-response tests. Gnani STT/TTS, a web UI, and live Gemini calls are not
part of the automated test suite.

The safe local configuration is:

```dotenv
COMMERCE_BACKEND=mock
SWIGGY_LIVE_ACCESS=false
ALLOW_REAL_CHECKOUT=false
```

For read-only local account connection, set `COMMERCE_BACKEND=swiggy` and
`SWIGGY_LIVE_ACCESS=true`, then run `uv run python -m app.swiggy_auth`. Open the
printed Swiggy authorization URL, finish authentication, paste the redirected
`http://localhost` address-bar URL into the CLI, choose a saved address, and
review the product search, current cart, address, and total. This CLI never
places an order.

Never enable real checkout for automated tests or demos. For any later real
order, explicitly enable `ALLOW_REAL_CHECKOUT=true`, review the full cart,
delivery address, payment method, and total in the UI, then confirm the exact
readback. If checkout times out, reconcile with order status before attempting
anything again.
