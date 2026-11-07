# Connecting Gmail, Google Calendar and WhatsApp

**Status: implemented and tested against stand-ins built from each service's official docs. Not
live-verified.** No real Google or Meta credentials were available, so nothing here has run against
the real services. Each step below ends with what to check. Until you set these up, Saathi uses
clearly labelled **demo** data for that service and never pretends otherwise.

All of it is free: Google's OAuth "Testing" mode and Meta's WhatsApp test number cost nothing.
Do not enable anything paid.

## Gmail and Google Calendar

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project (free).
2. **APIs & Services > Library**: enable the **Gmail API** and the **Google Calendar API**.
3. **OAuth consent screen**: choose **External**, keep the publishing status **Testing**, and add
   yourself and anyone who will try it under **Test users** (up to 100). Testing mode needs no
   Google review, but Google shows an "unverified app" warning and expires a test user's refresh
   token after 7 days (they reconnect).
4. **Credentials > Create credentials > OAuth client ID > Web application**. Under
   **Authorized redirect URIs** add exactly `PUBLIC_BASE_URL/api/google/callback`, for example
   `https://namma-saathi-7b62.onrender.com/api/google/callback`, and for local tests
   `http://localhost:8123/api/google/callback` (with `PUBLIC_BASE_URL=http://localhost:8123`).
5. Set in the server's environment (never in git):

   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   PUBLIC_BASE_URL=https://your-site
   ```

6. Sign in, open **My day > Connected apps**, press **Connect** for Gmail, approve, then Calendar.

Scopes requested (the minimum): `openid email`, `gmail.readonly` and `gmail.send` for Gmail, and
`calendar.events` for Calendar. Saathi stores only the refresh token, encrypted per person and per
service. **Disconnect** revokes it at Google and deletes it.

**Check it works:** "Check my important emails" shows your real mail without a Demo badge. "Reply to
X saying ..." shows a draft; **Send** really sends only after you press it.

Not verified against the real Google: whether a web client may send a PKCE `code_verifier` together
with its secret, whether a refresh can return a new refresh token (handled if it does), the exact
403 reason strings, and `Retry-After` on 429. Treat the first live run as a test.

## WhatsApp

Official **WhatsApp Business Cloud API** only. No WhatsApp Web, no unofficial library.

1. In [Meta for Developers](https://developers.facebook.com/) create an app of type **Business** and
   add the **WhatsApp** product. Meta gives a free **test phone number**.
2. Under **API Setup** add up to 5 recipient numbers (your own) and verify each with the code.
3. Copy the **Phone number ID**, a **temporary access token** (expires in 24 hours; a permanent one
   needs a System User) and the **App secret** (App settings > Basic).
4. **Webhooks**: callback URL `PUBLIC_BASE_URL/api/whatsapp/webhook`, a verify token you invent, and
   subscribe to `messages`.
5. Set:

   ```
   WHATSAPP_ACCESS_TOKEN=...
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_APP_SECRET=...
   WHATSAPP_VERIFY_TOKEN=...
   WHATSAPP_GRAPH_VERSION=v21.0
   ```

   The adapter's author found the docs' examples use a newer version (v27.0). Use the version your
   Meta dashboard shows as current.

Rules the app follows: it sends only after you approve the exact message; free text works only
within 24 hours of the recipient's last message to your number (otherwise Meta needs an approved
template, which this version does not send, and the error is shown plainly); and it shows the status
Meta reports, "accepted" first, never "delivered" until Meta's webhook says so.

**Check it works:** save a contact (My day > People you message) with a verified number, then
"Send Amma a WhatsApp message saying I'll be late", approve it, and watch the status change.

Not verified against Meta: error code meanings 131030 and the test-number limits, the full status
value list, and the signature page's wording (standard HMAC-SHA256 over the raw body is used).

## What stays demo until you do this

| Service | Without setup | With setup |
|---|---|---|
| Gmail | Seeded practice inbox, marked Demo, nothing sent | Your real inbox, real sends after approval |
| Calendar | Practice calendar, marked Demo, nothing created | Your real calendar, real events after approval |
| WhatsApp | Practice number, marked Demo, "accepted" at most | The test number, real messages after approval |
| Reminders, notes, memory | Real (stored in your account) | Same |

Meeting invitations: an event with attendees sends Google invitations by email, and the approval
card says so. Events without attendees send nothing.

## WhatsApp through Twilio (inbound)

Lets a person message Namma Saathi from their own WhatsApp. Implemented and tested against signed
test posts and Twilio's own documented signature example; **not yet tried with real Twilio.**

**Webhook:** `PUBLIC_BASE_URL/api/twilio/whatsapp`, method **POST**. For the live site:
`https://namma-saathi-7b62.onrender.com/api/twilio/whatsapp` (this exists only once the branch is
deployed and the settings below are set; until then that address returns 404).

Settings (server environment only, never in git):

```
TWILIO_ACCOUNT_SID=AC...             # requests for any other account are refused
TWILIO_AUTH_TOKEN=...                # verifies X-Twilio-Signature on every request
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886   # the sandbox number, shown to people when linking
PUBLIC_BASE_URL=https://namma-saathi-7b62.onrender.com
```

`PUBLIC_BASE_URL` must match the webhook address exactly: the signature covers the full URL.

In Twilio: **Messaging > Try it out > Send a WhatsApp message > Sandbox settings**, set
**When a message comes in** to the webhook above, method **POST**, and save. From your phone, send the
sandbox's `join <code>` message to the sandbox number first.

Linking: sign in, open **Apps > WhatsApp (Twilio) > Link my WhatsApp**, then send the shown
`link ABC123` from your phone within 15 minutes. Only a linked number can act on an account, and only
on its own. Replies come back in the same chat (TwiML), within Twilio's and WhatsApp's 24-hour window.
Email, calendar, reminders, WhatsApp drafts and the day plan work over WhatsApp; anything that changes
something still needs "send it", the read-back, and "confirm". Groceries, food and tables stay on the
website.
