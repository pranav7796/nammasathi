"""Check, from outside, that real people can use a Namma Saathi deployment before Swiggy access.

    uv run python scripts/verify_multi_user.py https://YOUR-SERVICE.onrender.com
    uv run python scripts/verify_multi_user.py URL --voice        # also real Gnani speech in
    uv run python scripts/verify_multi_user.py URL --phase before-restart --state FILE
    uv run python scripts/verify_multi_user.py URL --phase after-restart --state FILE

It creates two throwaway accounts, signs in and out, runs the three demo journeys in English,
Hindi, and Kannada, checks that the two people cannot see each other's conversations, history,
or orders, and deletes the accounts at the end. Mock shops only: it never asks for live Swiggy.
Each run spends about 30 turns of the site's daily allowance, more with --voice.

The state file holds the test accounts' session cookies between phases; it is written with
owner-only permissions and removed after the second phase.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.demo_check import spoken

PASSWORD = "verify " + uuid4().hex[:12]
JOURNEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "en-IN": {
        "grocery": ("Two kilos of rice and one litre of milk", "Yes"),
        "food": ("I want something for dinner", "Chicken biryani", "The second one", "Yes"),
        "dining": (
            "Book a table for two in Indiranagar tomorrow at 7 PM, Italian",
            "First",
            "Yes",
        ),
    },
    "hi-IN": {
        "grocery": ("दो किलो चावल और एक लीटर दूध", "हाँ"),
        "dining": ("कल शाम सात बजे इंदिरानगर में दो लोगों के लिए इटालियन टेबल बुक करो", "पहला", "हाँ"),
    },
    "kn-IN": {
        "grocery": ("ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು", "ಹೌದು"),
        "food": ("ರಾತ್ರಿ ಊಟಕ್ಕೆ ಏನಾದರೂ ಬೇಕು", "ಚಿಕನ್ ಬಿರಿಯಾನಿ", "ಎರಡನೆಯದು", "ಹೌದು"),
    },
}
DEMO_WORDS = ("demo", "not a real", "डेमो", "असली नहीं", "ಡೆಮೊ", "ನಿಜವಾದ")


class Report:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def check(self, name: str, passed: bool, detail: object = "") -> bool:
        suffix = f": {detail}" if detail not in ("", None) else ""
        print(f"{'PASS' if passed else 'FAIL'}  {name}{suffix}", flush=True)
        if not passed:
            self.failed.append(name)
        return passed


class Person:
    def __init__(self, base: str, email: str) -> None:
        self.email = email
        self.http = httpx.AsyncClient(base_url=base, timeout=120, follow_redirects=False)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        return await self.http.post(path, json=body or {})


async def journey(
    person: Person, language: str, turns: tuple[str, ...], *, voice: str | None
) -> dict[str, Any]:
    started = await person.post("/api/sessions", {"language": language})
    started.raise_for_status()
    reply: dict[str, Any] = started.json()
    headers = {"X-Session-ID": reply["session_id"]}
    for index, text in enumerate(turns):
        if voice and index == 0:
            audio = await spoken(text, language, voice)
            response = await person.http.post(
                "/api/session/turn",
                content=audio,
                headers={**headers, "Content-Type": "audio/wav"},
            )
        else:
            response = await person.http.post(
                "/api/session/text", json={"text": text}, headers=headers
            )
        if response.status_code != 200:
            return {"state": f"HTTP {response.status_code}", "detail": response.text[:120]}
        reply = response.json()
        if reply.get("options") and index + 1 < len(turns) and turns[index + 1] == "Yes":
            break  # an extra question we did not script; reported below, not guessed through
    reply["session_id"] = headers["X-Session-ID"]
    return reply


async def run(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    report = Report()
    voice_key = os.environ.get("GNANI_API_KEY") if args.voice else None
    if args.voice and not voice_key:
        print("--voice needs GNANI_API_KEY in this machine's environment to make test speech.")
        return 2

    if args.phase == "after-restart":
        return await after_restart(base, Path(args.state), report)

    async with httpx.AsyncClient(timeout=120) as plain:
        health = await plain.get(f"{base}/healthz")
        report.check("site is up", health.status_code == 200, health.json().get("commerce_mode"))
        ready = await plain.get(f"{base}/readyz")
        report.check("database is reachable", ready.status_code == 200, ready.text[:80])
        if base.startswith("https://"):
            page = await plain.get(f"{base}/")
            report.check("HSTS", "max-age" in page.headers.get("strict-transport-security", ""))
            insecure = await plain.get(base.replace("https://", "http://", 1) + "/healthz")
            report.check(
                "plain HTTP is not served",
                insecure.status_code in {301, 302, 307, 308} or insecure.is_error,
                insecure.status_code,
            )
        me = (await plain.get(f"{base}/api/account/me")).json()
        if not report.check("accounts are on", me.get("enabled") is True and me.get("available")):
            return 1

    tag = uuid4().hex[:8]
    asha = Person(base, f"verify-asha-{tag}@example.com")
    ravi = Person(base, f"verify-ravi-{tag}@example.com")
    people = (asha, ravi)
    try:
        for person in people:
            response = await person.post(
                "/api/account/register",
                {"email": person.email, "password": PASSWORD, "consent": True},
            )
            report.check(
                f"{person.email[:13]}: sign up", response.status_code == 200, response.text[:80]
            )
            cookie = response.headers.get("set-cookie", "")
            if base.startswith("https://"):
                hardened = all(a in cookie.lower() for a in ("httponly", "secure", "samesite=lax"))
                report.check("session cookie is HttpOnly, Secure, SameSite", hardened)

        out = await asha.post("/api/account/logout")
        gone = (await asha.http.get("/api/account/me")).json().get("user")
        report.check("sign out ends the session", out.status_code == 200 and gone is None)
        wrong = await asha.post(
            "/api/account/login", {"email": asha.email, "password": "wrong one"}
        )
        report.check("a wrong password is refused", wrong.status_code == 401)
        back = await asha.post("/api/account/login", {"email": asha.email, "password": PASSWORD})
        report.check(
            "sign in again", back.status_code == 200, f"{back.status_code} {back.text[:80]}"
        )

        # Asha uses all three journeys across the three languages; Ravi does one in Hindi.
        finished = 0
        for language, journeys in JOURNEYS.items():
            for capability, turns in journeys.items():
                reply = await journey(asha, language, turns, voice=voice_key)
                done = reply.get("state") == "DONE" and reply.get("commerce_mode") == "mock"
                text = str(reply.get("assistant_text") or "")
                honest = any(word in text.casefold() for word in DEMO_WORDS)
                finished += done
                report.check(
                    f"{language} {capability}: finished on demo shops"
                    + (", spoken first turn" if voice_key else ""),
                    done,
                    reply.get("state") if not done else reply.get("understood_by") or "direct",
                )
                if done:
                    report.check(f"{language} {capability}: says it was a demo", honest, text[:70])
                    if voice_key:
                        report.check(
                            f"{language} {capability}: spoken reply",
                            bool(reply.get("audio_base64")),
                        )
        ravi_reply = await journey(ravi, "hi-IN", ("दो किलो चावल",), voice=None)

        orders = (await asha.http.get("/api/account/demo-orders")).json()["orders"]
        report.check(
            "Asha's demo orders are kept, marked demo", len(orders) == finished, len(orders)
        )
        real = (await asha.http.get("/api/account/orders")).json()["orders"]
        report.check("no demo order appears as a real order", real == [])
        history = (await asha.http.get("/api/account/history")).json()["turns"]
        report.check("Asha's conversation history is kept", len(history) > 0, len(history))
        language = (await asha.http.get("/api/account/me")).json()["preferences"]["language"]
        report.check("Asha's last language is remembered", language == "kn-IN", language)

        stolen = await ravi.http.get(
            "/api/session", headers={"X-Session-ID": str(ravi_reply.get("session_id"))}
        )
        report.check("Ravi can use his own conversation", stolen.status_code == 200)
        theirs = await asha.http.get(
            "/api/session", headers={"X-Session-ID": str(ravi_reply.get("session_id"))}
        )
        report.check("Asha cannot open Ravi's conversation", theirs.status_code == 410)
        ravi_said = [
            turn["said"] for turn in (await ravi.http.get("/api/account/history")).json()["turns"]
        ]
        asha_said = [turn["said"] for turn in history]
        report.check(
            "histories are separate",
            "दो किलो चावल" in ravi_said
            and "दो किलो चावल" not in asha_said
            and not any("ಅಕ್ಕಿ" in said for said in ravi_said),
        )
        report.check(
            "Ravi has none of Asha's orders",
            (await ravi.http.get("/api/account/demo-orders")).json()["orders"] == [],
        )

        phone = Person(base, asha.email)
        await phone.post("/api/account/login", {"email": asha.email, "password": PASSWORD})
        current = (await phone.http.get("/api/session/current")).json().get("session_id")
        report.check("Asha's conversation follows her to another device", bool(current))
        await phone.http.aclose()

        if args.phase == "before-restart":
            state = {
                "asha": {"email": asha.email, "cookies": dict(asha.http.cookies)},
                "ravi": {"email": ravi.email, "cookies": dict(ravi.http.cookies)},
                "password": PASSWORD,
                "orders": len(orders),
                "saved_at": time.time(),
            }
            path = Path(args.state)
            path.write_text(json.dumps(state))
            path.chmod(0o600)
            print(f"\nRestart the server now, then run with --phase after-restart --state {path}")
            people = ()  # keep the accounts for the second phase
    finally:
        for person in people:
            deleted = await person.post("/api/account/delete", {"password": PASSWORD})
            report.check(f"{person.email[:13]}: account deleted", deleted.status_code == 200)
        for person in (asha, ravi):
            await person.http.aclose()

    return summary(report)


async def after_restart(base: str, state_path: Path, report: Report) -> int:
    state = json.loads(state_path.read_text())
    try:
        for name in ("asha", "ravi"):
            person = Person(base, state[name]["email"])
            for key, value in state[name]["cookies"].items():
                person.http.cookies.set(key, value)
            me: dict[str, Any] = (await person.http.get("/api/account/me")).json()
            user: dict[str, Any] = me.get("user") or {}
            report.check(
                f"{name}: still signed in after the restart", user.get("email") == person.email
            )
            if name == "asha":
                orders = (await person.http.get("/api/account/demo-orders")).json()["orders"]
                report.check("demo orders survived", len(orders) == state["orders"], len(orders))
                history = (await person.http.get("/api/account/history")).json()["turns"]
                report.check("history survived", len(history) > 0, len(history))
                report.check("language survived", me["preferences"]["language"] == "kn-IN")
            deleted = await person.post("/api/account/delete", {"password": state["password"]})
            report.check(f"{name}: account deleted", deleted.status_code == 200)
            await person.http.aclose()
    finally:
        state_path.unlink(missing_ok=True)
    return summary(report)


def summary(report: Report) -> int:
    print("\nAll checks passed." if not report.failed else f"\n{len(report.failed)} checks FAILED.")
    return 1 if report.failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument(
        "--phase", choices=("all", "before-restart", "after-restart"), default="all"
    )
    parser.add_argument("--state", default="var/verify-state.json")
    args = parser.parse_args()
    if args.phase != "all" and not args.state:
        parser.error("--state is required with --phase")
    load_dotenv()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
