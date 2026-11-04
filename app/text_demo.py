"""Interactive Gemini-to-MockInstamart text demo."""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from app.commerce.mock import MockInstamart
from app.llm.extractor import GeminiIntentExtractor, IntentExtractionError
from app.orchestrator.state_machine import CallStarted, Effect, Say
from app.orchestrator.turn_runner import TextTurnRunner


async def _run() -> None:
    load_dotenv()
    try:
        extractor = GeminiIntentExtractor.from_env()
    except IntentExtractionError as exc:
        print(f"Cannot start demo: {exc}")
        return
    runner = await TextTurnRunner.create(MockInstamart(), extractor=extractor)
    _print_effects(await runner.dispatch(CallStarted(user_known=True)))
    print("Type 'quit' to exit. This demo uses MockInstamart checkout.")
    while True:
        text = input("You: ").strip()
        if text.casefold() in {"quit", "exit"}:
            return
        try:
            _print_effects(await runner.dispatch_text(text))
        except Exception as exc:
            print(f"Assistant: I could not complete that turn safely ({type(exc).__name__}).")


def _print_effects(effects: list[Effect]) -> None:
    for effect in effects:
        if isinstance(effect, Say):
            if effect.template == "intent_error":
                message = effect.data.get("message") or "I could not understand that request."
                print(f"Assistant: {message}")
                continue
            if effect.data:
                print(f"Assistant: {effect.template} — {effect.data}")
            else:
                print(f"Assistant: {effect.template}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
