"""hermes_sentinel — Hermes Agent plugin entry point.

Registers:

  * Eight tools (sentinel_curate, sentinel_start, sentinel_stop,
    sentinel_status, sentinel_suggest, sentinel_post, sentinel_history,
    sentinel_overlay)
  * Three lifecycle hooks (on_session_start, on_session_end, pre_llm_call)
  * One CLI command tree (``hermes sentinel``)

The plugin is opt-in via ``plugins.enabled`` in Hermes' config.yaml. After
``hermes plugins install ~/.hermes/plugins/sentinel`` and
``hermes plugins enable sentinel``, the agent gains live meeting
intelligence without any further setup beyond setting backend API keys in
``~/.env``.

Reference pattern: ``plugins/google_meet/__init__.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Tuple

from sentinel._version import __version__
from sentinel.cli import register_cli as _register_sentinel_cli
from sentinel.cli import sentinel_command as _sentinel_command
from sentinel.hooks import on_session_end as _on_session_end
from sentinel.hooks import on_session_start as _on_session_start
from sentinel.hooks import pre_llm_call as _pre_llm_call
from sentinel.tools import (
    SENTINEL_CURATE_SCHEMA,
    SENTINEL_HISTORY_SCHEMA,
    SENTINEL_OVERLAY_SCHEMA,
    SENTINEL_POST_SCHEMA,
    SENTINEL_START_SCHEMA,
    SENTINEL_STATUS_SCHEMA,
    SENTINEL_STOP_SCHEMA,
    SENTINEL_SUGGEST_SCHEMA,
    check_sentinel_requirements,
    handle_sentinel_curate,
    handle_sentinel_history,
    handle_sentinel_overlay,
    handle_sentinel_post,
    handle_sentinel_start,
    handle_sentinel_status,
    handle_sentinel_stop,
    handle_sentinel_suggest,
)

logger = logging.getLogger(__name__)


# (tool_name, schema, handler, emoji)
_TOOLS: Tuple[Tuple[str, dict, Callable[..., str], str], ...] = (
    ("sentinel_curate",  SENTINEL_CURATE_SCHEMA,  handle_sentinel_curate,  "📋"),
    ("sentinel_start",   SENTINEL_START_SCHEMA,   handle_sentinel_start,   "🎙️"),
    ("sentinel_stop",    SENTINEL_STOP_SCHEMA,    handle_sentinel_stop,    "🛑"),
    ("sentinel_status",  SENTINEL_STATUS_SCHEMA,  handle_sentinel_status,  "🟢"),
    ("sentinel_suggest", SENTINEL_SUGGEST_SCHEMA, handle_sentinel_suggest, "💡"),
    ("sentinel_post",    SENTINEL_POST_SCHEMA,    handle_sentinel_post,    "📝"),
    ("sentinel_history", SENTINEL_HISTORY_SCHEMA, handle_sentinel_history, "🔎"),
    ("sentinel_overlay", SENTINEL_OVERLAY_SCHEMA, handle_sentinel_overlay, "👁️"),
)


def register(ctx: Any) -> None:
    """Register tools, hooks, and CLI command with the Hermes plugin loader.

    Called once when the plugin is enabled. ``ctx`` is the Hermes
    ``PluginContext`` facade (see ``hermes_cli.plugins.PluginContext``).

    Args:
        ctx: PluginContext provided by the loader.
    """
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="sentinel",
            schema=schema,
            handler=handler,
            check_fn=check_sentinel_requirements,
            emoji=emoji,
            description=schema.get("description", ""),
        )

    ctx.register_cli_command(
        name="sentinel",
        help="Hermes Sentinel — meeting intelligence (curate, capture, suggest)",
        setup_fn=_register_sentinel_cli,
        handler_fn=_sentinel_command,
        description=(
            "Pre-meeting curation, dual-channel stealth capture, real-time "
            "contextual suggestions, post-meeting auto-extract. Run "
            "`hermes sentinel doctor` to verify dependencies."
        ),
    )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_llm_call", _pre_llm_call)

    logger.info("hermes_sentinel v%s registered (tools=%d)", __version__, len(_TOOLS))
