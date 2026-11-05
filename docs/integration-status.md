# Integration status

Checked on 5 November 2026. **No real order or booking has ever been placed.** Every journey below
runs end to end on mock shops, in English, Hindi, and Kannada, with real Gnani speech and a real
LLM (`uv run python -m app.demo_check --voice`).

Key: **Mock** works only against the built-in shops. **Read** verified against real Swiggy,
read-only. **Write** verified against real Swiggy with a real change. **Blocked** needs Swiggy.

| Step | Instamart | Food | Dineout |
|---|---|---|---|
| Authentication (localhost OAuth, PKCE, DCR) | Read | Read | Read |
| Service discovery (tool list) | Read: 17 tools | Read: 20 tools | Read: 11 tools |
| Search | Read | Read | Read |
| Selection and clarification | Read | Read | Read |
| Cart or slot management | **Write**: one rehearsal | Mock; cart write refused by our own lock | Read: free slots |
| Readback | **Write**: total matched the Swiggy app | Mock | Read: from live slots |
| Explicit confirmation | Mock; live yes never given | Mock | Read: the yes stopped at our own lock |
| Execution (order or booking) | **Blocked** | **Blocked** | **Blocked** |
| Authoritative verification | Read: order history; order check Mock | Read: order history; order check Mock | Mock; there is no booking history |
| Failure handling | Mock, plus one live failure handled | Mock | Mock |

## Why execution is blocked

Swiggy's access page says developers get staging through allowlisting during review, and production
after staging has been working for 48 hours. It does not say whether a personal localhost login may
place orders. So real execution waits for Swiggy's answer and for the owner's approval of the exact
order. `ALLOW_REAL_CHECKOUT` and `ALLOW_REAL_BOOKING` stay off until then.

## Still unproven

- The live write paths `update_food_cart`, `place_food_order`, `book_table`, and Instamart
  `checkout` have never run, so they may differ from the docs. Dineout's search and slots already
  did. The code stops rather than guesses when a response does not match.
- The fixed Instamart cancel, against the live cart.
- The hosted Swiggy sign-in, against real Swiggy. It is tested against a stand-in server only.
