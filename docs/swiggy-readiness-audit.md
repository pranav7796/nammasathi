# Swiggy readiness audit

Audited on 6 November 2026 at commit `1d7c49a` on `feat/swiggy-application-readiness`, then
improved in the next commit. **Nothing here has run against real Swiggy staging or production.**
"Tested" means against `tests/fake_swiggy.py`, a stand-in built from Swiggy's published contracts,
through the real adapters and the real MCP SDK. That proves our side only.

Key: **Done** built and tested on the stand-in. **Partial** works with stated limits. **Missing**
not built. **Needs Swiggy** cannot be finished or proven without access.

## A. What is implemented

### 1. Swiggy authentication

| Check | Status | Where, and how it was tested |
|---|---|---|
| OAuth 2.1 authorization code flow, per user | Done | `app/oauth_web.py`, per [delegated auth](https://mcp.swiggy.com/builders/docs/start/enterprise/delegated-auth.md). Browser test connects two users. |
| Dynamic Client Registration | Done | `POST /auth/register` once per redirect URI; client ID kept in the database across restarts (tested). |
| PKCE | Done | 64-character random verifier, S256 challenge, sent only at token exchange. The stand-in checks the challenge. |
| Redirect URI | Done | Exactly `PUBLIC_BASE_URL + /auth/callback`. Today: `https://namma-saathi-7b62.onrender.com/auth/callback`. |
| State and CSRF | Done | Random state, single use, 10 minutes, bound to an HttpOnly browser cookie **and** to the signed-in account. Tested: forged state refused; a callback in another account refused. Cross-site POSTs refused by origin check. |
| Connect and disconnect | Done | Account panel. Disconnect calls Swiggy's `/auth/logout` and drops the token (tested). |
| Tokens encrypted and isolated | Done | AES-256-GCM, bound to user and issuer, so a copied row cannot be read (tested). Keys rotate. Staging and production tokens are stored apart. |
| Expiry and refresh | Done | Refreshed within 60 s of expiry and once after a 401, with rotation saved first. **New:** refreshed within two days of expiry when the person visits, because an expired token cannot be refreshed. |
| Revocation and reconnection | Done | 401 after a refresh, or 419, drops the token; the page asks the person to connect again. Tested: nothing is placed, and no uncertain order is left. |
| Passwords and OTPs | Done | Never seen: the person signs in on Swiggy's own page. |

**Unproven against Swiggy:** the token exchange is form-encoded (what the SDK sent on localhost, where
it worked), while the docs show JSON; refresh is JSON per the docs and has never run live.

### 2. MCP integration

| Check | Status | Notes |
|---|---|---|
| Endpoints | Done | `https://mcp.swiggy.com/{im,food,dineout}`. Staging `https://mcp-staging.swiggy.com/im` is **our inference** from the docs' `{server}` pattern. Nothing else is accepted. |
| Initialization and sessions | Done | One long-lived Streamable HTTP session per server per user, opened when first needed (so servers start one after another, as the rate-limit page asks), closed after 120 s idle. |
| Tool discovery | Done (**new**) | Each session reads `tools/list` (all pages). A tool the server does not offer that account is never sent, and an order that was never sent is recorded as not placed. `activation_check reads` also reports any missing tool by name. |
| Tool permissions | Done | Each adapter calls only an allowlist. Write tools also need their own switch. |
| Forbidden responses | Done (**new**) | HTTP 403 or JSON-RPC -32003 is "not attempted": safe even for checkout, and it leaves no uncertain order. |
| 401, 429, 5xx, timeouts | Done | 429 honours `Retry-After` at the HTTP layer (the SDK hides it). Reads retry; order placement never blind-retries. |
| Correlation IDs and hashed user IDs in logs | Done | Every `mcp_tool_call` log line. |

**Instamart:** search, your go-to items, cart replace and clear, cash-on-delivery checkout, order
history, tracking. **Food:** dish search, cart, cash-on-delivery order, order history, tracking.
**Dineout:** restaurant search, free slots, free booking, booking status.

### 3. Transactions

| Check | Status |
|---|---|
| Search products, dishes, restaurants | Done |
| Add, change, remove items by voice or text | Done (mock and stand-in; cart writes proven live once on Instamart from localhost) |
| Total, fees, savings, delivery address, payment read back | Done |
| Explicit yes, bound to the exact readback the browser received | Done |
| An uncertain checkout reconciled against order history before anything else | Done; one person's uncertain order blocks only that person |
| Order verified in Swiggy's order history, then by tracking | Done for Instamart and Food. Dineout uses `get_booking_status`. |
| Limits: total, per person ever, per person per day | Done |

### 4. Experience

| Check | Status |
|---|---|
| Kannada, Hindi, English | Done. Real Gnani speech tested in all three on demo shops. Phrases need a native speaker's review. |
| Voice and typing in the same conversation | Done |
| Natural requests and clarifying questions | Done for packs, quantities, choices, restaurants, and slots. Needs an LLM; rules otherwise. |
| Connection status, active tasks, order history | Done: account panel, tasks bar, real orders, demo orders, history. |

### 5. Security

Done and tested: per-user isolation of tokens, carts, journals, conversations, history, and limits;
scrypt passwords; HttpOnly, Secure, SameSite cookies; cross-site write refusal; CSP, HSTS, and other
headers; sign-in and sign-up limits; no tokens, emails, addresses, or passwords in logs (tested);
a planted session ended at sign-in; request size cap.

Limits, not fixed: rate limits are in memory, for one process; `X-Forwarded-For` can be forged, so
the per-address limit is weaker than the per-account one; registration reveals whether an email is
taken; no outside security review.

### 6. Deployment findings

| Finding | Evidence | Fix |
|---|---|---|
| **Gnani serves one request at a time per key.** A second simultaneous request gets 429. This is why two of three live replies failed. | Real key, 6 November: 3 at once gave 1 success and 2 instant 429s; one after another, all succeeded. Recognition and synthesis share the limit. | **Fixed:** one shared slot for all Gnani calls, and a brief retry of 429. Real key afterwards: 5 replies at once all succeeded, the last after 3.6 s. More capacity needs Gnani (`GNANI_MAX_CONCURRENT`). |
| **The live site's turns were understood by rules after about 10 s.** | 5 November live probe. Locally, Groq answered in about 1 s; Gemini returned "503 high demand" on 6 November. | Render's settings are not visible from here. Likely causes: no `GROQ_API_KEY` on Render, or Groq's free tier rate-limiting a shared key, leaving overloaded Gemini. **Fixed in code:** a failed provider rests for 30 s, so later turns skip straight to one that works instead of waiting 10 s. **New log line at start:** `{"event": "providers", "op": "groq,gemini,scripted", ...}`, names only, shows in Render's logs what the server will use. |
| Render Free sleeps and restarts | Render docs | Unfinished baskets are lost; everything else is in Neon. Paid plan $7/month for production. |
| Neon | Tested pooled and direct, on real Neon | Works. Production uses `main`; `staging` branch exists. |

## B. Incomplete, and can be built without Swiggy

| Item | Why it matters |
|---|---|
| Unfinished baskets survive a restart | Render Free restarts often. Needs the state machines saved to the database. |
| Dineout for any location | Only places with known coordinates (Bengaluru areas, Mumbai, Delhi). Needs geocoding or `get_saved_locations`. |
| Food add-ons, multi-group options, restaurant menus | Dishes needing them are left out. |
| Real email sending | Code exists, never sent a real email. |
| Rate limits across processes | Needed only past one server. |
| Native-speaker review of Kannada and Hindi phrasing | Quality. |

## C. Needs Swiggy

- Allowlisting the redirect URI, at staging and then production.
- Confirming the staging address and the OAuth issuer for staging.
- Every real response shape: token exchange format, refresh, and the write tools
  `update_food_cart`, `place_food_order`, `book_table`, `get_booking_status`, Instamart `checkout`.
- 48 hours of working staging, then production access.
- Which track applies (developer, or platform operator with a contract), and a data agreement if
  data is processed outside India (Render is in Singapore; LLMs see item names).
- Permission to place the first real order and booking, by the owner, per
  [first-real-order.md](first-real-order.md).
- Coupons and UPI: Swiggy gates coupons per account; UPI is deliberately not built.

## D. Configuration after approval

Already set (owner reports): `DATABASE_URL`, `TOKEN_ENCRYPTION_KEYS`, `SUPPORT_EMAIL`.

| Variable | Staging | Production | Purpose |
|---|---|---|---|
| `COMMERCE_BACKEND` | `swiggy` | `swiggy` | Server may use Swiggy |
| `SWIGGY_LIVE_ACCESS` | `true` | `true` | Required with the above |
| `MULTI_USER_LIVE` | `true` | `true` | Per-person connections; others keep demo shops |
| `SWIGGY_MCP_URL` | `https://mcp-staging.swiggy.com/im` (confirm) | `https://mcp.swiggy.com/im` | Server address |
| `DEMO_UNDERSTANDING` | `llm` | `llm` | Rules-only understanding is mock-only |
| `GROQ_API_KEY`, `GEMINI_API_KEY` | set | set | At least one; check the `providers` log line |
| `GNANI_API_KEY` | set | set | Speech |
| `ALLOW_SWIGGY_CART_WRITES` | `true` | `false`, then owner | Change carts |
| `ALLOW_REAL_CHECKOUT` | `true` | `false`, then owner | Place orders |
| `ALLOW_REAL_BOOKING` | `true` | `false`, then owner | Book tables |
| `MAX_REAL_ORDER_INR` | `500` | `500` | Required with checkout |
| `REAL_ORDER_LIMIT` | `50` | `5` | Per person, ever |
| `REAL_ORDER_DAILY_LIMIT` | `10` | `2` | Per person, per day |
| `OPEN_REGISTRATION` | `true` | `true` or `false` | Close for an invite-only pilot |
| `GNANI_MAX_CONCURRENT` | `1` | what Gnani grants | Speech requests at once |

Then per person: `uv run python -m app.accounts_admin verify EMAIL` and `grant EMAIL`.

## E. Plan with acceptance criteria

| Step | Done when |
|---|---|
| 1. Merge and turn on accounts | `scripts/verify_multi_user.py https://…` passes on the live site, with `--voice` |
| 2. Fix language understanding on Render | The `providers` log line lists `groq`; a turn's `understood_by` is `groq` in under 3 s |
| 3. Swiggy allowlists the callback | `/auth/login` on staging redirects to Swiggy's page, not our 503 |
| 4. Staging switched on | `activation_check config` all pass |
| 5. Staging reads | `activation_check reads --user you` all pass, no missing tools |
| 6. Staging journey | `activation_check staging-journey` places one seeded order per service and books one table, each verified |
| 7. Spoken staging journeys | All three, in all three languages, on the site |
| 8. 48 hours clean | No `login_lost`, `not_offered`, or uncaught errors in the logs |
| 9. Production read-only | `activation_check reads` on production passes |
| 10. First real order | The owner places it, per first-real-order.md, and it is verified in the Swiggy app |

## F. Implemented and tested in this round

| Change | Test |
|---|---|
| Gnani: one shared slot and busy retry | Unit tests; real Gnani: 5 replies and 3 recognitions at once all succeed |
| LLM provider rests after failing; startup log of providers | Unit tests |
| Tool discovery; never send an unoffered tool | Stand-in: an order is never sent and is recorded as not placed |
| 403 and -32003 are "not attempted" | Stand-in, both forms; refused checkout leaves nothing uncertain |
| Login kept fresh for regular visitors | Stand-in: refreshed within two days, left alone otherwise |

Totals: 895 Python tests on Postgres, 17 frontend tests, 29 browser checks, all passing.

## G. How fast activation can be

| Phase | Our work | Waiting on |
|---|---|---|
| Swiggy allowlists staging | 15 minutes: set the staging variables, grant yourself | Swiggy |
| Staging checks (steps 4 to 7) | 1 to 2 hours if Swiggy matches its docs | Nobody |
| Fixing a mismatch | Hours to a day per difference, most likely in write-tool responses | Nobody |
| Clean staging | Monitoring only | 48 hours, Swiggy's rule |
| Production | 30 minutes of configuration, then the owner's first order | Swiggy's confirmation; a data agreement and track decision may come first |

So the engineering on the day of approval is a few hours, unless Swiggy's live responses differ from
its docs. The calendar is at least three days, set by Swiggy's 48-hour staging rule, plus however long
Swiggy takes for production, and any contract or data agreement it asks for.
