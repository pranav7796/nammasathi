"""Check a deployed Namma Saathi from outside, and print its exact redirect URI.

    uv run python scripts/verify_deployment.py https://YOUR-SERVICE.onrender.com

It only uses the public demo's own routes. It finishes one journey on the mock shops, and it does
not contact Swiggy itself.
"""

from __future__ import annotations

import sys

import httpx


def main(base: str) -> int:
    base = base.rstrip("/")
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        print(f"{'PASS' if passed else 'FAIL'}  {name}{f': {detail}' if detail else ''}")
        if not passed:
            failures.append(name)

    with httpx.Client(timeout=120, follow_redirects=False) as client:
        health = client.get(f"{base}/healthz").json()
        check("served over HTTPS", base.startswith("https://"))
        check("reports healthy", health.get("status") == "ok")
        check("runs mock shops only", health.get("commerce_mode") == "mock")
        check("public guard is on", health.get("public_deployment") is True)
        callback = health.get("oauth_callback")
        check("callback uses this address", callback == f"{base}/auth/callback", str(callback))
        check("page loads", client.get(f"{base}/").status_code == 200)
        ready = client.get(f"{base}/readyz")
        body = ready.json()
        check("database reachable", ready.status_code == 200, str(body.get("database")))
        print(f"      live Swiggy open: {body.get('multi_user_live')}")
        account = client.get(f"{base}/api/account/me").json()
        print(f"      accounts enabled: {account.get('enabled')}")
        page = client.get(f"{base}/")
        check(
            "security policy is set",
            "script-src 'self'" in page.headers.get("content-security-policy", ""),
        )
        check(
            "HSTS is set",
            "max-age" in page.headers.get("strict-transport-security", ""),
        )

        forged = client.get(f"{base}/auth/callback", params={"code": "x", "state": "forged"})
        check("callback exists and refuses a forged state", forged.status_code == 400)

        started = client.post(f"{base}/api/sessions", json={"language": "en-IN"})
        check("conversation starts", started.status_code == 200, started.text[:120])
        if started.status_code == 200:
            headers = {"X-Session-ID": started.json()["session_id"]}
            for text, wanted in (
                ("Two kilos of rice and one litre of milk", "READBACK"),
                ("Yes", "DONE"),
            ):
                reply = client.post(
                    f"{base}/api/session/text", json={"text": text}, headers=headers
                )
                body = reply.json()
                check(f"'{text}' reaches {wanted}", body.get("state") == wanted)
                print(f"      voice: {'yes' if body.get('audio_base64') else 'no'}")
                print(f"      understood by: {body.get('understood_by') or 'direct'}")

    if failures:
        print(f"\n{len(failures)} checks failed.")
        return 1
    print(f"\nAll checks passed.\nProduction redirect URI: {callback}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
