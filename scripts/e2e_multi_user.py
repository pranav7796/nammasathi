"""Drive the whole multi-user product in a real browser, against the stand-in Swiggy.

    uv run --with playwright python scripts/e2e_multi_user.py [--chromium PATH] [--out DIR]
    uv run python scripts/e2e_multi_user.py --serve     # try it by hand at http://localhost:18791

Real page, real HTTP, real OAuth redirects, real adapters, real MCP SDK. Swiggy itself is
tests/fake_swiggy.py, which proves our side only. Nothing can reach real Swiggy from here: a
SwiggyStub routes every call to the stand-in. Scripted understanding and no voice.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import httpx
import httpx2
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.accounts.service import AccountService, SwiggyStub
from app.commerce.swiggy import SWIGGY_STAGING_MCP_URL
from app.config import Settings
from app.llm.scripted import ScriptedExtractor
from app.web import create_app
from tests.fake_swiggy import FakeSwiggy

SWIGGY_PORT, APP_PORT = 18790, 18791
SITE = f"http://localhost:{APP_PORT}"
KEY = base64.urlsafe_b64encode(b"e" * 32).decode()
PASSWORD = "correct horse battery"


class Inbox:
    can_send = True

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def send(self, to: str, subject: str, body: str) -> bool:
        del to, subject
        self.bodies.append(body)
        print(f"[inbox] {body}", flush=True)
        return True

    def link(self, marker: str) -> str:
        return self.bodies[-1].split(f"{SITE}/#{marker}=", maxsplit=1)[1].split()[0]


class SilentTTS:
    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        del text
        destination = Path(output_path)
        destination.write_bytes(b"RIFF")
        return destination


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, passed: bool, detail: object = "") -> None:
        print(
            f"{'PASS' if passed else 'FAIL'}  {name}{f': {detail}' if detail else ''}", flush=True
        )
        if not passed:
            self.failed += 1


async def serve_forever(app: object, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, port=port, log_level="warning")  # type: ignore[arg-type]
    server = uvicorn.Server(config)
    asyncio.ensure_future(server.serve())  # noqa: RUF006
    while not server.started:
        await asyncio.sleep(0.05)
    return server


async def journey(args: argparse.Namespace, fake: FakeSwiggy, inbox: Inbox, app: object) -> int:
    from playwright.async_api import Page, async_playwright

    checks = Checks()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    service: AccountService = app.state.accounts  # type: ignore[attr-defined]

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(executable_path=args.chromium)
        problems: list[str] = []

        async def browser_for(*, phone: bool = False) -> Page:
            context = await browser.new_context(
                viewport={"width": 390, "height": 844} if phone else {"width": 1280, "height": 800}
            )
            page = await context.new_page()

            def on_console(message: object) -> None:
                text = str(getattr(message, "text", ""))
                # The script's deliberate wrong password, and a stale conversation after sign-out.
                expected = "401" in text or "410" in text
                if getattr(message, "type", "") == "error" and not expected:
                    problems.append(text)

            page.on("console", on_console)
            page.on("pageerror", lambda error: problems.append(str(error)))
            return page

        async def say(page: Page, text: str) -> None:
            await page.fill("#text-input", text)
            await page.click("#text-send")
            await page.wait_for_function("() => !document.querySelector('#text-send').disabled")

        async def register(page: Page, email: str) -> None:
            await page.goto(SITE)
            await page.click("#account-button")
            await page.click("#register-open")
            await page.fill("#register-email", email)
            await page.fill("#register-password", PASSWORD)
            await page.check('#register-form input[name="consent"]')
            await page.click('#register-form button[type="submit"]')
            await page.wait_for_selector("#account-view-account:not([hidden])")

        async def connect(page: Page, email: str, account: str) -> None:
            await page.goto(f"{SITE}/#verify={inbox.link('verify')}")
            await page.wait_for_selector("#account-view-account:not([hidden])")
            user = service.store.user_by_email(email)
            assert user is not None
            service.store.set_live_access(user.id, True)
            await page.goto(f"{SITE}/#signin")
            await page.wait_for_selector("#account-connect:not([hidden])")
            await page.screenshot(path=out / f"{account}-before-connect.png")
            await page.click("#account-connect")
            await page.wait_for_selector("text=Stand-in Swiggy sign-in")
            await page.click(f"text=Continue as {account}")
            await page.wait_for_selector("#account-disconnect:not([hidden])")
            status = await page.text_content("#account-swiggy-status")
            checks.check(f"{account}: Swiggy connected", "Connected" in (status or ""))
            await page.click("#account-close")

        # 1. A stranger can try the demo with no account, and it says it's a demo.
        visitor = await browser_for()
        await visitor.goto(SITE)
        await visitor.wait_for_function("() => !document.querySelector('#start-button').disabled")
        checks.check("no account: Sign in is offered", await visitor.is_visible("#account-button"))
        checks.check("no account: no live choice", not await visitor.is_visible("#mode-choice"))
        await visitor.goto(f"{SITE}/?typed=1")
        await visitor.wait_for_function("() => !document.querySelector('#start-button').disabled")
        await visitor.click("#start-button")
        await visitor.wait_for_selector("#conversation:not([hidden])")
        await say(visitor, "Two kilos of rice and one litre of milk")
        await say(visitor, "Yes")
        result = await visitor.text_content("#order-result")
        checks.check(
            "no account: demo order is labelled", "demo" in (result or "").casefold(), result
        )
        checks.check("no account: nothing reached Swiggy", fake.calls == [])

        # 2. Asha registers on a laptop. She has an account, but no live access yet.
        asha = await browser_for()
        await register(asha, "asha@example.com")
        await asha.screenshot(path=out / "account-new.png")
        verified = await asha.text_content("#account-verified")
        checks.check("register: email awaits verification", "not verified" in (verified or ""))
        await asha.click("#account-close")
        checks.check("register: still no live choice", not await asha.is_visible("#mode-choice"))

        # 2b. Before any Swiggy access: her own demo, in Hindi, kept on her account.
        await asha.goto(f"{SITE}/?typed=1")
        await asha.wait_for_function("() => !document.querySelector('#start-button').disabled")
        await asha.click('[data-lang="hi-IN"]')
        await asha.click("#start-button")
        await asha.wait_for_selector("#conversation:not([hidden])")
        await say(asha, "दो किलो चावल और एक लीटर दूध")
        await say(asha, "हाँ")
        await asha.wait_for_selector("#order-panel:not([hidden])")
        hindi = await asha.text_content("#order-result")
        checks.check(
            "demo account: Hindi order finished and says demo",
            "demo" in (hindi or "").casefold(),
            hindi,
        )
        await asha.goto(f"{SITE}/#signin")
        await asha.wait_for_selector("#account-demo-orders li")
        demo_list = await asha.text_content("#account-demo-orders")
        checks.check(
            "demo account: order kept, marked not real",
            "not real" in (demo_list or "").casefold(),
            demo_list,
        )
        await asha.click("#account-history-card summary")
        history_text = await asha.text_content("#account-history")
        checks.check("demo account: history kept", "चावल" in (history_text or ""))
        real_empty = await asha.is_visible("#account-orders-empty")
        checks.check("demo account: nothing under real orders", real_empty)
        await asha.screenshot(path=out / "account-demo-history.png")
        await asha.click("#account-close")

        # 2c. On another device she signs in: her language and conversation come with her.
        tablet = await browser_for()
        await tablet.goto(f"{SITE}/?typed=1")
        await tablet.click("#account-button")
        await tablet.fill("#signin-email", "asha@example.com")
        await tablet.fill("#signin-password", PASSWORD)
        await tablet.click('#signin-form button[type="submit"]')
        await tablet.wait_for_selector("#account-view-account:not([hidden])")
        await tablet.click("#account-close")
        await tablet.goto(f"{SITE}/?typed=1")
        await tablet.wait_for_selector("#conversation:not([hidden])")
        restored = await tablet.text_content("#assistant-text")
        checks.check(
            "another device: conversation carried over", "डेमो" in (restored or ""), restored
        )
        await tablet.click("#new-session-button")
        pressed = await tablet.get_attribute('[data-lang="hi-IN"]', "aria-pressed")
        checks.check("another device: Hindi remembered", pressed == "true")

        # 3. Verified and invited, she connects through the OAuth redirect and shops live.
        await connect(asha, "asha@example.com", "asha")
        await asha.goto(f"{SITE}/?typed=1")
        # Signed in, she lands back in her demo conversation; she starts a new one to go live.
        await asha.wait_for_selector("#conversation:not([hidden])")
        await asha.click("#new-session-button")
        await asha.wait_for_selector("#mode-choice:not([hidden])")
        await asha.wait_for_function("() => !document.querySelector('#start-button').disabled")
        pressed = await asha.get_attribute("#mode-live", "aria-pressed")
        checks.check("live: chosen after connecting", pressed == "true")
        options = await asha.text_content("#delivery-address")
        checks.check("live: her own address", "CMH Road" in (options or ""), options)
        await asha.screenshot(path=out / "start-live-choice.png")
        await asha.click("#start-button")
        await asha.wait_for_selector("#conversation:not([hidden])")
        await say(asha, "One litre of milk")
        for _ in range(3):
            if await asha.is_visible("#confirmation-panel"):
                break
            await say(asha, "First")
        await asha.screenshot(path=out / "live-readback.png")
        button = await asha.text_content("#confirm-button")
        # This harness is the staging address, so it must not claim to be real.
        checks.check("staging: says test, not real", "test" in (button or "").casefold(), button)
        badge = await asha.text_content("#commerce-badge")
        checks.check("staging: badge says test data", "test" in (badge or "").casefold(), badge)
        checks.check("live: no order before the yes", "checkout" not in fake.tools_called())
        await say(asha, "Yes")
        await asha.wait_for_selector("#order-panel:not([hidden])")
        await asha.screenshot(path=out / "live-order-done.png")
        checks.check("live: exactly one order", len(fake.accounts["asha"].orders) == 1)
        order_text = await asha.text_content("#order-result")
        checks.check(
            "live: not called a demo", "demo" not in (order_text or "").casefold(), order_text
        )

        # 4. Ravi, on a phone, sees none of it.
        ravi = await browser_for(phone=True)
        await register(ravi, "ravi@example.com")
        await connect(ravi, "ravi@example.com", "ravi")
        await ravi.goto(f"{SITE}/?typed=1")
        await ravi.wait_for_selector("#mode-choice:not([hidden])")
        await ravi.wait_for_function("() => !document.querySelector('#start-button').disabled")
        options = await ravi.text_content("#delivery-address")
        checks.check(
            "isolation: Ravi sees his address", "Temple Street" in (options or ""), options
        )
        checks.check("isolation: not Asha's", "CMH Road" not in (options or ""))
        await ravi.screenshot(path=out / "phone-start.png")
        await ravi.goto(f"{SITE}/#signin")
        await ravi.wait_for_selector("#account-view-account:not([hidden])")
        await ravi.screenshot(path=out / "phone-account.png")
        empty = await ravi.is_visible("#account-orders-empty")
        checks.check("isolation: Ravi has no orders", empty)
        overflow = await ravi.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        checks.check("phone: no sideways scroll", overflow <= 0, overflow)
        await ravi.click("#account-close")

        # 5. Asha's order is on her account page, and a wrong password is refused.
        await asha.goto(f"{SITE}/#signin")
        await asha.wait_for_selector("#account-view-account:not([hidden])")
        await asha.wait_for_selector("#account-orders li")
        await asha.screenshot(path=out / "account-orders.png")
        await asha.click("text=Your data")
        await asha.fill("#delete-password", "not my password")
        await asha.click("#delete-form button[type='submit']")
        await asha.wait_for_selector("#account-message:not([hidden])")
        checks.check(
            "delete: wrong password refused",
            service.store.user_by_email("asha@example.com") is not None,
        )

        # 6. Disconnecting ends it at Swiggy; deleting removes the account.
        await asha.click("#account-disconnect")
        await asha.wait_for_selector("#account-connect:not([hidden])")
        checks.check("disconnect: ended at Swiggy", fake.logouts == ["asha"])
        await asha.fill("#delete-password", PASSWORD)
        await asha.click("#delete-form button[type='submit']")
        await asha.wait_for_url(f"{SITE}/")
        await asha.wait_for_selector("#account-button:has-text('Sign in')")
        checks.check(
            "delete: account gone", service.store.user_by_email("asha@example.com") is None
        )

        checks.check("no script, policy, or page errors", not problems, problems[:5])
        await browser.close()

    print(f"\nScreenshots in {out}")
    print("All checks passed." if not checks.failed else f"{checks.failed} checks FAILED.")
    return 1 if checks.failed else 0


async def run(args: argparse.Namespace) -> int:
    inbox = Inbox()
    fake = FakeSwiggy({f"{SITE}/auth/callback"})
    with tempfile.TemporaryDirectory(prefix="saathi-e2e-") as folder:
        settings = replace(
            Settings(),
            commerce_backend="swiggy",
            swiggy_live_access=True,
            swiggy_mcp_url=SWIGGY_STAGING_MCP_URL,
            multi_user_live=True,
            public_base_url=SITE,
            database_url=f"sqlite:///{Path(folder) / 'e2e.db'}",
            token_encryption_keys=KEY,
            allow_swiggy_cart_writes=True,
            allow_real_checkout=True,
            allow_real_booking=True,
            real_order_limit=50,
        )
        stub = SwiggyStub(
            oauth_transport=httpx.ASGITransport(app=fake.app),
            mcp_transport=httpx2.ASGITransport(app=fake.app),
            browser_base=f"http://localhost:{SWIGGY_PORT}",
        )
        app = create_app(
            settings=settings,
            extractor_factory=ScriptedExtractor,  # type: ignore[arg-type]
            tts_factory=lambda _s, _l: SilentTTS(),  # type: ignore[arg-type,return-value]
            mailer=inbox,
            swiggy_stub=stub,
        )
        fake_server = await serve_forever(fake.app, SWIGGY_PORT)
        app_server = await serve_forever(app, APP_PORT)
        try:
            if args.serve:
                print(f"Open {SITE}  (stand-in Swiggy on port {SWIGGY_PORT}; Ctrl-C to stop)")
                print("Verification links print here. Everyone is invited in this harness.")
                while True:
                    with app.state.accounts.database.transaction() as tx:
                        tx.execute("UPDATE users SET live_access = 1")
                    await asyncio.sleep(2)
            return await journey(args, fake, inbox, app)
        finally:
            app_server.should_exit = True
            fake_server.should_exit = True
            await asyncio.sleep(0.3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default=shutil.which("chromium") or "chromium")
    parser.add_argument("--out", default="var/e2e-screenshots")
    parser.add_argument("--serve", action="store_true")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
