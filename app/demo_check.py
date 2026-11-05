"""Rehearse the demo against real speech and LLM services, on mock shops only.

    uv run python -m app.demo_check            # typed turns, real LLM and real voice replies
    uv run python -m app.demo_check --voice    # also speaks each request through real recognition

It always forces COMMERCE_BACKEND=mock, so it cannot reach Swiggy. It prints what each stage cost
and whether each journey finished. Run it right before recording.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import httpx
from dotenv import load_dotenv

from app.config import load_settings
from app.voice.gnani import DEFAULT_VOICES, TimbreTTS
from app.web import create_app

JOURNEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "en-IN": {
        "Groceries": ("Two kilos of rice and one litre of milk", "Yes"),
        "Meal": ("I want something for dinner", "Chicken biryani", "The second one", "Yes"),
        "Table": ("Book a table for two in Indiranagar tomorrow at seven, Italian", "First", "Yes"),
    },
    "hi-IN": {
        "Groceries": ("दो किलो चावल और एक लीटर दूध", "हाँ"),
        "Table": ("कल सात बजे इंदिरानगर में दो लोगों के लिए इटालियन टेबल बुक करो", "पहला", "हाँ"),
    },
    "kn-IN": {
        "Groceries": ("ಎರಡು ಕೆಜಿ ಅಕ್ಕಿ ಮತ್ತು ಒಂದು ಲೀಟರ್ ಹಾಲು", "ಹೌದು"),
        "Table": ("ನಾಳೆ ಏಳು ಗಂಟೆಗೆ ಇಂದಿರಾನಗರದಲ್ಲಿ ಇಬ್ಬರಿಗೆ ಇಟಾಲಿಯನ್ ಟೇಬಲ್ ಬುಕ್ ಮಾಡಿ", "ಮೊದಲನೆಯದು", "ಹೌದು"),
    },
}
STAGES = ("stt", "understanding", "provider", "orchestration", "speech", "total")


def repaired_wav(audio: bytes) -> bytes:
    """Timbre streams its WAV, so the header claims an endless length. Write the true one."""
    start = audio.index(b"data") + 8
    body = audio[start:]
    fixed = bytearray(audio[:start])
    struct.pack_into("<I", fixed, 4, len(fixed) - 8 + len(body))
    struct.pack_into("<I", fixed, start - 4, len(body))
    return bytes(fixed) + body


async def spoken(text: str, language: str, key: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="saathi-check-") as folder:
        tts = TimbreTTS(key, voice=DEFAULT_VOICES[language], language=language, timeout=20)
        path = await tts.synthesize(text, Path(folder) / "said.wav")
        return repaired_wav(path.read_bytes())


@dataclass
class Row:
    label: str
    state: str
    by: str
    timings: dict[str, int]


async def run_journey(
    client: httpx.AsyncClient, language: str, turns: tuple[str, ...], *, voice: bool, key: str
) -> tuple[list[Row], bool]:
    started = await client.post("/api/sessions", json={"language": language})
    started.raise_for_status()
    session = cast(str, started.json()["session_id"])
    rows: list[Row] = []
    state = ""
    heard: dict[str, str] = {}
    for text in turns:
        headers = {"X-Session-ID": session, **heard}
        if voice:
            response = await client.post(
                "/api/session/turn",
                content=await spoken(text, language, key),
                headers={**headers, "Content-Type": "audio/wav"},
            )
        else:
            response = await client.post("/api/session/text", json={"text": text}, headers=headers)
        if response.status_code != 200:
            rows.append(Row(text, f"HTTP {response.status_code}", "", {}))
            return rows, False
        body = response.json()
        state = body["state"]
        if body.get("can_confirm"):
            heard = {
                "X-Heard-Cart-Version": str(body["cart_version"]),
                "X-Heard-Cart-Hash": body["cart_hash"],
            }
        rows.append(Row(text, state, body.get("understood_by") or "direct", body["timings_ms"]))
        # A real silent gap between turns keeps a free-tier rate limit from ending the rehearsal.
        await asyncio.sleep(4)
    return rows, state == "DONE"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", action="store_true", help="use real speech recognition too")
    parser.add_argument("--language", choices=sorted(JOURNEYS), help="only this language")
    arguments = parser.parse_args()
    load_dotenv()
    environment = {**os.environ, "COMMERCE_BACKEND": "mock", "SWIGGY_LIVE_ACCESS": "false"}
    environment.update(
        ALLOW_SWIGGY_CART_WRITES="false", ALLOW_REAL_CHECKOUT="false", ALLOW_REAL_BOOKING="false"
    )
    settings = replace(load_settings(environment))
    application = create_app(settings=settings)
    failed = 0
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            for language, journeys in JOURNEYS.items():
                if arguments.language not in (None, language):
                    continue
                for name, turns in journeys.items():
                    rows, finished = await run_journey(
                        client,
                        language,
                        turns,
                        voice=arguments.voice,
                        key=settings.gnani_api_key or "",
                    )
                    print(f"\n{language} {name}: {'FINISHED' if finished else 'FAILED'}")
                    print(f"{'ms: ' + ' '.join(f'{s:>7.7}' for s in STAGES)}  state, understood by")
                    for row in rows:
                        cells = " ".join(f"{row.timings.get(s, 0):>7}" for s in STAGES)
                        print(f"    {cells}  {row.state}, {row.by}")
                    failed += 0 if finished else 1
    print(f"\n{'All journeys finished.' if not failed else f'{failed} journeys failed.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
