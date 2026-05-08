"""JSON Schemas for the 8 Sentinel tools.

Schemas only — no runtime imports beyond stdlib so the dashboard / docs /
tests can import them cheaply.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


# ---------------------------------------------------------------------------
# Reusable enums
# ---------------------------------------------------------------------------

_BACKEND_ENUM = ["openai", "grok", "assemblyai", "deepgram", "gemini", "local"]

_SUGGEST_MODE_ENUM = ["talking_points", "reply", "recap", "objection", "freeform"]

_OVERLAY_ACTION_ENUM = ["show", "hide", "toggle", "position"]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SENTINEL_CURATE_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_curate",
    "description": (
        "Pre-meeting curation. Loads briefing docs (paths or inline text), "
        "records the meeting goal and persona, compresses everything to a "
        "token budget, and stages the briefing for injection at "
        "sentinel_start. Call this BEFORE sentinel_start so the realtime "
        "model already knows the context when audio begins."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "meeting_id": {
                "type": "string",
                "description": "Stable ID for the meeting (e.g. calendar event id, slug).",
            },
            "docs": {
                "type": "array",
                "description": (
                    "List of doc inputs. Each item is one of: "
                    "{path: '/abs/path.md', title: '...'} | "
                    "{inline: '...raw text...', title: '...'} | "
                    "{url: 'https://...', title: '...'}. URLs are recorded "
                    "but NOT fetched here — pre-fetch with regular tools and "
                    "pass body as 'inline' if you need the content."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "path": {"type": "string"},
                        "inline": {"type": "string"},
                        "url": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "goal": {
                "type": "string",
                "description": "What you want out of this meeting.",
            },
            "persona": {
                "type": "string",
                "description": "Persona for the copilot to adopt (optional).",
            },
            "max_tokens": {
                "type": "integer",
                "description": "Briefing token budget. Default 2000.",
                "minimum": 200,
                "maximum": 16000,
            },
        },
        "required": ["meeting_id"],
        "additionalProperties": False,
    },
}


SENTINEL_START_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_start",
    "description": (
        "Start Hermes Sentinel live audio capture and stream it to the "
        "selected realtime backend. If a briefing was staged via "
        "sentinel_curate, it is injected on connect via session.update. "
        "Returns immediately — poll sentinel_status for liveness."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "backend": {
                "type": "string",
                "enum": _BACKEND_ENUM,
                "description": "Realtime backend to use.",
            },
            "meeting_url": {
                "type": "string",
                "description": "Optional meeting URL for traceability.",
            },
            "meeting_id": {
                "type": "string",
                "description": (
                    "Optional meeting_id. Defaults to the most recent "
                    "sentinel_curate meeting_id, or a generated id."
                ),
            },
            "channels": {
                "type": "integer",
                "enum": [1, 2],
                "description": "1 = mic only, 2 = system + mic. Default 2.",
            },
        },
        "required": ["backend"],
        "additionalProperties": False,
    },
}


SENTINEL_STOP_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_stop",
    "description": (
        "Stop the active sentinel capture, close the backend WebSocket, "
        "and finalize the transcript. If auto_extract is true, kicks off "
        "post-meeting extraction (action items, summary)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "auto_extract": {
                "type": "boolean",
                "description": "Run sentinel_post after stopping. Default true.",
            },
        },
        "additionalProperties": False,
    },
}


SENTINEL_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_status",
    "description": (
        "Report live sentinel session state — backend, audio status, "
        "transcript line count, suggestion count."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


SENTINEL_SUGGEST_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_suggest",
    "description": (
        "Request an instant contextual suggestion based on the live "
        "transcript and briefing. Result is pushed to the overlay AND "
        "returned in the tool result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you need help with right now.",
            },
            "mode": {
                "type": "string",
                "enum": _SUGGEST_MODE_ENUM,
                "description": (
                    "talking_points: bullet talking points; "
                    "reply: drafted reply to last speaker; "
                    "recap: short recap of last few minutes; "
                    "objection: handle a likely objection; "
                    "freeform: model decides shape."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


SENTINEL_POST_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_post",
    "description": (
        "Run post-meeting extraction. Builds summary, action items, and "
        "(optionally) a new skill from the meeting transcript. Saves to "
        "yaowpedia if enabled in config."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "meeting_id": {
                "type": "string",
                "description": (
                    "Meeting to process. Defaults to the most recent."
                ),
            },
            "create_skill": {
                "type": "boolean",
                "description": (
                    "Create a Hermes skill from the meeting. Default false."
                ),
            },
        },
        "additionalProperties": False,
    },
}


SENTINEL_HISTORY_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_history",
    "description": (
        "Search past meeting transcripts and briefings for relevant "
        "snippets. Returns matches with meeting_id, score, snippet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (substring match in v1).",
            },
            "limit": {
                "type": "integer",
                "description": "Max matches. Default 10.",
                "minimum": 1,
                "maximum": 100,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


SENTINEL_OVERLAY_SCHEMA: Dict[str, Any] = {
    "name": "sentinel_overlay",
    "description": (
        "Show, hide, toggle, or position the stealth Tauri overlay window — "
        "and, optionally, mute/unmute the mic or system audio capture and "
        "set a per-application source filter. If the overlay process isn't "
        "running, 'show' attempts to launch it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _OVERLAY_ACTION_ENUM,
            },
            "position": {
                "type": "object",
                "description": "Used when action='position'.",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            "mute_mic": {
                "type": "boolean",
                "description": "When true, mute the microphone capture stream.",
            },
            "mute_system": {
                "type": "boolean",
                "description": "When true, mute the system-audio capture stream.",
            },
            "audio_sources": {
                "type": "object",
                "description": (
                    "Per-application audio source filter, keyed by source id "
                    "(e.g. 'sink_input_42' or 'chrome'). True keeps the "
                    "source in the capture mix; false drops it."
                ),
                "additionalProperties": {"type": "boolean"},
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional sentinel session id to target — defaults to the "
                    "current/most-recent active session."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

ALL_SCHEMAS: Tuple[Dict[str, Any], ...] = (
    SENTINEL_CURATE_SCHEMA,
    SENTINEL_START_SCHEMA,
    SENTINEL_STOP_SCHEMA,
    SENTINEL_STATUS_SCHEMA,
    SENTINEL_SUGGEST_SCHEMA,
    SENTINEL_POST_SCHEMA,
    SENTINEL_HISTORY_SCHEMA,
    SENTINEL_OVERLAY_SCHEMA,
)
