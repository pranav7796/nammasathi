"""Browser check of reminders, notes and the waitlist against a running local server.

    uv run uvicorn app.web:app --port 18123      # with DATABASE_URL and TOKEN_ENCRYPTION_KEYS set
    uv run --with playwright python scripts/e2e_personal.py http://localhost:18123 OUT_DIR

It signs up a throwaway account, uses the page the way a person would, and deletes the account.
Demo shops only. Nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from uuid import uuid4

from playwright.async_api import async_playwright

BASE = sys.argv[1]
OUT = sys.argv[2]
PASSWORD = "check " + uuid4().hex[:10]
EMAIL = f"personal-{uuid4().hex[:8]}@example.com"
failed: list[str] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    if not passed:
        failed.append(name)


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=shutil.which("chromium"))
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        await page.goto(BASE)
        await page.wait_for_selector("#try-text")
        check("waitlist section is shown", await page.is_visible("#waitlist"))
        await page.fill("#waitlist-mount input[type=email]", f"wl-{uuid4().hex[:6]}@example.com")
        await page.check("#waitlist-mount input[type=checkbox] >> nth=-1")
        await page.click("#waitlist-mount button[type=submit]")
        await page.wait_for_selector("text=on the list", timeout=8000)
        check("waitlist sign-up shows success", True)

        reg = await page.evaluate(
            """async ([email, password]) => (await fetch('/api/account/register', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, password, consent: true})})).status""",
            [EMAIL, PASSWORD],
        )
        check("sign up", reg == 200, reg)
        await page.reload()
        await page.wait_for_selector("#personal-button:not([hidden])")
        check("Reminders button appears when signed in", True)

        await page.click("#personal-button")
        await page.wait_for_selector("#personal-dialog[open] .genui-w-form")
        await page.fill("#personal-widgets input[type=text] >> nth=0", "Call Amma")
        await page.fill("#personal-widgets input[type=date]", "2030-01-02")
        await page.fill("#personal-widgets input[type=time]", "19:00")
        await page.click("#personal-widgets .genui-w-form >> nth=0 >> button[type=submit]")
        await page.wait_for_selector("#personal-widgets >> text=Call Amma")
        check("a reminder made with the form appears in the list", True)
        await page.screenshot(path=f"{OUT}/personal-dialog.png")

        await page.click("#personal-widgets .genui-w-reminder_list >> role=checkbox >> nth=0")
        await page.wait_for_timeout(600)
        listed = await page.evaluate("fetch('/api/personal/reminders').then(r => r.json())")
        check("marking it done is saved", listed["reminders"][0]["done"] is True)

        await page.fill("#personal-widgets textarea", "Milk, rice, and curd")
        await page.click("#personal-widgets .genui-w-form >> nth=1 >> button[type=submit]")
        await page.wait_for_selector("#personal-widgets >> text=Milk, rice, and curd")
        check("a note appears in the list", True)
        await page.click("#personal-close")

        await page.click('[data-lang="en-IN"]')
        await page.click("#start-button")
        await page.wait_for_selector("#text-input")
        await page.fill("#text-input", "Remind me to pay rent tomorrow at 9 am")
        await page.click("#text-form button[type=submit], #text-form button")
        await page.wait_for_selector("#genui-inline >> text=pay rent", timeout=15000)
        check("saying a reminder shows the list in the conversation", True)
        said = await page.inner_text("#assistant-text")
        check("Saathi says it cannot alert yet", "can't alert you yet" in said, said[:80])
        await page.screenshot(path=f"{OUT}/personal-inline.png")

        mobile = await browser.new_page(viewport={"width": 390, "height": 844})
        await mobile.goto(BASE)
        await mobile.wait_for_selector("#try-text")
        check(
            "phone: no sideways scroll",
            not await mobile.evaluate("document.documentElement.scrollWidth > innerWidth"),
        )
        check("no script or page errors", not errors, errors[:2])

        await page.evaluate(
            """async (password) => fetch('/api/account/delete', {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password})})""",
            PASSWORD,
        )
        await browser.close()
    print("\nAll checks passed." if not failed else f"\n{len(failed)} checks FAILED.")
    sys.exit(1 if failed else 0)


asyncio.run(main())
