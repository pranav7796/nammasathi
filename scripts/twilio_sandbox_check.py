# ruff: noqa: E501
"""Check the Twilio WhatsApp setup for real, without sending any WhatsApp message.

    uv run python scripts/twilio_sandbox_check.py                 # settings + Twilio credentials
    uv run python scripts/twilio_sandbox_check.py --webhook       # also call your public webhook

1. Lists which Twilio settings are present (names only, never values).
2. Reads your Twilio account with those credentials (a read; nothing is sent).
3. With --webhook: posts one request signed with your Auth Token to
   PUBLIC_BASE_URL/api/twilio/whatsapp from a made-up, unlinked number. A correct setup answers 200
   with "isn't linked". That proves the address is reachable over HTTPS and the signature check
   passes. The reply comes back to this script, not to any phone.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.integrations.base import IntegrationError, NotConnected, ReauthRequired
from app.twilio_whatsapp import expected_signature, webhook_url

NAMES = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY_SID",
    "TWILIO_API_KEY_SECRET",
    "TWILIO_WHATSAPP_FROM",
    "PUBLIC_BASE_URL",
)


async def main(check_webhook: bool) -> int:
    import os

    load_dotenv()
    failed = 0
    print("Settings (names only):")
    for name in NAMES:
        print(f"  {'set    ' if os.environ.get(name) else 'MISSING'}  {name}")
    settings = load_settings()
    if not settings.twilio_auth_token:
        print("\nTWILIO_AUTH_TOKEN is missing. Twilio signs every webhook with the Auth Token,")
        print("so inbound messages will be refused until it is set (Console > Account info).")
        failed += 1
    print(f"\nSender: {settings.twilio_sender}")
    print(
        f"Webhook to enter in Twilio (POST): {webhook_url(settings.public_base_url or 'PUBLIC_BASE_URL')}"
    )

    from app.twilio_messaging import TwilioMessaging

    class _NoDatabase:  # check() reads Twilio only; it never touches the database
        pass

    print("\nTwilio credentials:", end=" ")
    try:
        await TwilioMessaging(settings, _NoDatabase()).check()  # type: ignore[arg-type]
        print("accepted by Twilio")
    except NotConnected:
        print("not enough settings to sign in to Twilio")
        failed += 1
    except ReauthRequired:
        print("REFUSED by Twilio (wrong SID, token, or API key)")
        failed += 1
    except IntegrationError as exc:
        print(f"could not check ({exc})")
        failed += 1

    if check_webhook:
        if not (settings.twilio_auth_token and settings.public_base_url.startswith("https://")):
            print("\nWebhook: needs TWILIO_AUTH_TOKEN and an https PUBLIC_BASE_URL")
            return 1
        url = webhook_url(settings.public_base_url)
        params = {
            "MessageSid": "SMselfcheck0001",
            "AccountSid": settings.twilio_account_sid,
            "From": "whatsapp:+10000000000",
            "To": settings.twilio_sender,
            "Body": "self check",
        }
        signature = expected_signature(settings.twilio_auth_token, url, params)
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.post(
                url,
                content=urlencode(params),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Twilio-Signature": signature,
                },
            )
        ok = response.status_code == 200 and "isn't linked" in response.text
        print(
            f"\nWebhook {url}: {response.status_code} {'reachable, signature accepted' if ok else response.text[:120]}"
        )
        failed += 0 if ok else 1
    print("\nAll checks passed." if not failed else f"\n{failed} problem(s) to fix.")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args().webhook)))
