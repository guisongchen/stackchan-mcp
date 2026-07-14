"""STT framework for Phase 4 (Issue #91).

This package provides the engine-agnostic skeleton for the gateway-side
``listen(duration_ms)`` MCP tool plus the concrete ASRCore engine that
talks to the local ASR Core service over ``/tmp/asr_core.sock``.

The package exports :class:`STTEngine`, an :class:`EngineRegistry`, the
:func:`listen_and_transcribe` orchestrator, and registers the ASRCore
engine at import time.
"""

from __future__ import annotations

import logging
from typing import Callable

from .base import EngineRegistry, STTEngine, get_registry
from .orchestrator import DEFAULT_ENGINE, listen_and_transcribe

_logger = logging.getLogger(__name__)


def _try_register(register_fn: Callable[[], None], engine_label: str) -> None:
    """Run ``register_fn`` and swallow ImportErrors."""
    try:
        register_fn()
    except ImportError as exc:
        _logger.debug("Skipping %s engine registration: %s", engine_label, exc)


def _register_asrcore() -> None:
    from .asrcore import ASRCoreEngine

    get_registry().register(ASRCoreEngine())


_try_register(_register_asrcore, "asrcore")


__all__ = [
    "DEFAULT_ENGINE",
    "EngineRegistry",
    "STTEngine",
    "get_registry",
    "listen_and_transcribe",
]
