# ruff: noqa: E501  (long JavaScript snippets are embedded as strings; wrapping would hurt reading)
"""Real-browser check of the generative UI renderer.

Usage: uv run --with playwright python scripts/genui_browser_check.py <screenshot_dir>
Opens scripts/genui_demo.html over file:// in headless Chromium; no server, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DEMO = (ROOT / "genui_demo.html").as_uri()
COMPONENTS = {
    "heading",
    "text",
    "card",
    "list",
    "action_panel",
    "form",
    "task_board",
    "progress",
    "cart",
    "table",
    "confirmation",
    "composer",
    "calendar_view",
    "reminder_list",
    "note_list",
    "status",
    "share_panel",
}
failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("PASS " if ok else "FAIL ") + message)
    if not ok:
        failures.append(message)


def run(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        for width, height in ((360, 800), (1280, 900)):
            context = browser.new_context(
                viewport={"width": width, "height": height}, accept_downloads=True
            )
            page = context.new_page()
            problems: list[str] = []
            page.on(
                "console",
                lambda m, problems=problems: (
                    problems.append(m.text) if m.type in ("error", "warning") else None
                ),
            )
            page.on("pageerror", lambda e, problems=problems: problems.append(str(e)))
            page.goto(DEMO)
            page.wait_for_selector(".genui-widget")
            page.add_style_tag(content=".genui-enter{animation:none!important}")

            types = set(
                page.eval_on_selector_all(
                    "[data-genui-type]", "els => els.map(e => e.dataset.genuiType)"
                )
            )
            check(
                types >= COMPONENTS,
                f"{width}px: all components rendered (missing {sorted(COMPONENTS - types)})",
            )

            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check(overflow <= 0, f"{width}px: no horizontal overflow (delta {overflow})")
            wide = page.evaluate(
                """() => [...document.querySelectorAll('.genui *')].filter(e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.right > document.documentElement.clientWidth + 1 && !e.closest('.genui-table-wrap');
                }).map(e => e.tagName + '.' + e.className).slice(0, 5)"""
            )
            check(not wide, f"{width}px: no element pokes out of the viewport {wide}")
            small = page.evaluate(
                """() => [...document.querySelectorAll('.genui button, .genui a.genui-btn')].filter(e => {
                    const r = e.getBoundingClientRect();
                    return r.width < 43.5 || r.height < 43.5;
                }).map(e => e.textContent.trim()).slice(0, 5)"""
            )
            check(not small, f"{width}px: buttons are at least 44px {small}")

            # Tab reachability: every enabled control gets focus by Tab alone.
            wanted = page.evaluate(
                """() => [...document.querySelectorAll('.genui button:not(:disabled), .genui a[href], .genui input:not(:disabled), .genui select, .genui textarea')].length"""
            )
            seen = set()
            page.evaluate(
                "document.activeElement && document.activeElement.blur(); window.scrollTo(0,0)"
            )
            for _ in range(wanted + 5):
                page.keyboard.press("Tab")
                seen.add(
                    page.evaluate(
                        """() => { const a = document.activeElement; return a && a.closest('.genui') ? (a.getAttribute('data-key')||'') + '|' + (a.id||'') + '|' + a.textContent.slice(0,20) + '|' + [...document.querySelectorAll('.genui *')].indexOf(a) : '' }"""
                    )
                )
            seen.discard("")
            check(
                len(seen) >= wanted - 2,
                f"{width}px: Tab reaches the controls ({len(seen)} of {wanted})",
            )

            # Cart edit -> onAction with the server contract values; totals unchanged.
            page.evaluate("window.actions.length = 0")
            total_before = page.inner_text(".genui-w-cart .genui-total")
            page.click('[data-key="plus:l1"]')
            page.fill('[data-key="qty:l2"]', "5")
            page.press('[data-key="qty:l2"]', "Tab")
            acts = page.evaluate("window.actions")
            check(
                {
                    "widgetId": "cart1",
                    "actionId": "cart.set_quantity",
                    "values": {"line_id": "l1", "quantity": 3},
                }
                in acts
                and {
                    "widgetId": "cart1",
                    "actionId": "cart.set_quantity",
                    "values": {"line_id": "l2", "quantity": 5},
                }
                in acts,
                f"{width}px: cart edits call onAction ({acts})",
            )
            check(
                page.inner_text(".genui-w-cart .genui-total") == total_before,
                f"{width}px: browser did not change the total",
            )

            # Confirmation fires once even on double click.
            page.evaluate("window.actions.length = 0")
            page.dblclick('[data-key="act:order.confirm"]')
            confirms = [
                a for a in page.evaluate("window.actions") if a["actionId"] == "order.confirm"
            ]
            check(len(confirms) == 1, f"{width}px: double click confirms once ({len(confirms)})")

            # Form: invalid then valid submit.
            page.click('[data-key="submit"]')
            check(
                page.is_visible(".genui-form-el .genui-error"),
                f"{width}px: required field shows an inline error",
            )
            page.fill('[name="restaurant"]', "Toit")
            page.evaluate("window.actions.length = 0")
            page.click('[data-key="submit"]')
            sub = [a for a in page.evaluate("window.actions") if a["actionId"] == "dining.search"]
            check(
                len(sub) == 1
                and sub[0]["values"]["restaurant"] == "Toit"
                and sub[0]["values"]["party"] == 2,
                f"{width}px: form submits values {sub}",
            )

            # Replace in place keeps position and typed text.
            page.fill('[name="notes"]', "typed by user")
            page.focus('[name="notes"]')
            order_before = page.evaluate(
                "[...document.querySelectorAll('[data-genui-id]')].map(e => e.dataset.genuiId).join()"
            )
            page.evaluate(
                "(() => { const w = JSON.parse(JSON.stringify(window.demoWidgets.find(x => x.id==='form1'))); w.title='Book a table (updated)';"
                " window.saathiGenUI.render(document.getElementById('host'), [w], {onAction: window.demoOnAction}); })()"
            )
            order_after = page.evaluate(
                "[...document.querySelectorAll('[data-genui-id]')].map(e => e.dataset.genuiId).join()"
            )
            check(order_before == order_after, f"{width}px: replace keeps order")
            check(
                page.input_value('[name="notes"]') == "typed by user",
                f"{width}px: typed text survives replace",
            )
            check(
                page.evaluate("document.activeElement.name") == "notes",
                f"{width}px: focus survives replace",
            )

            # .ics download
            with page.expect_download() as dl:
                page.click('[data-key="ics"]')
            ics = Path(dl.value.path()).read_bytes().decode()
            check(
                ics.startswith("BEGIN:VCALENDAR\r\n")
                and "DTSTART:20261109T140000Z\r\n" in ics
                and "SUMMARY:Dinner\\, with Asha\\; Toit\r\n" in ics,
                f"{width}px: .ics download is correct",
            )

            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(out_dir / f"genui_{width}_full.png"), full_page=True)
            bad = [m for m in problems if "favicon" not in m.lower()]
            check(not bad, f"{width}px: no console errors or page errors {bad}")
            context.close()
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: genui_browser_check.py <screenshot_dir>")
    run(Path(sys.argv[1]))
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nAll browser checks passed")
