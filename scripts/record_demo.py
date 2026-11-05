"""Record the demo: a real browser, real Gnani voice replies, and mock shops only.

    uv run --with playwright python scripts/record_demo.py out.mp4 [--chromium /usr/bin/chromium]

It starts its own server with COMMERCE_BACKEND=mock, types each request (captioned on screen),
plays Saathi's real voice replies into the soundtrack, and states on screen that nothing is real.
It cannot reach Swiggy: mock mode never constructs a Swiggy client.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import Page, Response, async_playwright

PORT = 18765
SAMPLE_BYTES_PER_SECOND = 48000 * 2
OPENING = (
    "Namma Saathi: your personal AI for everyday life.<br><br>"
    "This recording uses built-in mock shops.<br>"
    "No real order or booking is placed."
)
CLOSING = (
    "One assistant, three services, one safety rule:<br>"
    "nothing happens until you hear the details and say yes.<br><br>"
    "Mock shops in this video. The same code has read live Swiggy data."
)
ENGLISH = (
    ("Groceries: Saathi asks which pack", "I need rice and milk"),
    ("Groceries: Saathi asks which pack", "First"),
    ("Groceries: Saathi reads back items, total, address, payment", "First"),
    (
        "Change of mind: the groceries wait",
        "Book a table for two in Indiranagar tomorrow at 7 PM, Italian",
    ),
    ("Table: Saathi offers restaurants", "First"),
    ("Table: a yes only books the table", "Yes"),
    ("Back to the waiting task: a fresh readback", "Back to my groceries"),
    ("Groceries: the yes applies to what was just read back", "Yes"),
    ("A meal: Saathi asks what you feel like", "I want something for dinner"),
    ("A meal: Saathi offers restaurant choices", "Chicken biryani"),
    ("A meal: Saathi reads it back", "The second one"),
    ("A meal: explicit yes", "Yes"),
)
KANNADA = (
    ("Kannada: the same assistant", "ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು"),
    ("Kannada: explicit yes", "ಹೌದು"),
)
OVERLAY = """
() => {
  const style = document.createElement('style');
  style.textContent = `
    #demo-caption { position: fixed; left: 0; right: 0; bottom: 0; z-index: 99; padding: 14px 24px;
      background: #1f1a12; color: #fff8ec; font: 20px system-ui; }
    #demo-caption small { display: block; color: #e8c98f; font-size: 15px; margin-bottom: 2px; }
    #demo-card { position: fixed; inset: 0; z-index: 100; display: flex; align-items: center;
      justify-content: center; text-align: center; padding: 48px; background: #fff8ec;
      color: #1f1a12; font: 32px/1.4 system-ui; }
    .composer { bottom: 64px; }
    .conversation { padding-bottom: 230px; }`;
  document.head.append(style);
}
"""


async def card(page: Page, html: str, seconds: float) -> None:
    await page.evaluate(
        "(html) => { const c = document.createElement('div'); c.id = 'demo-card'; "
        "c.innerHTML = html; document.body.append(c); }",
        html,
    )
    await asyncio.sleep(seconds)
    await page.evaluate("() => document.querySelector('#demo-card').remove()")


async def caption(page: Page, label: str, text: str) -> None:
    await page.evaluate(
        "([label, text]) => { let c = document.querySelector('#demo-caption');"
        "if (!c) { c = document.createElement('div'); c.id = 'demo-caption'; "
        "document.body.append(c); }"
        "c.replaceChildren(); const s = document.createElement('small'); s.textContent = label;"
        "c.append(s, 'You: ' + text); }",
        [label, text],
    )


async def start_session(page: Page, language: str) -> None:
    await page.goto(f"http://localhost:{PORT}/?typed=1")
    await page.evaluate("() => sessionStorage.clear()")
    await page.reload()
    await page.evaluate(OVERLAY)
    await page.click(f'[data-lang="{language}"]')
    await page.wait_for_function("() => !document.querySelector('#start-button').disabled")


async def focus_latest(page: Page) -> None:
    """Keep the panel that changed on screen, as a person would while watching."""
    await page.evaluate(
        "() => { const order = ['#order-panel', '#confirmation-panel', '#options-panel'];"
        "for (const id of order) { const n = document.querySelector(id);"
        "if (n && !n.hidden) { n.scrollIntoView({behavior: 'smooth', block: 'center'}); return; } }"
        "document.querySelector('#tasks-panel')"
        ".scrollIntoView({behavior: 'smooth', block: 'start'}); }"
    )


async def record(args: argparse.Namespace, workdir: Path) -> None:
    frames = workdir / "frames"
    frames.mkdir()
    sounds: list[tuple[float, Path, float]] = []
    timeline: list[tuple[float, Path]] = []
    started = time.monotonic()

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            executable_path=args.chromium,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        # The captions are injected styles, which the page's own security policy would block.
        page = await browser.new_page(viewport={"width": 1280, "height": 800}, bypass_csp=True)
        cdp = await page.context.new_cdp_session(page)

        async def on_frame(event: dict[str, object]) -> None:
            path = frames / f"{len(timeline):06}.jpg"
            path.write_bytes(base64.b64decode(str(event["data"])))
            timeline.append((time.monotonic() - started, path))
            await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

        cdp.on("Page.screencastFrame", lambda event: asyncio.ensure_future(on_frame(event)))
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 85,
                "everyNthFrame": 1,
                "maxWidth": 1280,
                "maxHeight": 800,
            },
        )

        pending: list[asyncio.Task[None]] = []

        async def keep_audio(response: Response) -> None:
            if "/api/session" not in response.url or response.request.method != "POST":
                return
            body = await response.json()
            audio = body.get("audio_base64")
            if not audio:
                return
            raw = base64.b64decode(audio)
            path = workdir / f"reply{len(sounds):03}.wav"
            start = raw.index(b"data") + 8
            path.write_bytes(raw[start:])
            length = (len(raw) - start) / SAMPLE_BYTES_PER_SECOND
            sounds.append((time.monotonic() - started, path, length))

        page.on("response", lambda r: pending.append(asyncio.ensure_future(keep_audio(r))))

        async def speak_out() -> None:
            """Wait for the reply to be heard in full, as a viewer must."""
            await asyncio.gather(*pending)
            if sounds:
                at, _, length = sounds[-1]
                await asyncio.sleep(max(at + length - (time.monotonic() - started), 0) + 0.8)

        async def turn(label: str, text: str) -> None:
            await caption(page, label, text)
            await page.fill("#text-input", text)
            await asyncio.sleep(1.2)
            before = len(sounds)
            await page.click("#text-send")
            await page.wait_for_function("() => !document.querySelector('#text-send').disabled")
            await focus_latest(page)
            await asyncio.gather(*pending)
            if len(sounds) > before:
                await speak_out()

        await start_session(page, "en-IN")
        await card(page, OPENING, 4.5)
        await caption(page, "One assistant", "Tap to start")
        await asyncio.sleep(1.0)
        await page.click("#start-button")
        await page.wait_for_selector("#conversation:not([hidden])")
        await speak_out()
        for label, text in ENGLISH:
            await turn(label, text)

        await start_session(page, "kn-IN")
        await asyncio.sleep(0.5)
        await page.click("#start-button")
        await page.wait_for_selector("#conversation:not([hidden])")
        await speak_out()
        for label, text in KANNADA:
            await turn(label, text)

        await card(page, CLOSING, 5.0)
        ended = time.monotonic() - started
        await cdp.send("Page.stopScreencast")
        await browser.close()

    stitch(timeline, sounds, ended, args.output, workdir)


def stitch(
    timeline: list[tuple[float, Path]],
    sounds: list[tuple[float, Path, float]],
    ended: float,
    output: Path,
    workdir: Path,
) -> None:
    listing = workdir / "frames.txt"
    # Frames arrive in bursts, and a short duration in the list gets rounded up, so the picture
    # falls behind the voice. Sampling a fixed grid keeps both on the same clock.
    step = 0.08
    with listing.open("w") as handle:
        index = 0
        for tick in range(int(ended / step) + 1):
            while index + 1 < len(timeline) and timeline[index + 1][0] <= tick * step:
                index += 1
            handle.write(f"file '{timeline[index][1]}'\nduration {step:.2f}\n")
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
    ]
    filters: list[str] = []
    for index, (at, path, _) in enumerate(sounds):
        command += ["-f", "s16le", "-ar", "48000", "-ac", "1", "-i", str(path)]
        filters.append(f"[{index + 1}:a]adelay={int(at * 1000)}:all=1[a{index}]")
    mixed = "".join(f"[a{index}]" for index in range(len(sounds)))
    filters.append(f"{mixed}amix=inputs={len(sounds)}:normalize=0[out]")
    command += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "0:v",
        "-map",
        "[out]",
        "-vf",
        "fps=15,format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-t",
        f"{ended:.2f}",
        str(output),
    ]
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chromium", default=shutil.which("chromium") or "chromium")
    args = parser.parse_args()
    load_dotenv()
    environment = {
        **os.environ,
        "COMMERCE_BACKEND": "mock",
        "SWIGGY_LIVE_ACCESS": "false",
        "ALLOW_SWIGGY_CART_WRITES": "false",
        "ALLOW_REAL_CHECKOUT": "false",
        "ALLOW_REAL_BOOKING": "false",
        "DEMO_UNDERSTANDING": "fallback",
        "GNANI_TTS_SPEED": "1.10",  # Keeps the video short; the app's default is slower.
    }
    server = subprocess.Popen(
        ["uv", "run", "uvicorn", "app.web:app", "--port", str(PORT), "--log-level", "warning"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://localhost:{PORT}/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        with tempfile.TemporaryDirectory(prefix="saathi-record-") as folder:
            asyncio.run(record(args, Path(folder)))
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
