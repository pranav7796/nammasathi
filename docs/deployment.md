# Deployment

The public deployment has three levels, each set by configuration:

| Level | Adds | Reaches Swiggy? |
|---|---|---|
| Demo | Mock shops for anyone, no sign-up | No |
| Accounts | `DATABASE_URL` and `TOKEN_ENCRYPTION_KEYS`: sign-up, sign-in, account management | No |
| Live | `MULTI_USER_LIVE`, after Swiggy grants access: approved users connect their own Swiggy | Yes, per user |

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

## What a visitor can spend

Each public turn uses real speech and LLM quota. `PUBLIC_DAILY_TURN_LIMIT` (default 300) and
`PUBLIC_MAX_SESSIONS` (default 50) cap that, after which visitors see a plain message. OpenRouter's
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
