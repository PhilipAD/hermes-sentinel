"""``on_session_start`` hook for Sentinel.

Lifts the overlay HTTP/WebSocket server (idempotent — only starts once per
process) and seeds an empty SentinelSessionState so subsequent tool calls
have somewhere to write into.

Note: we intentionally do NOT auto-start audio capture here, even though
the spec mentions it as an option. Auto-starting on every Hermes session
would feel surveillance-y; the user must explicitly call ``sentinel_start``
to begin recording. The overlay server is the only thing that comes up
automatically, and only when ``overlay.enabled`` is true.
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel import config as cfgmod
from sentinel.state import get_state

logger = logging.getLogger(__name__)


def on_session_start(**kwargs: Any) -> None:
    """Initialize sentinel state for the new session and start overlay.

    Args:
        **kwargs: Hermes lifecycle kwargs. We use ``session_id`` if present.
    """
    session_id = str(kwargs.get("session_id") or "sentinel-default")
    get_state(session_id, create=True)

    cfg = cfgmod.load()
    if not cfg.overlay.enabled:
        logger.debug("sentinel: overlay disabled in config — skipping server start")
        return

    try:
        from sentinel.overlay_api import ensure_overlay_server
        ensure_overlay_server(cfg)
    except Exception as e:
        logger.warning("sentinel: overlay server start failed: %s", e)
