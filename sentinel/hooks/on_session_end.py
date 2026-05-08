"""``on_session_end`` hook for Sentinel.

If audio capture is still running when the agent's session ends, we stop it
cleanly so we don't orphan a Rust subprocess or leak a sounddevice stream.
We also drop the in-memory session state so a fresh session starts clean.
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel import config as cfgmod
from sentinel.state import drop_state, get_state

logger = logging.getLogger(__name__)


def on_session_end(**kwargs: Any) -> None:
    """Best-effort cleanup. Never raises into the host session.

    Args:
        **kwargs: Hermes lifecycle kwargs (we use ``session_id``).
    """
    session_id = str(kwargs.get("session_id") or "sentinel-default")
    state = get_state(session_id)
    if state is None:
        return

    if state.is_active:
        try:
            from sentinel.runtime import stop_session
            stop_session(state=state)
        except Exception as e:
            logger.debug("sentinel: stop_session in on_session_end failed: %s", e)

    cfg = cfgmod.load()
    if cfg.post_meeting.auto_extract_actions and state.transcript:
        try:
            from sentinel.runtime import post_extract
            post_extract(state=state, create_skill=cfg.post_meeting.auto_create_skills)
        except Exception as e:
            logger.debug("sentinel: post_extract in on_session_end failed: %s", e)

    drop_state(session_id)
