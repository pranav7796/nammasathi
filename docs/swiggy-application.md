# Swiggy production access application

Working pack for the [Builders Club application](https://mcp.swiggy.com/builders/access/).
Update the status line after the test order. Say exactly what happened, including any
refusal, and keep this file free of tokens, addresses, and phone numbers.

## Form answers

Check every answer against the current state before submitting. Fill in these yourself: full name,
email, LinkedIn, the terms acknowledgement (only after reading the terms), and the **Legal Details**
section. That section asks for your government-ID name, date of birth, PAN, address, business
category, and agreement email, and Swiggy sends an agreement to e-sign.

| Field | Answer |
|---|---|
| Are you applying as | Individual Developer |
| Team / Project Name | Namma Saathi |
| GitHub or Portfolio URL | The public repository URL, once published |
| Which MCP servers | Swiggy Food, Swiggy Instamart, Swiggy Dineout |
| Integration type | AI Agent / Copilot |
| Production Redirect URI | The URI printed by `scripts/verify_deployment.py`. Never guess it |
| Expected request volume | < 1K/day |
| Demo video link | The link, after the upload checklist in `docs/demo-script.md` |

### What are you building?

```text
Namma Saathi is a voice-first personal assistant for everyday tasks, for people who would rather speak than navigate an app, including older family members. It understands Kannada, Hindi, and English. One assistant orders groceries from Instamart, orders a meal from Swiggy Food, and reserves a table on Dineout. I'm building it as an individual developer, and it is also my entry in the Gnani AI Great Indian AI Internship Challenge.

Flow: the user says what they need, and Saathi picks the right service and asks if something is unclear. It reads back the exact items or booking, prices, fees, total, address, and payment, and acts only after an explicit yes. If the user switches tasks, for example from groceries to a table, the first task waits, and a yes only applies to what was just read back.

Safety: the LLM only interprets language and has no access to Swiggy tools. Deterministic code controls carts, checkout, and bookings. Approval is tied to exactly what was read back, existing carts are kept, and checkout is never retried automatically. An unclear outcome blocks further real actions until a person resolves it.

Current status: all three journeys work end to end on built-in mock shops, and that is what the video shows, labelled as mock. With real speech and a real LLM, I have run groceries and table booking in all three languages and the meal journey in English. Against the real Swiggy servers from localhost, on my own account, I have used Instamart search and order history, Food search and order history, and Dineout search and free slots. On 4 November 2026, one Instamart rehearsal updated my real cart and read back a total that matched the Swiggy app. No real order or booking has been placed, and the Food cart, Food ordering, table booking, and Instamart checkout paths have not yet run against Swiggy.

Access: I'm asking for staging access first, using the redirect URI in this form. The deployed site serves the mock demo and the OAuth callback; connecting a real user's account there is what I will build and test on staging. Please also tell me what agenticPaymentEligible=false means for a cash-on-delivery order.
```

### Tech stack and architecture

```text
Python 3.12 and FastAPI with a browser push-to-talk page, deployed in Docker over HTTPS. Speech uses Gnani Prisma (STT) and Timbre (TTS). Intent extraction uses Groq first and Gemini second, each with a short deadline, validated against a strict schema. Pure state machines decide every cart, order, and booking action; the LLM cannot trigger any of them.

MCP: the official Python MCP SDK over Streamable HTTP to the Instamart, Food, and Dineout servers, with one long-lived session per server and a tool allowlist per server. OAuth 2.1 with PKCE and Dynamic Client Registration, with state checked at the callback. 401 and 429 have explicit handling, and an uncertain checkout is reconciled against order history before anything else can happen, never blindly retried. Cash on delivery and free table reservations only; no UPI or paid deals.

Data: spoken requests and catalogue names go to the LLM providers. The text read aloud, including cart contents and delivery address, goes to Gnani. Addresses and phone numbers are not sent to the LLM or written to logs. Hosted tokens stay in memory only, and the local order journal stores only order IDs, totals, product names, hashes, and refusal reasons.
```

## Also listed on the access page

The [access page](https://mcp.swiggy.com/builders/access/) lists these beyond the form.
Have them ready in case Swiggy follows up.

| Item | Answer |
|---|---|
| Static IP or gateway IP | None. The hosted demo sends no Swiggy tool calls; live use is from a local machine on a home connection |
| Security contact | Your email |
| Data handling and privacy declaration | The data paragraph above; nothing is stored beyond the journal and logs described |
| Environment and infrastructure | Hosted: one Docker service on Render, mock shops only. Live: one process on my machine, accepting only local requests |
| Security audit, SOC 2, ISO 27001 | Optional; none |
| Traffic and scaling plan | Optional; a handful of test users, a few orders at most, and a daily cap on the public demo |

## Demo video

See the script and both checklists in [demo-script.md](demo-script.md).
