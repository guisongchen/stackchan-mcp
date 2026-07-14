"""TTS engine abstraction.

Each concrete engine produces 16 kHz mono PCM bytes from input text.
Opus encoding for transmission to the device, and the WebSocket push,
are handled by :mod:`stackchan_mcp.tts.orchestrator` so engines stay
focused on synthesis.

This module is intentionally dependency-free: it must import cleanly
without ``opuslib`` so that callers can introspect the registered
engines even when the optional ``[tts]`` extra is not installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TTSEngine(ABC):
    """Abstract base for TTS engines.

    Subclasses must set :attr:`name` to a stable identifier (matched
    against the ``voice`` argument of the ``say`` MCP tool) and implement
    :meth:`synthesize`.
    """

    #: Stable identifier used to look this engine up in the registry.
    #: Concrete subclasses must override with a non-empty string.
    name: str = ""

    #: Whether the engine natively interprets emoji as voice-style cues.
    #: Engines that leave this false receive emoji-stripped text from
    #: the orchestrator.
    supports_emoji_style: bool = False

    @abstractmethod
    async def synthesize(self, text: str, **opts: Any) -> bytes:
        """Synthesise ``text`` into 16 kHz mono PCM (signed 16-bit LE).

        Args:
            text: Text to synthesise. Implementations should reject
                empty strings.
            **opts: Engine-specific options (e.g. ``speaker_id`` for
                engines that support numeric speaker identifiers).
                Engines should ignore unknown options rather than raise,
                so the ``say`` tool can pass a uniform argument set.

        Returns:
            Raw PCM bytes at 16 kHz, mono, signed 16-bit little-endian.
            The orchestrator handles Opus encoding and frame chunking
            before pushing to the device.
        """


class EngineRegistry:
    """Tracks available TTS engines by name.

    Concrete engines register themselves at import time (see
    :mod:`stackchan_mcp.tts.ttscore`).
    """

    def __init__(self) -> None:
        self._engines: dict[str, TTSEngine] = {}

    def register(self, engine: TTSEngine) -> None:
        """Register ``engine`` under ``engine.name``.

        Replaces any previously registered engine with the same name —
        this is intentional so tests can inject fakes.
        """
        if not engine.name:
            raise ValueError("TTSEngine.name must be a non-empty string")
        self._engines[engine.name] = engine

    def get(self, name: str) -> TTSEngine | None:
        """Return the engine registered under ``name``, or ``None``."""
        return self._engines.get(name)

    def names(self) -> list[str]:
        """Return all registered engine names, sorted alphabetically."""
        return sorted(self._engines.keys())


_default_registry = EngineRegistry()


def get_registry() -> EngineRegistry:
    """Return the process-wide default :class:`EngineRegistry`."""
    return _default_registry
