# ruff: noqa: E501  (long JavaScript snippets are embedded as strings; wrapping would hurt reading)
"""Responsive, multilingual and accessibility checks of the redesigned page, in a real browser.

    uv run uvicorn app.web:app --port 18125     # with DATABASE_URL and TOKEN_ENCRYPTION_KEYS set
    uv run --with playwright python scripts/e2e_design.py http://localhost:18125 OUT_DIR

At 1366, 1024, 768, 390 and 360 px wide it checks for horizontal scroll, text spilling out of its
box (long Kannada and Hindi included), content hidden behind the fixed composer, dialogs that do not
fit the screen, small tap targets, and reduced-motion handling. It signs up a throwaway account,
uses demo data only, saves a screenshot of each screen, and deletes the account.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from uuid import uuid4

from playwright.async_api import Page, async_playwright

BASE, OUT = sys.argv[1], sys.argv[2]
PASSWORD = "design " + uuid4().hex[:10]
SIZES = [(1366, 850), (1024, 768), (768, 1024), (390, 844), (360, 740)]
failed: list[str] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    if not passed:
        failed.append(name)


async def idle(page: Page) -> None:
    await page.wait_for_selector("#text-send:not([disabled])", timeout=30000)
    await page.wait_for_timeout(350)


async def say(page: Page, text: str) -> None:
    await idle(page)
    await page.fill("#text-input", text)
    await page.press("#text-input", "Enter")
    await page.wait_for_timeout(400)
    await idle(page)


async def overflow(page: Page) -> bool:
    return bool(await page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"))


async def spilling(page: Page) -> list[str]:
    """Visible text elements whose content is wider than their box, or that poke past the screen."""
    return list(
        await page.evaluate(
            """() => {
              const bad = [];
              const width = window.innerWidth;
              for (const e of document.querySelectorAll('.assistant-text, .transcript, .bubble, .try-phrase, .prompt-chip, .task-row, .app-card, .day-card, .cap, .genui-widget')) {
                const r = e.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (e.scrollWidth > e.clientWidth + 2 && getComputedStyle(e).overflowX !== 'visible') bad.push(e.className + ' scrolls');
                if (r.right > width + 1 || r.left < -1) bad.push(e.className + ' off-screen');
              }
              return bad.slice(0, 5);
            }"""
        )
    )


async def header_ok(page: Page) -> bool:
    """The wordmark is whole, nothing in the header overlaps, and it all fits the screen."""
    return bool(
        await page.evaluate(
            """() => {
              const bar = document.querySelector('.topbar-inner');
              const word = document.querySelector('.wordmark');
              if (word.scrollWidth > word.clientWidth + 1) return false;
              const kids = [...bar.querySelectorAll('.brand, .workflow-title, .demo-badge, .account-button')]
                .filter(e => e.offsetParent).map(e => e.getBoundingClientRect());
              if (kids.some(r => r.right > window.innerWidth + 1 || r.left < -1)) return false;
              for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
                const a = kids[i], b = kids[j];
                if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) return false;
              }
              return true;
            }"""
        )
    )


async def clear_of_composer(page: Page) -> bool:
    """After scrolling to the end, the last content sits above the fixed composer."""
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(300)
    return bool(
        await page.evaluate(
            """() => {
              const composer = document.querySelector('#composer').getBoundingClientRect();
              const last = document.querySelector('#new-session-button').getBoundingClientRect();
              return last.bottom <= composer.top + 1;
            }"""
        )
    )


async def fits(page: Page, selector: str) -> bool:
    return bool(
        await page.evaluate(
            """(s) => { const r = document.querySelector(s).getBoundingClientRect();
              return r.left >= -1 && r.right <= window.innerWidth + 1 && r.top >= -1 && r.bottom <= window.innerHeight + 1; }""",
            selector,
        )
    )


async def tall_enough(page: Page, selector: str, minimum: float) -> bool:
    return bool(
        await page.evaluate(
            """([s, m]) => [...document.querySelectorAll(s)].filter(e => e.offsetParent).every(e => e.getBoundingClientRect().height >= m)""",
            [selector, minimum],
        )
    )


async def main() -> None:
    email = f"design-{uuid4().hex[:8]}@example.com"
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=shutil.which("chromium"))
        errors: list[str] = []
        for width, height in SIZES:
            tag = f"{width}"
            context = await browser.new_context(viewport={"width": width, "height": height})
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and errors.append(m.text))
            await page.goto(BASE)
            await page.wait_for_selector("#try-text")
            await page.wait_for_timeout(700)
            check(
                f"{tag}: header text is not cut off or overlapped",
                await header_ok(page),
                await header_ok(page),
            )
            check(f"{tag}: home has no sideways scroll", not await overflow(page))
            check(
                f"{tag}: home text stays inside its boxes",
                not await spilling(page),
                await spilling(page),
            )
            await page.screenshot(path=f"{OUT}/design-{tag}-home.png")

            status = await page.evaluate(
                """async ([email, password]) => (await fetch('/api/account/register', {method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({email, password, consent: true})})).status""",
                [f"{tag}-{email}", PASSWORD],
            )
            check(f"{tag}: sign up", status == 200, status)
            await page.reload()
            await page.wait_for_selector("#personal-button:not([hidden])")

            # Kannada: the WhatsApp question and its answers stay in the WhatsApp workflow.
            fresh = page.locator("#new-session-button")
            if await fresh.count() and await fresh.is_visible():
                await fresh.click()  # a signed-in person returns to their open conversation
            await page.click('.language-choice[data-lang="kn-IN"]')
            await page.click("#start-button")
            await page.wait_for_selector("#text-input")
            await say(page, "ಅಮ್ಮನಿಗೆ ವಾಟ್ಸಾಪ್ ಮಾಡಿ")
            ask = await page.inner_text("#assistant-text")
            check(f"{tag}: Kannada question is a WhatsApp question", "ಏನು ಹೇಳಲಿ" in ask, ask[:40])
            check(f"{tag}: signed-in header is not cut off or overlapped", await header_ok(page))
            check(
                f"{tag}: workflow title says WhatsApp",
                "WhatsApp" in await page.inner_text("#workflow-title"),
            )
            check(
                f"{tag}: no basket beside a WhatsApp question",
                not await page.is_visible(".cart-panel"),
            )
            await say(page, "ನಾನು ಸುಮಾರು ಹದಿನೈದು ನಿಮಿಷ ತಡವಾಗಿ ಬರುತ್ತೇನೆ ದಯವಿಟ್ಟು ಕಾಯಿರಿ ಎಂದು ಹೇಳಿ")
            await page.wait_for_selector(".genui-w-composer", timeout=20000)
            check(f"{tag}: Kannada: no sideways scroll", not await overflow(page))
            check(
                f"{tag}: Kannada: text not clipped", not await spilling(page), await spilling(page)
            )
            check(f"{tag}: Kannada: composer clear of content", await clear_of_composer(page))
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=f"{OUT}/design-{tag}-kannada-whatsapp.png")

            # Hindi email workflow after the WhatsApp one: nothing from WhatsApp may remain.
            await page.click('.lang-pill[data-lang="hi-IN"]')
            await page.wait_for_selector("#assistant-text")
            await say(page, "जरूरी ईमेल देखो")
            await page.wait_for_selector(".genui-w-email_list", timeout=20000)
            check(
                f"{tag}: email replaces the WhatsApp draft",
                await page.locator(".genui-w-composer").count() == 0,
            )
            check(f"{tag}: Hindi: no sideways scroll", not await overflow(page))
            check(f"{tag}: Hindi: text not clipped", not await spilling(page), await spilling(page))
            await page.screenshot(path=f"{OUT}/design-{tag}-hindi-email.png", full_page=False)

            # A real order still shows the basket, and only then.
            await page.click('.lang-pill[data-lang="en-IN"]')
            await say(page, "Two kilos of rice and one litre of milk")
            await page.wait_for_selector(".cart-panel", state="visible", timeout=10000)
            check(f"{tag}: an order shows the basket", await page.is_visible("#cart-items"))
            check(
                f"{tag}: an order shows no WhatsApp widgets",
                await page.locator(".genui-w-composer").count() == 0
                or not await page.is_visible(".genui-w-composer"),
            )
            check(f"{tag}: order: no sideways scroll", not await overflow(page))
            check(f"{tag}: order: composer clear of content", await clear_of_composer(page))
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=f"{OUT}/design-{tag}-order.png")

            # Tap targets in the composer and header.
            check(
                f"{tag}: mic, input and send are at least 44px tall",
                await tall_enough(page, "#record-button, #text-input, #text-send", 44),
            )
            check(
                f"{tag}: header buttons are at least 36px tall",
                await tall_enough(page, ".account-button", 36),
            )
            check(
                f"{tag}: language pills are at least 30px tall",
                await tall_enough(page, ".lang-pill", 30),
            )

            # Dialogs fit the screen.
            await page.click("#apps-button")
            await page.wait_for_selector(
                "#apps-dialog[open] .status-chip:not([data-state=loading])", timeout=15000
            )
            check(f"{tag}: Apps dialog fits the screen", await fits(page, "#apps-dialog"))
            check(
                f"{tag}: Apps dialog has no sideways scroll",
                not await page.evaluate(
                    "document.querySelector('#apps-dialog').scrollWidth > document.querySelector('#apps-dialog').clientWidth + 1"
                ),
            )
            await page.screenshot(path=f"{OUT}/design-{tag}-apps.png")
            await page.click("#apps-close")
            await page.click("#personal-button")
            await page.wait_for_selector("#myday-dialog[open] .day-card")
            check(f"{tag}: My day dialog fits the screen", await fits(page, "#myday-dialog"))
            await page.screenshot(path=f"{OUT}/design-{tag}-myday.png")
            await page.click("#myday-close")

            # Keyboard: Tab reaches the composer controls and shows where focus is.
            await page.evaluate("document.activeElement && document.activeElement.blur()")
            reached = set()
            for _ in range(60):
                await page.keyboard.press("Tab")
                reached.add(
                    await page.evaluate("document.activeElement && document.activeElement.id")
                )
            check(
                f"{tag}: keyboard reaches the mic and the text box",
                {"record-button", "text-input"} <= reached,
            )

            await page.evaluate(
                """async (password) => fetch('/api/account/delete', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password})})""",
                PASSWORD,
            )
            await context.close()

        # Reduced motion switches the decorative animation off.
        context = await browser.new_context(
            reduced_motion="reduce", viewport={"width": 1366, "height": 850}
        )
        page = await context.new_page()
        await page.goto(BASE)
        await page.wait_for_selector("#try-text")
        duration = await page.evaluate(
            "getComputedStyle(document.querySelector('.hero-art .bar')).animationDuration"
        )
        check(
            "reduced motion: the wave animation is off",
            float(duration.rstrip("s")) < 0.01,
            duration,
        )
        await context.close()
        await browser.close()
    check("no script or page errors", not errors, errors[:2])
    print("\nAll checks passed." if not failed else f"\n{len(failed)} checks FAILED.")
    sys.exit(1 if failed else 0)


asyncio.run(main())
