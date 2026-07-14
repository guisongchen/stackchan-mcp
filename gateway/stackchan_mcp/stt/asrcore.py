"""ASRCore STT engine — calls local ASRCore HTTP API over Unix socket."""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from .base import STTEngine

DEVICE_SAMPLE_RATE = 16000
SOCKET_PATH = "/tmp/asr_core.sock"
DEFAULT_MODEL_NAME = "qwen3-asr-0.6b"

logger = logging.getLogger(__name__)


def _pcm_to_wav(pcm: bytes, path: str) -> None:
    """Write 16 kHz mono signed 16-bit LE PCM as a WAV file."""
    with open(path, "wb") as f:
        f.write(struct.pack("<4sI4s", b"RIFF", 36 + len(pcm), b"WAVE"))
        f.write(struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, DEVICE_SAMPLE_RATE,
                            DEVICE_SAMPLE_RATE * 2, 2, 16))
        f.write(struct.pack("<4sI", b"data", len(pcm)))
        f.write(pcm)


class ASRCoreEngine(STTEngine):
    name = "asrcore"

    def __init__(self, socket_path: str = SOCKET_PATH) -> None:
        self._socket_path = socket_path

    async def _ensure_loaded(self, session: aiohttp.ClientSession) -> None:
        """Ask ASRCore to load its default model if it is not ready yet."""
        try:
            async with session.get(
                "http://localhost/status",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"ASRCore status returned {resp.status}: {body[:500]}")
                status = await resp.json()
        except aiohttp.ClientConnectorError as exc:
            raise RuntimeError(
                f"Cannot connect to ASRCore at {self._socket_path}: {exc}"
            ) from exc

        state = status.get("state")
        if state == "loaded":
            return

        if state == "unloaded":
            try:
                async with session.post(
                    "http://localhost/load",
                    json={"model_name": DEFAULT_MODEL_NAME},
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"ASRCore load returned {resp.status}: {body[:500]}")
            except aiohttp.ClientConnectorError as exc:
                raise RuntimeError(
                    f"Cannot connect to ASRCore at {self._socket_path}: {exc}"
                ) from exc

        for _ in range(60):
            async with session.get("http://localhost/status") as resp:
                status = await resp.json()
            state = status.get("state")
            if state == "loaded":
                return
            if state == "error":
                error_message = status.get("error_message") or "unknown error"
                raise RuntimeError(f"ASRCore model failed to load: {error_message}")
            await asyncio.sleep(0.5)

        raise RuntimeError("ASRCore model did not become loaded in time")

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        if not pcm:
            raise ValueError("asrcore transcribe: empty PCM buffer")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)
        try:
            _pcm_to_wav(pcm, tmp_path)

            connector = aiohttp.UnixConnector(path=self._socket_path)
            async with aiohttp.ClientSession(connector=connector) as session:
                await self._ensure_loaded(session)

                payload: dict[str, Any] = {"audio_path": tmp_path}
                url = "http://localhost/transcribe"
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(
                            f"ASRCore returned {resp.status}: {body[:500]}"
                        )
                    result = await resp.json()

            text = result.get("text", "")
            detected_lang = result.get("detected_language", "")

            return {"text": text, "language": detected_lang}
        finally:
            Path(tmp_path).unlink(missing_ok=True)
