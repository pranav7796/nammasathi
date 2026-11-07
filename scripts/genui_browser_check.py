# ruff: noqa: E501  (long JavaScript snippets are embedded as strings; wrapping would hurt reading)
"""Real-browser check of the generative UI renderer.

Usage: uv run --with playwright python scripts/genui_browser_check.py <screenshot_dir>
Opens scripts/genui_demo.html over file:// in headless Chromium; no server, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

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
    "email_list",
    "event_preview",
    "day_planner",
    "memory_list",
}
NEW_WIDGETS = (
    "tasks1",
    "emails1",
    "event.e1",
    "event.e2",
    "event.e3",
    "planner1",
    "memory1",
    "comp1",
    "comp2",
    "comp3",
    "comp4",
)
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

            for wid in NEW_WIDGETS:
                page.locator(f'[data-genui-id="{wid}"]').screenshot(
                    path=str(out_dir / f"genui_{width}_{wid.replace('.', '_')}.png")
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
            for _ in range(wanted * 2 + 10):  # date and time inputs have several tab stops each:
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

            def posted(action_id: str, page: Any = page) -> list[dict[str, Any]]:
                return [a for a in page.evaluate("window.actions") if a["actionId"] == action_id]

            # Task board: approve fires once and rests; view and edit post their ids.
            page.evaluate("window.actions.length = 0")
            page.dblclick('[data-key="approve:k4"]')
            page.click('[data-key="view:k4"]')
            page.click('[data-key="edit:k4"]')
            check(
                len(posted("task.approve:k4")) == 1
                and posted("task.approve:k4")[0]["values"] == {"task_id": "k4"}
                and len(posted("task.view:k4")) == 1
                and len(posted("task.edit:k4")) == 1,
                f"{width}px: task approve/view/edit post once ({page.evaluate('window.actions')})",
            )
            check(
                page.is_disabled('[data-key="approve:k4"]'),
                f"{width}px: Approve rests after one click",
            )

            # Email list.
            page.evaluate("window.actions.length = 0")
            page.click('[data-key="open:18c3F_a1"]')
            page.click('[data-key="reply:18c3F_a1"]')
            check(
                len(posted("email.open:18c3F_a1")) == 1
                and len(posted("email.reply:18c3F_a1")) == 1,
                f"{width}px: email open and reply post their ids",
            )
            check(
                "min ago" in page.inner_text('[data-genui-id="emails1"]')
                and "Unread" in page.inner_text('[data-genui-id="emails1"]'),
                f"{width}px: emails show relative time and text chips",
            )

            # Event preview: edit then approve posts the edited values once.
            page.evaluate("window.actions.length = 0")
            page.fill('[data-genui-id="event.e1"] [name="title"]', "Dinner at Toit")
            page.fill('[data-genui-id="event.e1"] [name="minutes"]', "120")
            page.fill(
                '[data-genui-id="event.e1"] [name="attendees"]',
                "asha@example.com\n\nkiran@example.com\n",
            )
            page.dblclick('[data-genui-id="event.e1"] [data-key="approve"]')
            ev = posted("task.approve:e1")
            check(
                len(ev) == 1
                and ev[0]["values"]
                == {
                    "title": "Dinner at Toit",
                    "date": "2026-11-09",
                    "start": "19:30",
                    "minutes": 120,
                    "attendees": ["asha@example.com", "kiran@example.com"],
                },
                f"{width}px: event approve posts edited values {ev}",
            )
            check(
                page.locator('[data-genui-id="event.e2"] input').count() == 0,
                f"{width}px: created event is read-only",
            )

            # Day planner.
            page.evaluate("window.actions.length = 0")
            page.click('[data-key="toggle:p2"]')
            page.select_option('[data-key="priority:p3"]', "high")
            page.click('[data-key="down:p2"]')
            page.fill('[data-genui-id="planner1"] [name="title"]', "Call Ajji")
            page.fill('[data-genui-id="planner1"] [name="time"]', "18:00")
            page.click('[data-genui-id="planner1"] [data-key="add"]')
            check(
                posted("plan.toggle:p2")
                and posted("plan.priority:p3")[0]["values"] == {"priority": "high"}
                and posted("plan.move:p2")[0]["values"] == {"direction": "down"}
                and posted("plan.add")[0]["values"] == {"title": "Call Ajji", "time": "18:00"},
                f"{width}px: planner actions {page.evaluate('window.actions')}",
            )

            # Memory.
            page.evaluate("window.actions.length = 0")
            page.fill('[name="value_diet"]', "Eggetarian")
            page.click('[data-key="save:diet"]')
            page.click('[data-key="forget:home_area"]')
            check(
                posted("memory.save")[0]["values"] == {"key": "diet", "value": "Eggetarian"}
                and len(posted("memory.forget:home_area")) == 1,
                f"{width}px: memory save and forget {page.evaluate('window.actions')}",
            )

            # Composer: Send fires once; states carry text.
            page.evaluate("window.actions.length = 0")
            page.fill('[data-genui-id="comp1"] [name="body"]', "Edited body")
            page.dblclick('[data-genui-id="comp1"] [data-key="send"]')
            sent = posted("task.approve:c1")
            check(
                len(sent) == 1 and sent[0]["values"]["body"] == "Edited body",
                f"{width}px: composer Send fires once with edited values",
            )
            texts = {
                wid: page.inner_text(f'[data-genui-id="{wid}"]')
                for wid in ("comp2", "comp3", "comp4")
            }
            check(
                "Sent" in texts["comp2"]
                and "Delivered to Gmail" in texts["comp2"]
                and "Copied, not sent" in texts["comp3"]
                and "Failed" in texts["comp4"],
                f"{width}px: composer states carry text",
            )

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
