# Swiggy production access application

Working pack for the [Builders Club application](https://mcp.swiggy.com/builders/access/).
Update the status line after the test order. Say exactly what happened, including any
refusal, and keep this file free of tokens, addresses, and phone numbers.

## Form answers

| Field | Answer |
|---|---|
| Are you applying as | Individual Developer |
| Team / Project Name | Namma Saathi |
| MCP servers | Swiggy Food, Swiggy Instamart, Swiggy Dineout |
| Integration type | AI Agent / Copilot |
| Expected request volume | < 1K/day |
| Production redirect URI | None. This is a local-only application and has no deployed callback yet. |

Fill in personally: full name, email, GitHub or portfolio, LinkedIn, the terms
acknowledgement (only after reading the terms), and the video link.

### What are you building?

```text
Namma Saathi is a voice-first personal assistant that gets everyday tasks done for people speaking Kannada, Hindi, or English: ordering groceries from Instamart, ordering a meal from Swiggy Food, and reserving a table on Dineout. I'm building it as an individual developer, and it is also my entry in the Gnani AI Great Indian AI Internship Challenge.

Flow: the user says what they need, and Saathi picks the right service and asks a question if something is unclear. It reads back the exact items or booking, prices, fees, total, address, and payment, and acts only after an explicit yes.

Safety: the LLM only interprets language and has no access to Swiggy tools. Deterministic code controls carts, checkout, and bookings. Approval is tied to exactly what was read back, existing carts are preserved, and nothing is retried automatically. Unclear outcomes block further real actions until a person resolves them.

Current status: on 4 November 2026 I tested Instamart from localhost against the production server on my own account. Search, cart updates, and the spoken readback worked, and the total matched the Swiggy app. Checkout was disabled in the app, so no order has been placed. The test exposed a bug in cancelling a basket, which I fixed afterwards but have not yet re-tested live. For Food and Dineout I have used read tools only, and the full journeys run against built-in mock services.

Use and access: it runs on my machine for my own Swiggy account. There are no other users yet. I would like to place one small cash-on-delivery test order and book one free table. Please confirm whether I may do that, what approval this needs, and whether staging is required first. I would also like to know what agenticPaymentEligible=false means for a COD order.
```

### Tech stack and architecture

```text
Python 3 and FastAPI with a browser push-to-talk front end. Speech uses Gnani Prisma (STT) and Timbre (TTS). Intent extraction uses Gemini, with Groq as a fallback, and validates output against a strict schema. A pure state machine decides every cart and order action.

MCP: the official Python MCP SDK over Streamable HTTP to the Instamart server, with one long-lived session. OAuth 2.1 with PKCE and Dynamic Client Registration, with tokens in the OS keychain. Each server has its own allowlist of tools, covering search, cart, cash-on-delivery checkout, free table booking, order history, and tracking. Paid Dineout deals and UPI are not used.

Data: spoken requests and catalogue product names go to the LLM providers. The text read aloud, including cart contents and delivery address, goes to Gnani. Addresses and phone numbers are not sent to the LLM or written to logs. The local order journal stores only order IDs, totals, product names, hashes, and Swiggy's refusal reason if there is one.
```

## Also listed on the access page

The [access page](https://mcp.swiggy.com/builders/access/) lists these beyond the form.
Have them ready in case Swiggy follows up.

| Item | Answer |
|---|---|
| Static IP or gateway IP | None; a local machine on a home connection |
| Security contact | Your email |
| Data handling and privacy declaration | The data paragraph above; nothing is stored beyond the journal and logs described |
| Environment and infrastructure | One process on one machine; API accepts only local requests in live mode |
| Security audit, SOC 2, ISO 27001 | Optional; none |
| Traffic and scaling plan | Optional; one user, a few orders at most |

## Demo video

Public link, 1 to 2 minutes:

1. Show the running app and the commerce badge.
2. Do one request in Kannada or Hindi, including Saathi asking which one you want. See docs/demo-script.md.
3. Show the full readback, then say yes.
4. Show the result and say plainly whether it is the mock shop or the real test order.

Keep `.env`, tokens, your real address, phone number, and order details off screen.
