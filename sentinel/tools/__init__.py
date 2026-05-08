"""Sentinel tools sub-package — schemas + handlers re-exported here.

Schemas are kept import-cheap (stdlib only) so dashboards / docs / tests can
introspect them. Handlers may import asyncio, subprocess, etc. — pulled in
lazily at runtime.
"""

from sentinel.tools.handlers import (
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
from sentinel.tools.schemas import (
    ALL_SCHEMAS,
    SENTINEL_CURATE_SCHEMA,
    SENTINEL_HISTORY_SCHEMA,
    SENTINEL_OVERLAY_SCHEMA,
    SENTINEL_POST_SCHEMA,
    SENTINEL_START_SCHEMA,
    SENTINEL_STATUS_SCHEMA,
    SENTINEL_STOP_SCHEMA,
    SENTINEL_SUGGEST_SCHEMA,
)

__all__ = [
    "ALL_SCHEMAS",
    "SENTINEL_CURATE_SCHEMA",
    "SENTINEL_HISTORY_SCHEMA",
    "SENTINEL_OVERLAY_SCHEMA",
    "SENTINEL_POST_SCHEMA",
    "SENTINEL_START_SCHEMA",
    "SENTINEL_STATUS_SCHEMA",
    "SENTINEL_STOP_SCHEMA",
    "SENTINEL_SUGGEST_SCHEMA",
    "check_sentinel_requirements",
    "handle_sentinel_curate",
    "handle_sentinel_history",
    "handle_sentinel_overlay",
    "handle_sentinel_post",
    "handle_sentinel_start",
    "handle_sentinel_status",
    "handle_sentinel_stop",
    "handle_sentinel_suggest",
]
