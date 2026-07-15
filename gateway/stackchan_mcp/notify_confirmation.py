"""Shared helper for pushing confirmation notifications to StackChan.

Both the MCP tool (``stackchan_notify_confirmation``) and the HTTP hook
endpoint use this module so the notification behavior stays consistent.
"""

from __future__ import annotations

import logging
from typing import Any

from .tts import synthesize_and_send

logger = logging.getLogger(__name__)


async def notify_confirmation_on_device(
    title: str,
    message: str,
    gateway: Any,
) -> dict[str, Any]:
    """Fire-and-forget notification to StackChan.

    Speaks the confirmation title/message on the device and triggers a
    visual cue. Any failure is swallowed so the terminal prompt is never
    blocked.

    Returns a simple ``{"ok": True}`` dict. Errors are logged but never
    raised to the caller.
    """
    parts = []
    if isinstance(title, str) and title:
        parts.append(title)
    if isinstance(message, str) and message:
        parts.append(message)

    if not parts:
        return {"ok": True}

    text = " ".join(parts)

    try:
        await synthesize_and_send({"text": text}, gateway=gateway)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to speak confirmation notification: %s", exc)

    # Best-effort visual cue; never let an avatar/LED failure break the hook.
    try:
        if gateway.esp32.device_connected:
            await gateway.esp32.call_tool(
                "self.display.set_avatar", {"face": "thinking"}
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to set avatar for notification: %s", exc)

    return {"ok": True}
