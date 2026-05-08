"""Sentinel lifecycle hooks.

Three hooks register against Hermes' plugin lifecycle:

  * :func:`on_session_start` — start overlay server, prime briefing
  * :func:`on_session_end` — finalize transcript, stop overlay, run extract
  * :func:`pre_llm_call` — inject briefing + transcript tail into messages

Each is idempotent: if no sentinel state exists for the session, the hook
no-ops rather than raising.
"""

from sentinel.hooks.on_session_end import on_session_end
from sentinel.hooks.on_session_start import on_session_start
from sentinel.hooks.pre_llm_call import pre_llm_call

__all__ = ["on_session_end", "on_session_start", "pre_llm_call"]
