# Deployment

The public deployment has three levels, each set by configuration:

| Level | Adds | Reaches Swiggy? |
|---|---|---|
| Demo | Mock shops for anyone, no sign-up | No |
| Accounts | `DATABASE_URL` and `TOKEN_ENCRYPTION_KEYS`: sign-up, sign-in, account management | No |
| Live | `MULTI_USER_LIVE`, after Swiggy grants access: approved users connect their own Swiggy | Yes, per user |

**Accounts need nothing from Swiggy.** With a database and a key, people can sign up, sign in, and use
the three demo journeys in three languages, with their own history, demo orders, and language kept.
This can run indefinitely while Swiggy access is pending.

`PUBLIC_DEPLOYMENT=true` refuses to start with anything live unless `MULTI_USER_LIVE` is on, along
with Postgres over TLS, an encryption key, and an HTTPS address. Visitors who are not signed in always
get the labelled mock shops. To go further than Demo, follow
[production-activation.md](production-activation.md).

## Host

Render's free plan, from `render.yaml`: one Docker web service with HTTPS on an `onrender.com`
address. **It costs nothing and needs no domain.** Render says its free services sleep after 15
minutes without traffic, so the first visit after a sleep can take about a minute. Open the page
before anyone needs it. Measured in Docker: 92 MB of memory against the free plan's 512 MB.

## Steps

1. Sign in at <https://dashboard.render.com> with GitHub. If Render asks for a card or shows any
   price, stop and decide; the free plan should not need one.
2. **New > Blueprint**, choose the public repository, and approve the `namma-saathi` service.
3. Enter `GNANI_API_KEY`, `GROQ_API_KEY`, and `GEMINI_API_KEY` when asked. Never commit them.
4. When the deploy is live, run:

   ```bash
   uv run python scripts/verify_deployment.py https://YOUR-SERVICE.onrender.com
   ```

5. The script prints the exact redirect URI for the application. If the name `namma-saathi` was
   taken, Render adds a suffix, so copy the address Render shows rather than assuming it.

## Turning on accounts

1. A Postgres database. Neon's free plan in AWS Singapore is what this project uses. Use the
   connection string from Neon's **Connect** dialog, pooled or direct; both are tested.
2. In Render, set `DATABASE_URL`, `TOKEN_ENCRYPTION_KEYS` (one line from
   `python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`), and
   `SUPPORT_EMAIL`. Keep a copy of the key.
3. Deploy, then check every requirement from outside:

   ```bash
   uv run python scripts/verify_multi_user.py https://YOUR-SERVICE.onrender.com
   ```

**If the database is down,** the site still starts and the anonymous demo still works. The page says
accounts are unavailable, `/readyz` answers 503, and accounts come back by themselves when the
database does. `/healthz`, which Render uses, stays green, so Render does not restart the service.

**What does not survive a restart:** an unfinished basket. Render Free restarts a service after 15
minutes without visitors. Accounts, history, finished demo orders, and languages are in the database.

## A separate staging service

Use a Neon branch and a second Render service, so tests never touch real users' data:

1. Neon already has a `staging` branch in the `namma-saathi` project. Copy its connection string
   from **Connect**, choosing the `staging` branch.
2. In Render, **New > Web Service** from the same repository, branch
   `feat/swiggy-application-readiness`, Docker, Free plan, region Singapore.
3. Set the same variables as production, with the staging database and its **own** encryption
   key, plus `PUBLIC_DEPLOYMENT=true` and `DEMO_UNDERSTANDING=fallback`.
4. Run `scripts/verify_multi_user.py` against it.

Its address will differ from production, so Swiggy would need it allowlisted separately before it
can test live sign-in. Until then it is a place to try changes before they reach `main`.

## What a visitor can spend

Each turn uses real speech and LLM quota. Visitors without an account share
`PUBLIC_DAILY_TURN_LIMIT` (default 300) and `PUBLIC_MAX_SESSIONS` (default 50) open conversations.
Each signed-in person has `USER_DAILY_TURN_LIMIT` (default 200) and one conversation at a time, so a
busy anonymous demo cannot lock them out. `GLOBAL_DAILY_TURN_LIMIT` (default 5000) caps everyone
together, which also bounds anyone making extra accounts. Sign-ups are limited to 20 per address
and 300 for the whole site per hour. OpenRouter's
free model may log requests, so a public deployment never uses it. Put no `OPENROUTER_API_KEY` in
Render either way.

## The sign-in callback

| Route | Purpose |
|---|---|
| `GET /auth/login` | Registers with Swiggy (DCR), then redirects with PKCE and a one-use state |
| `GET /auth/callback` | Checks the state and browser cookie, then exchanges the code |
| `GET /auth/status` | Whether this browser is connected |
| `POST /auth/logout` | Forgets the token here and logs out at Swiggy |

The server keeps no access log, because the callback's address holds a one-time code. Without a
database, tokens stay in memory and nothing uses them. With `MULTI_USER_LIVE`, only a signed-in,
approved person can start this flow, and the token is encrypted and stored for that person alone.

**What this does and does not do.** Swiggy allowlists redirect URIs exact-match, so a real sign-in
cannot complete until Swiggy adds this one. Until then the code is tested against a stand-in.

## Run the same image locally

```bash
docker build -t namma-saathi .
docker run --rm -p 8000:8000 -e PUBLIC_DEPLOYMENT=true \
  -e PUBLIC_BASE_URL=https://example.invalid -e DEMO_UNDERSTANDING=scripted namma-saathi
```

Without `GNANI_API_KEY` the replies appear as text with a note that the voice is unavailable.

## Try the multi-user product without Swiggy

```bash
uv run python scripts/e2e_multi_user.py --serve   # by hand, at http://localhost:18791
uv run --with playwright python scripts/e2e_multi_user.py   # the whole journey in a browser
```

Both use the stand-in Swiggy, and the page says "Swiggy test data". Nothing reaches real Swiggy.
