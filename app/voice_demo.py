"""Turn-based audio-file demo using Gnani REST and MockInstamart."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from app.commerce.mock import MockInstamart
from app.config import load_settings
from app.llm.extractor import GeminiIntentExtractor, IntentExtractionError
from app.orchestrator.state_machine import CallStarted
from app.orchestrator.turn_runner import TextTurnRunner
from app.voice.gnani import DEFAULT_VOICES, GnaniError, PrismaSTT, TimbreTTS
from app.voice.presentation import response_text


async def run(audio_paths: list[Path], output_dir: Path, language: str) -> None:
    load_dotenv()
    settings = load_settings()
    if not settings.gnani_api_key:
        raise GnaniError("configuration", "Set GNANI_API_KEY in .env")
    stt = PrismaSTT(
        settings.gnani_api_key,
        language=language,
        timeout=settings.gnani_timeout_seconds,
    )
    tts = TimbreTTS(
        settings.gnani_api_key,
        voice=settings.gnani_tts_voice or DEFAULT_VOICES[language],
        language=language,
        model=settings.gnani_tts_model,
        speed=settings.gnani_tts_speed,
        timeout=settings.gnani_timeout_seconds,
    )
    extractor = GeminiIntentExtractor.from_env()
    runner = await TextTurnRunner.create(MockInstamart(), extractor=extractor)
    await runner.dispatch(CallStarted(user_known=True, language=language))
    output_dir.mkdir(parents=True, exist_ok=True)

    pending_paths = list(audio_paths)
    turn = 0
    while True:
        if pending_paths:
            audio_path = pending_paths.pop(0)
        else:
            entered = input("Audio file path (or 'quit'): ").strip()
            if entered.casefold() in {"quit", "exit"}:
                break
            audio_path = Path(entered)
        try:
            transcript = await stt.transcribe(audio_path)
            print(f"You said: {transcript.text}")
            effects = await runner.dispatch_text(transcript.text)
            message = response_text(effects, language=language, commerce_mode="mock")
            print(f"Saathi: {message}")
            if message:
                turn += 1
                wav_path = await tts.synthesize(message, output_dir / f"response-{turn:03d}.wav")
                print(f"Audio: {wav_path}")
        except (GnaniError, IntentExtractionError) as exc:
            print(f"Turn failed ({getattr(exc, 'category', 'intent')}): {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Namma Saathi Gnani REST voice demo")
    parser.add_argument("audio", nargs="*", type=Path, help="Initial audio file turn(s)")
    parser.add_argument("--output-dir", type=Path, default=Path("voice-output"))
    parser.add_argument(
        "--language",
        choices=("kn-IN", "hi-IN", "en-IN"),
        required=True,
        help="Language for this conversation and its Gnani speech adapters",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args.audio, args.output_dir, args.language))
    except (GnaniError, IntentExtractionError) as exc:
        print(f"Cannot start voice demo: {exc}")


if __name__ == "__main__":
    main()
