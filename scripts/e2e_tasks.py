"""Browser check of the assistant on demo data: tasks, approvals, My day, and the guided demo.

    uv run uvicorn app.web:app --port 18123      # with DATABASE_URL and TOKEN_ENCRYPTION_KEYS set
    uv run --with playwright python scripts/e2e_tasks.py http://localhost:18123 OUT_DIR

Creates a throwaway account and deletes it. Demo data only: nothing reaches Gmail, Google
Calendar or WhatsApp, and every result on screen must say it is a demo.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from uuid import uuid4

from playwright.async_api import Page, async_playwright

BASE = sys.argv[1]
OUT = sys.argv[2]
PASSWORD = "check " + uuid4().hex[:10]
EMAIL = f"tasks-{uuid4().hex[:8]}@example.com"
failed: list[str] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    if not passed:
        failed.append(name)


async def until(read, expect, timeout: float = 15.0) -> str:
    """Poll a page value from Python (the site's CSP forbids string evaluation)."""
    deadline = asyncio.get_event_loop().time() + timeout
    value = ""
    while asyncio.get_event_loop().time() < deadline:
        value = str(await read())
        if expect(value):
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out; last value: {value[:200]!r}")


async def say(page: Page, text: str) -> None:
    await page.fill("#text-input", text)
    await page.press("#text-input", "Enter")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=shutil.which("chromium"))
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        await page.goto(BASE)
        await page.wait_for_selector("#try-text")
        status = await page.evaluate(
            """async ([email, password]) => (await fetch('/api/account/register', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, password, consent: true})})).status""",
            [EMAIL, PASSWORD],
        )
        check("sign up", status == 200, status)
        await page.reload()
        await page.wait_for_selector("#personal-button:not([hidden])")

        # The new example tabs show phrases in each language.
        await page.click('[data-lang="en-IN"]')
        await page.click('[data-cap="multi"]')
        check(
            "'Everything at once' example is offered", "Rahul" in await page.inner_text("#try-text")
        )
        await page.click('[data-lang="hi-IN"]')
        check("the example follows the language", "राहुल" in await page.inner_text("#try-text"))
        await page.click('[data-lang="en-IN"]')
        await page.screenshot(path=f"{OUT}/tasks-home.png")

        # One sentence, several tasks.
        await page.click("#try-phrase")
        await page.wait_for_selector(".genui-w-task_board", timeout=20000)
        check("one sentence produces a task board", True)
        await page.wait_for_selector(".genui-w-composer", timeout=20000)
        await page.wait_for_selector(".genui-w-event_preview", timeout=20000)
        await page.wait_for_selector(".genui-w-email_list", timeout=20000)
        board = await page.inner_text(".genui-w-task_board")
        check("the board says nothing changes until you approve", "approve" in board.lower())
        check("the board is marked demo", "demo" in board.lower())
        check("email, event and message widgets all appear", True)
        await page.screenshot(path=f"{OUT}/tasks-board.png", full_page=True)

        # Change your mind: the same card updates, no second card.
        await say(page, "Make the meeting 4 PM instead")
        await until(
            lambda: page.locator(".genui-w-event_preview input[type=time]").input_value(),
            lambda v: v.startswith("16:00"),
        )
        check("the meeting card updated in place", True)
        check(
            "there is still exactly one event card",
            await page.locator(".genui-w-event_preview").count() == 1,
        )

        # Approve the WhatsApp draft from its own widget.
        composer = page.locator(".genui-w-composer").first
        await composer.get_by_role("button", name="Send").click()
        await until(
            lambda: composer.inner_text(),
            lambda v: "not really sent" in v.lower() or "no message was sent" in v.lower(),
        )
        text = (await composer.inner_text()).lower()
        check(
            "a demo send says it was not really sent",
            "not really sent" in text or "no message was sent" in text,
            text[:100],
        )
        check("and never claims delivery", "delivered" not in text)

        # Approve the event.
        await page.locator(".genui-w-event_preview").get_by_role("button", name="Approve").click()
        await until(
            lambda: page.locator(".genui-w-event_preview").inner_text(),
            lambda v: "not really created" in v.lower() or "practice only" in v.lower(),
        )
        check("a demo event says it was not really created", True)
        await page.screenshot(path=f"{OUT}/tasks-approved.png", full_page=True)

        # Connected apps: honest states, never "connected" without the service confirming.
        await page.click("#apps-button")
        await page.wait_for_selector("#apps-dialog[open] .status-chip:not([data-state=loading])")
        apps = (await page.inner_text("#apps-grid")).lower()
        check(
            "apps say configuration is required when not set up", "configuration required" in apps
        )
        check("and name the settings that are missing", "google_client_id" in apps)
        check("nothing is shown as connected", "connected" not in apps.replace("not connected", ""))
        await page.screenshot(path=f"{OUT}/tasks-apps.png")
        await page.click("#apps-close")

        # My day, then reminders, notes and contacts.
        await page.click("#personal-button")
        await page.wait_for_selector("#myday-dialog[open] .day-card")
        check(
            "My day explains an unconnected calendar",
            "Connect Google Calendar" in await page.inner_text("#myday-grid"),
        )
        await page.screenshot(path=f"{OUT}/tasks-myday.png")
        await page.click("#myday-manage")
        await page.wait_for_selector("#personal-dialog[open] #contact-form")
        await page.fill('#contact-form input[name="name"]', "Amma")
        await page.fill('#contact-form input[name="phone"]', "+919876543210")
        await page.click("#contact-form button[type=submit]")
        await page.wait_for_selector("#contacts-list >> text=Amma")
        check("a contact can be saved", True)
        await page.click("#personal-close")

        # Guided demo, from a fresh start (a reload would otherwise resume the conversation).
        await page.evaluate("sessionStorage.clear()")
        await page.goto(BASE)
        await page.wait_for_selector("#personal-button:not([hidden])")
        await page.wait_for_timeout(1500)  # a signed-in person returns to their open conversation
        fresh = page.get_by_text("Start a new conversation")
        if await fresh.count() and await fresh.first.is_visible():
            await fresh.first.click()
        await page.click("#guide-start")
        await page.wait_for_selector("#guide:not([hidden])")
        check(
            "the guided demo starts",
            "step 1 of 6" in (await page.inner_text("#guide-step")).lower(),
        )
        await page.click("#guide-say")
        await page.wait_for_selector(".genui-w-day_planner", timeout=20000)
        check("step 1 shows the day planner", True)
        await page.click("#guide-next")
        check("it moves to step 2", "step 2 of 6" in (await page.inner_text("#guide-step")).lower())
        await page.screenshot(path=f"{OUT}/tasks-guide.png")
        await page.click("#guide-stop")

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
