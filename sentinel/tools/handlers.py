"""Tool handlers for the 8 Sentinel tools.

Schemas live in ``sentinel.tools.schemas``; this module wires each schema
to a concrete handler. Handlers are sync wrappers around async work — the
Hermes registry expects sync callables that return JSON strings, so any
WebSocket / asyncio work is run inside ``asyncio.run`` or scheduled on the
plugin's running loop.

Every handler returns a JSON string with a ``success`` boolean as the first
field, mirroring the google_meet plugin convention so the agent can rely on
the same ack shape across plugins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from sentinel import config as cfgmod
from sentinel.context_sync import compress_briefing
from sentinel.state import (
    Briefing,
    SentinelSessionState,
    Suggestion,
    current_state,
    get_state,
)

logger = logging.getLogger(__name__)


# Last meeting_id curated, in case sentinel_start is called without one.
_LAST_MEETING_ID: Optional[str] = None

# Where post-meeting transcripts are written.
_TRANSCRIPT_DIR = Path.home() / ".hermes" / "plugins" / "sentinel" / "transcripts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _err(msg: str, **extra: Any) -> str:
    return _json({"success": False, "error": msg, **extra})


def _ok(**fields: Any) -> str:
    return _json({"success": True, **fields})


def _state_for_session(session_id: Optional[str], *, create: bool = False) -> SentinelSessionState:
    """Resolve the session state, falling back to current_state()."""
    if session_id:
        st = get_state(session_id, create=create)
        if st is not None:
            return st
    cur = current_state()
    if cur is not None:
        return cur
    if create:
        return get_state(session_id or "sentinel-default", create=True)  # type: ignore[return-value]
    raise LookupError("no active sentinel session")


def _run_async(coro):  # noqa: ANN001
    """Run *coro* on the existing loop if any, else create one.

    Hermes tool handlers run on the synchronous side of the agent loop. We
    accept the slight cost of a transient loop here rather than make the
    whole tool surface async, because Hermes' registry signatures are sync.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
    except RuntimeError:
        pass
    return asyncio.run(coro)


def check_sentinel_requirements() -> bool:
    """Return True when the plugin can run on this machine.

    Soft gate — we only require pyyaml + pydantic (the rest degrade
    gracefully). Specific backends fail at connect-time with clear errors.
    """
    try:
        import yaml  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# sentinel_curate
# ---------------------------------------------------------------------------

def handle_sentinel_curate(args: Dict[str, Any], **_kw: Any) -> str:
    global _LAST_MEETING_ID
    meeting_id = (args.get("meeting_id") or "").strip()
    if not meeting_id:
        return _err("meeting_id is required")

    docs: List[Dict[str, Any]] = list(args.get("docs") or [])
    goal = str(args.get("goal") or "")
    persona = str(args.get("persona") or "")
    cfg = cfgmod.load()
    max_tokens = int(args.get("max_tokens") or cfg.pre_meeting.max_briefing_tokens)

    try:
        briefing = compress_briefing(
            meeting_id=meeting_id,
            docs=docs,
            goal=goal,
            persona=persona,
            max_tokens=max_tokens,
        )
    except Exception as e:
        logger.exception("sentinel_curate failed")
        return _err(f"compress_briefing failed: {e}")

    session_id = str(_kw.get("session_id") or "sentinel-default")
    st = get_state(session_id, create=True)
    assert st is not None
    st.meeting_id = meeting_id
    st.briefing = briefing
    _LAST_MEETING_ID = meeting_id

    return _ok(
        meeting_id=meeting_id,
        token_estimate=briefing.token_estimate,
        doc_count=len(briefing.docs),
        summary_chars=len(briefing.summary),
    )


# ---------------------------------------------------------------------------
# sentinel_start
# ---------------------------------------------------------------------------

def handle_sentinel_start(args: Dict[str, Any], **_kw: Any) -> str:
    backend = (args.get("backend") or "").strip().lower()
    if not backend:
        return _err("backend is required (openai|grok|assemblyai|deepgram|gemini|local)")

    session_id = str(_kw.get("session_id") or "sentinel-default")
    st = get_state(session_id, create=True)
    assert st is not None

    if st.is_active:
        return _err("sentinel already active for this session — call sentinel_stop first")

    meeting_id = str(args.get("meeting_id") or st.meeting_id or _LAST_MEETING_ID or f"meeting-{int(time.time())}")
    st.meeting_id = meeting_id
    st.mark_active(backend=backend)

    cfg = cfgmod.load()
    channels = int(args.get("channels") or cfg.audio.channels)

    # Lazy import — keeps cold-path light if user only ever curates.
    try:
        from sentinel.runtime import start_session as _start_session
    except Exception as e:
        logger.exception("sentinel_start: runtime import failed")
        st.mark_ended()
        return _err(f"sentinel runtime unavailable: {e}")

    try:
        _start_session(state=st, backend=backend, channels=channels)
    except Exception as e:
        logger.exception("sentinel_start failed")
        st.mark_ended()
        return _err(f"start failed: {e}")

    return _ok(
        meeting_id=meeting_id,
        backend=backend,
        channels=channels,
        meeting_url=args.get("meeting_url"),
    )


# ---------------------------------------------------------------------------
# sentinel_stop
# ---------------------------------------------------------------------------

def handle_sentinel_stop(args: Dict[str, Any], **_kw: Any) -> str:
    auto_extract = bool(args.get("auto_extract", True))
    try:
        st = _state_for_session(_kw.get("session_id"))
    except LookupError:
        return _err("no active sentinel session")

    try:
        from sentinel.runtime import stop_session as _stop_session
        transcript_path = _stop_session(state=st)
    except Exception as e:
        logger.exception("sentinel_stop failed")
        return _err(f"stop failed: {e}")

    out: Dict[str, Any] = {
        "meeting_id": st.meeting_id,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "lines": len(st.transcript),
    }

    if auto_extract:
        try:
            from sentinel.runtime import post_extract as _post_extract
            out["post"] = _post_extract(state=st, create_skill=False)
        except Exception as e:
            logger.warning("sentinel auto post-extract failed: %s", e)
            out["post_error"] = str(e)

    return _ok(**out)


# ---------------------------------------------------------------------------
# sentinel_status
# ---------------------------------------------------------------------------

def handle_sentinel_status(_args: Dict[str, Any], **_kw: Any) -> str:
    st = current_state()
    if st is None:
        return _ok(active=False, message="no sentinel session in this process")
    return _ok(active=st.is_active, **st.snapshot())


# ---------------------------------------------------------------------------
# sentinel_suggest
# ---------------------------------------------------------------------------

def handle_sentinel_suggest(args: Dict[str, Any], **_kw: Any) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    mode = str(args.get("mode") or "freeform").lower()

    try:
        st = _state_for_session(_kw.get("session_id"))
    except LookupError:
        return _err("no active sentinel session — call sentinel_start first")

    suggestion_id = uuid.uuid4().hex[:12]

    try:
        from sentinel.runtime import generate_suggestion as _gen
        text = _gen(state=st, query=query, mode=mode)
    except Exception as e:
        logger.warning("sentinel_suggest fallback (runtime err: %s)", e)
        text = (
            f"[stub suggestion / {mode}] {query}\n"
            f"(no live LLM hooked up — install backend or implement runtime.generate_suggestion)"
        )

    suggestion = Suggestion(id=suggestion_id, mode=mode, text=text)
    st.add_suggestion(suggestion)

    # Push to overlay (best-effort, non-blocking).
    try:
        from sentinel.overlay_api import broadcast_suggestion
        _run_async(broadcast_suggestion(suggestion))
    except Exception as e:
        logger.debug("sentinel: overlay broadcast skipped: %s", e)

    return _ok(id=suggestion_id, mode=mode, text=text)


# ---------------------------------------------------------------------------
# sentinel_post
# ---------------------------------------------------------------------------

def handle_sentinel_post(args: Dict[str, Any], **_kw: Any) -> str:
    meeting_id = (args.get("meeting_id") or "").strip() or _LAST_MEETING_ID
    create_skill = bool(args.get("create_skill", False))
    if not meeting_id:
        return _err("meeting_id required (no curated meeting in scope)")

    try:
        st = _state_for_session(_kw.get("session_id"))
    except LookupError:
        st = SentinelSessionState(session_id="post-only", meeting_id=meeting_id)

    try:
        from sentinel.runtime import post_extract as _post
        result = _post(state=st, create_skill=create_skill)
    except Exception as e:
        logger.exception("sentinel_post failed")
        return _err(f"post extraction failed: {e}")
    return _ok(meeting_id=meeting_id, **result)


# ---------------------------------------------------------------------------
# sentinel_history
# ---------------------------------------------------------------------------

def handle_sentinel_history(args: Dict[str, Any], **_kw: Any) -> str:
    query = (args.get("query") or "").strip().lower()
    if not query:
        return _err("query is required")
    limit = int(args.get("limit") or 10)

    if not _TRANSCRIPT_DIR.is_dir():
        return _ok(matches=[], note="no transcript history yet")

    matches: List[Dict[str, Any]] = []
    for path in sorted(_TRANSCRIPT_DIR.glob("*.txt")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        idx = text.lower().find(query)
        if idx < 0:
            continue
        snippet = text[max(0, idx - 80): idx + 160].strip()
        matches.append({
            "meeting_id": path.stem,
            "path": str(path),
            "score": 1.0,
            "snippet": snippet,
        })
        if len(matches) >= limit:
            break
    return _ok(matches=matches, count=len(matches))


# ---------------------------------------------------------------------------
# sentinel_overlay
# ---------------------------------------------------------------------------

def handle_sentinel_overlay(args: Dict[str, Any], **_kw: Any) -> str:
    action = (args.get("action") or "").strip().lower()
    if action not in {"show", "hide", "toggle", "position"}:
        return _err(f"invalid action {action!r}")

    # Optional knobs: mute toggles, per-source filter, session selection.
    mute_mic = args.get("mute_mic")
    mute_system = args.get("mute_system")
    audio_sources = args.get("audio_sources") or args.get("audio_source_filter")
    target_session = args.get("session_id") or _kw.get("session_id")

    # Resolve the session whose state we're operating on.
    state = None
    if target_session:
        state = get_state(str(target_session), create=False)
    if state is None:
        state = current_state()

    applied: Dict[str, Any] = {}

    if state is not None:
        if isinstance(mute_mic, bool):
            with state._lock:
                state.mic_muted = mute_mic
            applied["mic_muted"] = mute_mic
        if isinstance(mute_system, bool):
            with state._lock:
                state.system_muted = mute_system
            applied["system_muted"] = mute_system
        if isinstance(audio_sources, dict):
            normalised = {str(k): bool(v) for k, v in audio_sources.items()}
            with state._lock:
                state.audio_source_filter = normalised
                for s in state.audio_sources:
                    sid = str(s.get("id") or s.get("name") or "")
                    if sid in normalised:
                        s["enabled"] = normalised[sid]
            applied["audio_source_filter"] = normalised

    # Forward mute/source updates to the audio capture module if it offers
    # the optional hooks. Best-effort; ignore missing functions.
    if isinstance(mute_mic, bool) or isinstance(mute_system, bool) or isinstance(audio_sources, dict):
        try:
            from sentinel import audio_capture as _ac
            if isinstance(mute_mic, bool):
                fn = getattr(_ac, "set_channel_mute", None)
                if callable(fn): fn(channel="mic", muted=mute_mic)
            if isinstance(mute_system, bool):
                fn = getattr(_ac, "set_channel_mute", None)
                if callable(fn): fn(channel="system", muted=mute_system)
            if isinstance(audio_sources, dict):
                fn = getattr(_ac, "set_source_filter", None)
                if callable(fn): fn({str(k): bool(v) for k, v in audio_sources.items()})
        except Exception as e:
            logger.debug("sentinel: audio_capture hook unavailable: %s", e)

    try:
        from sentinel.overlay_api import overlay_command, broadcast_status
        result = _run_async(overlay_command(action, args.get("position")))
        # Make sure overlays see the new mute/filter state right away.
        if applied:
            try:
                _run_async(broadcast_status())
            except Exception as e:
                logger.debug("sentinel: post-action status broadcast failed: %s", e)
    except Exception as e:
        logger.warning("sentinel_overlay failed: %s", e)
        return _err(f"overlay command failed: {e}")
    return _ok(action=action, applied=applied, session_id=getattr(state, "session_id", None), **(result or {}))
