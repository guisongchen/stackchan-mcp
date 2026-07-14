"""ASRCore STT engine — calls local ASRCore HTTP API over Unix socket."""

from __future__ import annotations

import json
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

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        if not pcm:
            raise ValueError("asrcore transcribe: empty PCM buffer")

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(tmp_fd)
        try:
            _pcm_to_wav(pcm, tmp_path)

            connector = aiohttp.UnixConnector(path=self._socket_path)
            async with aiohttp.ClientSession(connector=connector) as session:
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
