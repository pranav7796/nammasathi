"""REST adapters for Gnani Prisma STT and Timbre TTS.

Contracts: https://docs.gnani.ai/api/STT/speech-to-text and
https://docs.gnani.ai/api/TTS/tts-inference. Timbre v2.5 voices have a
preferred language; see https://docs.gnani.ai/api/TTS/available-voices.
"""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

STT_ENDPOINT = "https://api.vachana.ai/stt/v3"
TTS_ENDPOINT = "https://api.vachana.ai/api/v1/tts/inference"
SUPPORTED_LANGUAGES = {"kn-IN", "hi-IN", "en-IN"}
# Each Timbre v2.5 voice has one preferred language (see available-voices above).
DEFAULT_VOICES = {"kn-IN": "Saanvi", "hi-IN": "Nalini", "en-IN": "Kaveri"}
SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a"}
MAX_AUDIO_SECONDS = 30  # Official REST transcription limit (not the task's proposed 60).


class GnaniError(Exception):
    """Sanitized Gnani API error with a stable category for CLI handling."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class PrismaTranscript:
    text: str
    language: str


class PrismaSTT:
    def __init__(self, api_key: str, *, language: str, timeout: float = 60) -> None:
        if not api_key:
            raise GnaniError("configuration", "GNANI_API_KEY is not configured")
        _validate_language(language)
        self._api_key = api_key
        self.language = language
        self.timeout = timeout

    async def transcribe(self, path: str | Path) -> PrismaTranscript:
        audio_path = _validate_audio_path(path)
        _validate_wav_duration(audio_path)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                with audio_path.open("rb") as audio:
                    response = await client.post(
                        STT_ENDPOINT,
                        headers={"X-API-Key-ID": self._api_key},
                        files={"audio_file": (audio_path.name, audio, _mime_type(audio_path))},
                        data={"language_code": self.language, "format": "transcribe"},
                    )
        except httpx.TimeoutException as exc:
            raise GnaniError("timeout", "Prisma transcription timed out") from exc
        except httpx.RequestError as exc:
            raise GnaniError("connection", "Could not connect to Prisma") from exc
        _raise_for_response(response, "Prisma")
        try:
            payload_value: object = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise GnaniError("invalid_response", "Prisma returned invalid JSON") from exc
        if not isinstance(payload_value, dict):
            raise GnaniError("transcription", "Prisma did not return a successful transcript")
        payload = cast(dict[str, object], payload_value)
        if payload.get("success") is not True:
            raise GnaniError("transcription", "Prisma did not return a successful transcript")
        transcript = payload.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise GnaniError("invalid_audio", "Prisma returned no speech transcript")
        return PrismaTranscript(transcript.strip(), self.language)


class TimbreTTS:
    def __init__(
        self,
        api_key: str,
        *,
        voice: str,
        language: str,
        model: str = "timbre-v2.5",
        speed: float = 0.90,
        timeout: float = 60,
    ) -> None:
        if not api_key:
            raise GnaniError("configuration", "GNANI_API_KEY is not configured")
        if not voice.strip():
            raise GnaniError("configuration", "GNANI_TTS_VOICE must be configured")
        _validate_language(language)
        if model != "timbre-v2.5":
            raise GnaniError("configuration", "GNANI_TTS_MODEL must be timbre-v2.5")
        if not 0.85 <= speed <= 1.15:
            raise GnaniError("configuration", "Timbre speed must be between 0.85 and 1.15")
        self._api_key = api_key
        self.voice = voice
        self.language = language
        self.model = model
        self.speed = speed
        self.timeout = timeout

    async def synthesize(self, text: str, output_path: str | Path) -> Path:
        if not text.strip():
            raise GnaniError("invalid_request", "Cannot synthesize empty text")
        body = {
            "text": text,
            "model": self.model,
            "voice": self.voice,
            "language": self.language,
            "speed": self.speed,
            "audio_config": {
                "encoding": "linear_pcm",
                "container": "wav",
                "num_channels": 1,
                "sample_rate": 48000,
                "sample_width": 2,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    TTS_ENDPOINT,
                    headers={"X-API-Key-ID": self._api_key},
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise GnaniError("timeout", "Timbre synthesis timed out") from exc
        except httpx.RequestError as exc:
            raise GnaniError("connection", "Could not connect to Timbre") from exc
        _raise_for_response(response, "Timbre")
        audio = response.content
        _validate_wav(audio)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(audio)
        return destination


def _validate_language(language: str) -> None:
    if language not in SUPPORTED_LANGUAGES:
        raise GnaniError("configuration", "Language must be kn-IN, hi-IN, or en-IN")


def _validate_audio_path(path: str | Path) -> Path:
    audio_path = Path(path)
    if not audio_path.is_file():
        raise GnaniError("invalid_audio", "Audio file does not exist")
    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        raise GnaniError("invalid_audio", "Audio format must be WAV, MP3, OGG, FLAC, AAC, or M4A")
    if audio_path.stat().st_size == 0:
        raise GnaniError("invalid_audio", "Audio file is empty")
    return audio_path


def _validate_wav_duration(path: Path) -> None:
    if path.suffix.lower() != ".wav":
        return  # Gnani validates duration for other documented codecs server-side.
    try:
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
    except (wave.Error, ZeroDivisionError) as exc:
        raise GnaniError("invalid_audio", "WAV audio file is invalid") from exc
    if duration > MAX_AUDIO_SECONDS:
        raise GnaniError("invalid_audio", "Audio must be 30 seconds or shorter")


def _mime_type(path: Path) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }[path.suffix.lower()]


def _raise_for_response(response: httpx.Response, service: str) -> None:
    if response.is_success:
        return
    status = response.status_code
    if status in {401, 403}:
        category, message = (
            "authentication",
            f"{service} authentication failed; check GNANI_API_KEY",
        )
    elif status == 400 or status == 422:
        category, message = "invalid_audio", f"{service} rejected the request or audio"
    elif status == 429:
        category, message = "rate_limit", f"{service} rate limit reached; try again later"
    elif status in {500, 502, 503, 504}:
        category, message = "service", f"{service} is temporarily unavailable"
    else:
        category, message = "api", f"{service} request failed with HTTP {status}"
    raise GnaniError(category, message)


def _validate_wav(audio: bytes) -> None:
    try:
        import io

        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getnframes() <= 0:
                raise GnaniError("invalid_response", "Timbre WAV must be nonempty mono 16-bit PCM")
    except (wave.Error, EOFError) as exc:
        raise GnaniError("invalid_response", "Timbre did not return a valid WAV file") from exc
