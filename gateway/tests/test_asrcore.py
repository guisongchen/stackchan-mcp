"""Integration tests for the ASRCore STT engine.

These tests spin up a minimal fake ASR Core service on a temporary Unix
socket and exercise the full engine surface: successful transcription,
automatic model loading, and error paths.
"""

from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path
from typing import Any

from aiohttp import web
import pytest
import pytest_asyncio

from stackchan_mcp.stt.asrcore import ASRCoreEngine, DEVICE_SAMPLE_RATE


class _FakeASRCore:
    """Minimal ASR Core service for testing."""

    def __init__(self, socket_path: Path, *, state: str = "loaded") -> None:
        self.socket_path = socket_path
        self.state = state
        self.load_calls: list[dict[str, Any]] = []
        self.transcribe_calls: list[dict[str, Any]] = []
        self.error_message: str | None = None
        self.transcribe_status = 200
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/status", self._status)
        app.router.add_post("/load", self._load)
        app.router.add_post("/transcribe", self._transcribe)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.UnixSite(self._runner, path=str(self.socket_path))
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _status(self, request: web.Request) -> web.Response:
        payload: dict[str, Any] = {"state": self.state}
        if self.error_message is not None:
            payload["error_message"] = self.error_message
        return web.json_response(payload)

    async def _load(self, request: web.Request) -> web.Response:
        data = await request.json()
        self.load_calls.append(data)
        self.state = "loaded"
        return web.json_response({"state": "loaded"})

    async def _transcribe(self, request: web.Request) -> web.Response:
        data = await request.json()
        self.transcribe_calls.append(data)

        if self.transcribe_status != 200:
            return web.json_response(
                {"detail": "service unavailable"}, status=self.transcribe_status
            )

        audio_path = data.get("audio_path")
        if not audio_path or not Path(audio_path).exists():
            return web.json_response(
                {"detail": "audio file not found"}, status=422
            )

        with wave.open(audio_path, "rb") as wav:
            n_frames = wav.getnframes()
            raw = wav.readframes(n_frames)

        text = "fake transcription" if raw else "silence"
        return web.json_response(
            {"text": text, "detected_language": "english"}
        )


@pytest_asyncio.fixture
async def fake_asr(tmp_path: Path):
    """Yield a started FakeASRCore and stop it after the test."""
    socket_path = tmp_path / "asr_core.sock"
    core = _FakeASRCore(socket_path)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


def _make_pcm_samples() -> bytes:
    """Return a few frames of 16 kHz mono 16-bit PCM."""
    samples = [i * 100 for i in range(DEVICE_SAMPLE_RATE // 100)]
    return struct.pack(f"<{len(samples)}h", *samples)


@pytest.mark.asyncio
async def test_transcribe_happy_path(fake_asr: _FakeASRCore) -> None:
    """ASRCore posts a WAV file and returns the service response."""
    engine = ASRCoreEngine(socket_path=str(fake_asr.socket_path))
    pcm = _make_pcm_samples()

    result = await engine.transcribe(pcm)

    assert result["text"] == "fake transcription"
    assert result["language"] == "english"
    assert len(fake_asr.transcribe_calls) == 1
    audio_path = fake_asr.transcribe_calls[0]["audio_path"]
    assert Path(audio_path).exists() is False  # temp file cleaned up


@pytest.mark.asyncio
async def test_transcribe_auto_loads_model(fake_asr: _FakeASRCore) -> None:
    """If the service starts unloaded, the engine calls /load first."""
    fake_asr.state = "unloaded"
    engine = ASRCoreEngine(socket_path=str(fake_asr.socket_path))

    result = await engine.transcribe(_make_pcm_samples())

    assert result["text"] == "fake transcription"
    assert fake_asr.state == "loaded"
    assert len(fake_asr.load_calls) == 1
    assert fake_asr.load_calls[0]["model_name"] == "qwen3-asr-0.6b"
    assert len(fake_asr.transcribe_calls) == 1


@pytest.mark.asyncio
async def test_transcribe_waits_for_loading_model(fake_asr: _FakeASRCore) -> None:
    """If the service is already loading, the engine polls until loaded."""
    fake_asr.state = "loading"

    async def _become_loaded() -> None:
        await asyncio.sleep(0.3)
        fake_asr.state = "loaded"

    asyncio.create_task(_become_loaded())
    engine = ASRCoreEngine(socket_path=str(fake_asr.socket_path))

    result = await engine.transcribe(_make_pcm_samples())

    assert result["text"] == "fake transcription"
    assert fake_asr.load_calls == []


@pytest.mark.asyncio
async def test_transcribe_propagates_load_error(fake_asr: _FakeASRCore) -> None:
    """A service that enters the error state raises a clear RuntimeError."""
    fake_asr.state = "error"
    fake_asr.error_message = "cuda out of memory"
    engine = ASRCoreEngine(socket_path=str(fake_asr.socket_path))

    with pytest.raises(RuntimeError, match="cuda out of memory"):
        await engine.transcribe(_make_pcm_samples())


@pytest.mark.asyncio
async def test_transcribe_propagates_http_error(fake_asr: _FakeASRCore) -> None:
    """A non-2xx from /transcribe becomes a RuntimeError."""
    fake_asr.transcribe_status = 503
    engine = ASRCoreEngine(socket_path=str(fake_asr.socket_path))

    with pytest.raises(RuntimeError, match="ASRCore returned 503"):
        await engine.transcribe(_make_pcm_samples())


@pytest.mark.asyncio
async def test_transcribe_rejects_empty_pcm(fake_asr: _FakeASRCore) -> None:
    """Empty PCM fails fast before touching the network."""
    engine = ASRCoreEngine(socket_path=str(fake_asr.socket_path))

    with pytest.raises(ValueError, match="empty PCM"):
        await engine.transcribe(b"")

    assert fake_asr.transcribe_calls == []


@pytest.mark.asyncio
async def test_transcribe_missing_socket_raises_clear_error() -> None:
    """If the Core service is not running, the failure is explicit."""
    engine = ASRCoreEngine(socket_path="/tmp/nonexistent_asr_core_test.sock")

    with pytest.raises(RuntimeError):
        await engine.transcribe(_make_pcm_samples())
