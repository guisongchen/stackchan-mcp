"""TTSCore TTS engine — calls local TTSCore HTTP API over Unix socket."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp

from .audio_utils import DEVICE_SAMPLE_RATE, resample_pcm16_linear, wav_to_pcm16_mono
from .base import TTSEngine

SOCKET_PATH = "/tmp/tts_core.sock"
DEFAULT_MODEL_NAME = "Qwen3-TTS-12Hz-0.6B-CustomVoice"

logger = logging.getLogger(__name__)


def _detect_language(text: str) -> str:
    """Pick a TTSCore language value from text content.

    TTSCore supports: auto, chinese, english, french, german, italian,
    japanese, korean, portuguese, russian, spanish.  We default to auto
    and only pin chinese when the text contains CJK characters, since
    that is the most common non-auto case for this device.
    """
    for ch in text:
        if "一" <= ch <= "鿿":
            return "chinese"
    return "auto"


class TTSCoreEngine(TTSEngine):
    name = "ttscore"

    def __init__(self, socket_path: str = SOCKET_PATH) -> None:
        self._socket_path = socket_path

    async def _ensure_loaded(self, session: aiohttp.ClientSession) -> None:
        """Ask TTSCore to load its default model if it is not ready yet.

        TTSCore may be started with the model unloaded. A single /load
        call is issued when status is not 'loaded'; if the service is
        already loading we wait briefly for it to finish. Synthesis is
        only attempted once the model reports 'loaded'.
        """
        async with session.get(
            "http://localhost/status",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"TTSCore status returned {resp.status}: {body[:500]}")
            status = await resp.json()

        state = status.get("state")
        if state == "loaded":
            return

        if state == "unloaded":
            async with session.post(
                "http://localhost/load",
                json={"model_name": DEFAULT_MODEL_NAME},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"TTSCore load returned {resp.status}: {body[:500]}")

        # Poll until loaded (or error). Back off modestly; model load can
        # take a few seconds on first use.
        for _ in range(60):
            async with session.get("http://localhost/status") as resp:
                status = await resp.json()
            state = status.get("state")
            if state == "loaded":
                return
            if state == "error":
                error_message = status.get("error_message") or "unknown error"
                raise RuntimeError(f"TTSCore model failed to load: {error_message}")
            await asyncio.sleep(0.5)

        raise RuntimeError("TTSCore model did not become loaded in time")

    async def synthesize(self, text: str, **opts: Any) -> bytes:
        if not text:
            raise ValueError("ttscore synthesize: empty text")

        language = opts.get("language")
        if not isinstance(language, str) or not language:
            language = _detect_language(text)

        speaker_name = opts.get("speaker_name")
        speaker = speaker_name if isinstance(speaker_name, str) and speaker_name else "Serena"

        connector = aiohttp.UnixConnector(path=self._socket_path)
        async with aiohttp.ClientSession(connector=connector) as session:
            await self._ensure_loaded(session)

            payload: dict[str, Any] = {
                "text": text,
                "language": language,
                "speaker": speaker,
            }
            async with session.post(
                "http://localhost/synthesize",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"TTSCore returned {resp.status}: {body[:500]}")
                result = await resp.json()

            audio_path = result.get("audio_path")
            if not audio_path:
                raise RuntimeError("TTSCore response missing audio_path")

        wav_path = Path(audio_path)
        if not wav_path.exists():
            raise RuntimeError(f"TTSCore audio file not found: {audio_path}")
        wav_bytes = wav_path.read_bytes()
        wav_path.unlink(missing_ok=True)

        sample_rate, pcm = wav_to_pcm16_mono(wav_bytes)
        if sample_rate != DEVICE_SAMPLE_RATE:
            pcm = resample_pcm16_linear(pcm, sample_rate, DEVICE_SAMPLE_RATE)
        return pcm
