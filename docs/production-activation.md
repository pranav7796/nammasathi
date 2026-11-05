# Production activation

What to do on the day Swiggy grants access. Everything here is configuration, not code, unless real
Swiggy behaves differently from its docs. Step 3 exists to find that out.

**Proven so far:** accounts, per-user OAuth, isolation, and all three journeys run through the real
adapters and the real MCP SDK, on SQLite and Postgres, against the stand-in at
`tests/fake_swiggy.py`. **Not proven:** anything about real Swiggy that the stand-in assumes. See
[What only Swiggy can prove](#what-only-swiggy-can-prove).

## Decisions and dependencies outside the code

Sort these out with Swiggy. They are not technical, and any of them can stop production.

| Item | Why | Source |
|---|---|---|
| **Which track are we on?** | A voice assistant serving many users is listed under the platform track, with an intro call, an architecture review, and a contract: "4+ weeks for enterprise-specific clauses". Ask whether an individual builder with few users can use the developer track, or a capped pilot. | [Enterprise](https://mcp.swiggy.com/builders/docs/start/enterprise/index.md), [Access](https://mcp.swiggy.com/builders/docs/operate/access.md) |
| **Signed data agreement** | Tool responses processed outside India need a signed DPA first. Today that covers Render (Singapore; it lists no India region) and the LLM providers, which receive catalogue names. Ask for the template. | [Data & compliance](https://mcp.swiggy.com/builders/docs/operate/data-and-compliance.md) |
| **Staging green for 48 hours** | Production access follows a working staging integration. | [Access](https://mcp.swiggy.com/builders/docs/operate/access.md) |
| **7 days' notice before a launch** | Swiggy asks for notice before a major traffic event. Traffic should ramp 1% → 10% → 50% → 100% over at least 24 hours. | [Access](https://mcp.swiggy.com/builders/docs/operate/access.md), [Ship to production](https://mcp.swiggy.com/builders/docs/build/ship-to-production.md) |
| **May real orders be placed?** | The owner's own decision for each first order and booking, as in [first-real-order.md](first-real-order.md). | AGENTS.md |

## Costs

Checked on 5 November 2026. Nothing here has been bought.

| Stage | Web server | Database | Cost |
|---|---|---|---|
| Staging | Render Free | Supabase (Mumbai available) or Neon (Singapore, its nearest) free plan | $0 |
| Production | Render Starter. Render says not to use Free for production: it sleeps after 15 minutes and may restart at any time. | A plan with backups, chosen by the owner | **$7/month** for the server, plus the database |

Do not use Render's free Postgres: it expires after 30 days and is deleted 14 days later, and the
order journal is what prevents duplicate orders. Render Free also blocks ports 25, 465, and 587, so
send email through a provider that offers another port, such as 2525 (`SMTP_PORT`).

## Step 1: Prepare the server (no Swiggy needed)

1. Create a Postgres database. Copy its URL, with `?sslmode=require` on the end.
2. Generate an encryption key, and store a copy somewhere safe. **Losing every copy disconnects
   every user.** Rotate by putting a new key first in the comma-separated list.

   ```bash
   uv run python -m app.accounts_admin generate-key
   ```

3. In Render, add `DATABASE_URL`, `TOKEN_ENCRYPTION_KEYS`, and `SUPPORT_EMAIL`. Accounts turn on and
   the site stays on mock shops. **This step is the launch for real people; it does not wait for
   Swiggy.** See [deployment.md](deployment.md#turning-on-accounts).
4. Optional: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`. Without them,
   use `accounts_admin verify` and `reset-link` by hand. **This SMTP code has only run against a
   fake mailer in tests, so send yourself a reset email to prove it.**
5. Check it from outside:

   ```bash
   uv run python scripts/verify_deployment.py https://YOUR-SERVICE.onrender.com
   ```

## Step 2: Turn on staging

Once Swiggy has allowlisted `https://YOUR-SERVICE.onrender.com/auth/callback`:

```
COMMERCE_BACKEND=swiggy
SWIGGY_LIVE_ACCESS=true
MULTI_USER_LIVE=true
SWIGGY_MCP_URL=https://mcp-staging.swiggy.com/im
DEMO_UNDERSTANDING=llm
```

**Confirm the staging path with Swiggy.** The docs give `mcp-staging.swiggy.com/{server}`, and `/im`
is our inference from production. Visitors without an account still get the labelled mock shops.

Run the following with the server's environment:

```bash
uv run python -m app.activation_check config
uv run python -m app.accounts_admin grant you@example.com     # after verifying your email
```

Sign in on the site and choose **Connect Swiggy**. The page shows "Swiggy test data" throughout.

## Step 3: Prove it against staging

```bash
uv run python -m app.activation_check reads --user you@example.com
```

It lists each server's tools and fails if any tool Saathi uses is missing, then makes read-only
calls. Then, with `ALLOW_SWIGGY_CART_WRITES=true`:

```bash
uv run python -m app.activation_check staging-journey --user you@example.com
```

It places a seeded order on Instamart and Food and books a table, then checks each. It refuses to run
anywhere but the staging address. Also do one full spoken journey per capability on the site, in
Kannada, Hindi, and English, with `ALLOW_REAL_CHECKOUT`, `MAX_REAL_ORDER_INR`, and
`ALLOW_REAL_BOOKING` set.

**If something fails here, it is a difference between Swiggy and its docs.** The adapters stop rather
than guess. Fix the adapter, and add the real shape to `tests/fake_swiggy.py` and
`docs/swiggy-integration-notes.md`.

Keep it running without errors for 48 hours, then email builders@swiggy.in.

## Step 4: Production

After Swiggy confirms, and the DPA is signed:

1. Move to a paid web plan and a database with backups. The owner approves the cost first.
2. Keep every real switch **off**, and set `SWIGGY_MCP_URL=https://mcp.swiggy.com/im`.
3. Allowlist the same callback in production if Swiggy treats it separately. Reconnect: staging and
   production tokens are stored apart and never mixed.
4. Run `activation_check config`, then `reads`.
5. **Owner only:** the first real order, following [first-real-order.md](first-real-order.md). Turn
   on `ALLOW_SWIGGY_CART_WRITES`, `ALLOW_REAL_CHECKOUT`, and `ALLOW_REAL_BOOKING`, with these limits:

   ```
   MAX_REAL_ORDER_INR=500
   REAL_ORDER_LIMIT=5
   REAL_ORDER_DAILY_LIMIT=2
   ```

   `REAL_ORDER_LIMIT` applies per person, for all time, so raise it as trust grows.
6. Roll out one person at a time with `accounts_admin grant`, and set `OPEN_REGISTRATION=false` if
   the list should stay closed.

## Running it

| Need | Command |
|---|---|
| Who has access | `uv run python -m app.accounts_admin list` |
| Remove someone at once | `uv run python -m app.accounts_admin revoke EMAIL` (their next turn ends) |
| Stop all real orders | Set `ALLOW_REAL_CHECKOUT=false` and `ALLOW_REAL_BOOKING=false`. The service restarts. |
| See an uncertain order | `uv run python -m app.order_admin --user EMAIL status` |
| Settle it, after checking the Swiggy app | `uv run python -m app.order_admin --user EMAIL resolve ID placed --order-id X` |
| Password reset without email | `uv run python -m app.accounts_admin reset-link EMAIL` |

Each `mcp_tool_call` log line has a correlation ID and a hashed user ID. Give Swiggy those IDs and
the time when reporting a failure. Security issues go to security@swiggy.in.

## Known limits

- **One server process.** Unfinished baskets, rate-limit counts, and daily allowances are in memory,
  so a restart ends an unfinished basket and resets the counts. Accounts, history, demo orders, and
  real orders are in the database. Do not scale past one instance yet. The
  order journal and token rows are safe across processes, but token refresh is locked per process.
- **The address limit is not trustworthy behind a proxy**, since `X-Forwarded-For` can be forged. The
  per-account limit still holds.
- **Registration shows whether an email is taken.** Sign-in and password reset do not.
- **A long-idle login expires.** Swiggy's first token lasts 5 days and an expired one cannot be
  refreshed, so someone idle that long sees **Connect Swiggy** again. Swiggy's docs say this is
  usually silent.
- **Uncertain orders are settled by the operator**, not the user.
- **No outside security review or penetration test has been done.** Swiggy's platform track prefers
  SOC 2, which this project does not have.
- The privacy notice was written from what the code does. It has not had legal review for DPDP.

## What only Swiggy can prove

| Assumption in the stand-in | Risk if wrong | Found by |
|---|---|---|
| Staging path is `/im`, and OAuth lives on the same host | Sign-in fails | Step 2 |
| `POST /auth/register` accepts our redirect | Sign-in fails, page says so | Step 2 |
| Token exchange works form-encoded, as the SDK sends it on localhost. The docs show JSON. | Sign-in fails | Step 2 |
| Refresh works as a JSON body, per the docs. Never run live. | Reconnect prompts every 5 days | Set a short-lived token, or wait |
| 401 and 419 arrive as HTTP statuses | A slower failure | Step 3 |
| Shapes of `update_food_cart`, `place_food_order`, `book_table`, `get_booking_status`, and `checkout`. Never run live. | The adapter stops rather than guesses | Step 3 |
| Seeded staging data has milk, biryani, and Italian restaurants | The journey reports nothing to order; change the search words | Step 3 |
