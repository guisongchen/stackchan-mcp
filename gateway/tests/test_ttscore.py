"""Integration tests for the TTSCore TTS engine.

These tests spin up a minimal fake TTS Core service on a temporary Unix
socket and exercise synthesis, automatic model loading, and cleanup.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web
import pytest
import pytest_asyncio

from _audio_fixtures import make_wav_bytes
from stackchan_mcp.tts.ttscore import TTSCoreEngine


class _FakeTTSCore:
    """Minimal TTS Core service for testing."""

    def __init__(self, socket_path: Path, *, state: str = "loaded") -> None:
        self.socket_path = socket_path
        self.state = state
        self.load_calls: list[dict[str, Any]] = []
        self.synthesize_calls: list[dict[str, Any]] = []
        self.generated_paths: list[str] = []
        self.error_message: str | None = None
        self.synthesize_status = 200
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/status", self._status)
        app.router.add_post("/load", self._load)
        app.router.add_post("/synthesize", self._synthesize)

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

    async def _synthesize(self, request: web.Request) -> web.Response:
        data = await request.json()
        self.synthesize_calls.append(data)

        if self.synthesize_status != 200:
            return web.json_response(
                {"detail": "service unavailable"}, status=self.synthesize_status
            )

        # Write a deterministic WAV file at 24 kHz; the engine must resample.
        audio_path = str(Path(tempfile.gettempdir()) / f"tts_test_{len(self.synthesize_calls)}.wav")
        self.generated_paths.append(audio_path)
        wav_bytes = make_wav_bytes(sample_rate=24000, duration_ms=100)
        Path(audio_path).write_bytes(wav_bytes)

        return web.json_response(
            {"audio_path": audio_path, "duration_seconds": 0.1, "sample_rate": 24000}
        )


@pytest_asyncio.fixture
async def fake_tts(tmp_path: Path):
    """Yield a started FakeTTSCore and stop it after the test."""
    socket_path = tmp_path / "tts_core.sock"
    core = _FakeTTSCore(socket_path)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


@pytest.mark.asyncio
async def test_synthesize_happy_path(fake_tts: _FakeTTSCore) -> None:
    """TTSCore fetches the WAV, resamples to 16 kHz, and cleans up."""
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    pcm = await engine.synthesize("hello")

    # 24 kHz 100 ms = 2400 samples. Resampled to 16 kHz = 1600 samples = 3200 bytes.
    assert isinstance(pcm, bytes)
    assert len(pcm) == 3200
    assert len(fake_tts.synthesize_calls) == 1
    assert fake_tts.synthesize_calls[0]["text"] == "hello"
    assert fake_tts.synthesize_calls[0]["language"] == "auto"
    assert fake_tts.synthesize_calls[0]["speaker"] == "Serena"
    # Generated WAV file was deleted after reading.
    assert Path(fake_tts.generated_paths[0]).exists() is False


@pytest.mark.asyncio
async def test_synthesize_detects_chinese(fake_tts: _FakeTTSCore) -> None:
    """CJK text is sent with language='chinese'."""
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    await engine.synthesize("我听见了")

    assert fake_tts.synthesize_calls[0]["language"] == "chinese"


@pytest.mark.asyncio
async def test_synthesize_uses_speaker_name_override(fake_tts: _FakeTTSCore) -> None:
    """speaker_name opt is forwarded as the 'speaker' field."""
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    await engine.synthesize("hello", speaker_name="Alex")

    assert fake_tts.synthesize_calls[0]["speaker"] == "Alex"


@pytest.mark.asyncio
async def test_synthesize_auto_loads_model(fake_tts: _FakeTTSCore) -> None:
    """If the service starts unloaded, the engine calls /load first."""
    fake_tts.state = "unloaded"
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    await engine.synthesize("hello")

    assert fake_tts.state == "loaded"
    assert len(fake_tts.load_calls) == 1
    assert fake_tts.load_calls[0]["model_name"] == "Qwen3-TTS-12Hz-0.6B-CustomVoice"
    assert len(fake_tts.synthesize_calls) == 1


@pytest.mark.asyncio
async def test_synthesize_waits_for_loading_model(fake_tts: _FakeTTSCore) -> None:
    """If the service is already loading, the engine polls until loaded."""
    fake_tts.state = "loading"

    async def _become_loaded() -> None:
        await asyncio.sleep(0.3)
        fake_tts.state = "loaded"

    asyncio.create_task(_become_loaded())
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    await engine.synthesize("hello")

    assert fake_tts.load_calls == []
    assert len(fake_tts.synthesize_calls) == 1


@pytest.mark.asyncio
async def test_synthesize_propagates_load_error(fake_tts: _FakeTTSCore) -> None:
    """A service that enters the error state raises a clear RuntimeError."""
    fake_tts.state = "error"
    fake_tts.error_message = "cuda out of memory"
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    with pytest.raises(RuntimeError, match="cuda out of memory"):
        await engine.synthesize("hello")


@pytest.mark.asyncio
async def test_synthesize_propagates_http_error(fake_tts: _FakeTTSCore) -> None:
    """A non-2xx from /synthesize becomes a RuntimeError."""
    fake_tts.synthesize_status = 503
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    with pytest.raises(RuntimeError, match="TTSCore returned 503"):
        await engine.synthesize("hello")


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_text(fake_tts: _FakeTTSCore) -> None:
    """Empty text fails fast before touching the network."""
    engine = TTSCoreEngine(socket_path=str(fake_tts.socket_path))

    with pytest.raises(ValueError, match="empty text"):
        await engine.synthesize("   ")

    assert fake_tts.synthesize_calls == []


@pytest.mark.asyncio
async def test_synthesize_missing_socket_raises_clear_error() -> None:
    """If the Core service is not running, the failure is explicit."""
    engine = TTSCoreEngine(socket_path="/tmp/nonexistent_tts_core_test.sock")

    with pytest.raises(RuntimeError):
        await engine.synthesize("hello")
