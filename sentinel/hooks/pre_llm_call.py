"""``pre_llm_call`` hook for Sentinel.

Hermes fires this hook before each LLM request with the constructed message
list. We splice in two synthetic system blocks when a sentinel session is
active so the agent always knows the meeting context:

  1. The compressed briefing from sentinel_curate (if any).
  2. The last 20 transcript chunks (a "tail") so the agent stays current
     with what was just said in the meeting.

Plugins can mutate ``messages`` in-place by returning the new list — Hermes'
hook system uses the first non-None return value. We always return a list,
never None, so that callers downstream see the injected context.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sentinel.context_sync import merge_into_pre_llm_call
from sentinel.state import current_state, get_state

logger = logging.getLogger(__name__)


_TRANSCRIPT_TAIL_LINES = 20


def pre_llm_call(
    messages: Optional[List[Dict[str, Any]]] = None,
    **kwargs: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Inject sentinel briefing + transcript tail into ``messages``.

    Args:
        messages: The current message list. May be None on early calls.
        **kwargs: lifecycle kwargs (we use ``session_id``).

    Returns:
        Mutated messages list, or None to leave unchanged.
    """
    if messages is None:
        return None

    session_id = kwargs.get("session_id")
    state = get_state(str(session_id)) if session_id else current_state()
    if state is None or not state.is_active:
        # Nothing to inject — leave messages alone.
        return None

    transcript_tail = state.transcript_text(last=_TRANSCRIPT_TAIL_LINES)
    briefing = state.briefing
    if not transcript_tail and not (briefing and briefing.summary):
        return None

    try:
        return merge_into_pre_llm_call(
            messages=messages,
            briefing=briefing,
            transcript_tail=transcript_tail,
        )
    except Exception as e:
        logger.debug("sentinel pre_llm_call inject skipped: %s", e)
        return None
