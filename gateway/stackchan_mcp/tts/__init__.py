"""TTS framework for Phase 4 (Issue #70).

This package provides the engine-agnostic skeleton for the gateway-side
``say(text)`` MCP tool plus the concrete TTSCore engine that talks to the
local TTS Core service over ``/tmp/tts_core.sock``.

The package exports :class:`TTSEngine`, an :class:`EngineRegistry`, the
:func:`synthesize_and_send` orchestrator, and registers the TTSCore
engine at import time.
"""

from __future__ import annotations

import logging
from typing import Callable

from .base import EngineRegistry, TTSEngine, get_registry
from .orchestrator import (
    DEFAULT_VOICE,
    send_pcm_audio,
    send_pcm_stream,
    synthesize_and_send,
)

_logger = logging.getLogger(__name__)


def _try_register(register_fn: Callable[[], None], engine_label: str) -> None:
    """Run ``register_fn`` and swallow ImportErrors."""
    try:
        register_fn()
    except ImportError as exc:
        _logger.debug("Skipping %s engine registration: %s", engine_label, exc)


def _register_ttscore() -> None:
    from .ttscore import TTSCoreEngine

    get_registry().register(TTSCoreEngine())


_try_register(_register_ttscore, "ttscore")


__all__ = [
    "DEFAULT_VOICE",
    "EngineRegistry",
    "TTSEngine",
    "get_registry",
    "send_pcm_audio",
    "send_pcm_stream",
    "synthesize_and_send",
]
