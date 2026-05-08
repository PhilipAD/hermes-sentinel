"""SentinelSessionState — in-memory per-session state.

One instance per Hermes session. Created in ``on_session_start``, mutated by
the backend router and audio capture, snapshot-read by tools and overlay,
torn down in ``on_session_end``.

Not threadsafe by default — all writers run on the asyncio event loop. If a
sync caller needs to read, use ``snapshot()``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TranscriptChunk:
    """One unit of streamed transcription output."""

    text: str
    channel: str  # "mic" | "system" | "mixed"
    speaker: Optional[str] = None
    ts: float = field(default_factory=time.time)
    is_final: bool = False


@dataclass
class Suggestion:
    """A single contextual suggestion shown in the overlay."""

    id: str
    mode: str  # "talking_points" | "reply" | "recap" | "objection" | "freeform"
    text: str
    ts: float = field(default_factory=time.time)
    consumed: bool = False


@dataclass
class Briefing:
    """Compressed pre-meeting context."""

    meeting_id: str
    goal: str = ""
    persona: str = ""
    docs: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    token_estimate: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class SentinelSessionState:
    """Holds everything we know about one active sentinel run."""

    session_id: str
    meeting_id: Optional[str] = None
    backend: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    is_active: bool = False
    audio_running: bool = False
    backend_connected: bool = False

    # Live audio control (driven by overlay UI).
    mic_muted: bool = False
    system_muted: bool = False
    audio_level: float = 0.0
    vad_sensitivity: float = 0.5
    # {source_id: enabled-bool} — when non-empty, only listed sources are kept.
    audio_source_filter: Dict[str, bool] = field(default_factory=dict)
    # Last enumerated source list shown to the overlay UI.
    audio_sources: List[Dict[str, Any]] = field(default_factory=list)
    # Active context-injection toggles from the overlay.
    context_flags: Dict[str, bool] = field(default_factory=lambda: {
        "docs": True, "persona": True, "goal": True, "history": True,
    })

    briefing: Optional[Briefing] = None
    transcript: List[TranscriptChunk] = field(default_factory=list)
    suggestions: List[Suggestion] = field(default_factory=list)

    # Subprocess / asyncio-task handles (opaque — never serialized).
    _audio_proc: Any = None
    _backend_task: Any = None
    _capture_task: Any = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add_transcript(self, chunk: TranscriptChunk) -> None:
        with self._lock:
            self.transcript.append(chunk)

    def add_suggestion(self, suggestion: Suggestion) -> None:
        with self._lock:
            self.suggestions.append(suggestion)

    def mark_active(self, backend: str) -> None:
        with self._lock:
            self.is_active = True
            self.backend = backend
            self.started_at = time.time()

    def mark_ended(self) -> None:
        with self._lock:
            self.is_active = False
            self.audio_running = False
            self.backend_connected = False
            self.ended_at = time.time()

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot of session state."""
        with self._lock:
            return {
                "session_id": self.session_id,
                "meeting_id": self.meeting_id,
                "backend": self.backend,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "is_active": self.is_active,
                "audio_running": self.audio_running,
                "backend_connected": self.backend_connected,
                "transcript_lines": len(self.transcript),
                "suggestion_count": len(self.suggestions),
                "mic_muted": self.mic_muted,
                "system_muted": self.system_muted,
                "audio_level": self.audio_level,
                "vad_sensitivity": self.vad_sensitivity,
                "audio_sources": list(self.audio_sources),
                "context_flags": dict(self.context_flags),
                "briefing": (
                    {
                        "meeting_id": self.briefing.meeting_id,
                        "goal": self.briefing.goal,
                        "persona": self.briefing.persona,
                        "doc_count": len(self.briefing.docs),
                        "summary_len": len(self.briefing.summary),
                        "token_estimate": self.briefing.token_estimate,
                    }
                    if self.briefing
                    else None
                ),
            }

    def transcript_text(self, last: Optional[int] = None) -> str:
        """Return the joined transcript as plain text, optionally last-N chunks."""
        with self._lock:
            chunks = self.transcript if last is None else self.transcript[-last:]
            return "\n".join(c.text for c in chunks if c.text)


# ---------------------------------------------------------------------------
# Process-level singleton registry (one state per session_id)
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, SentinelSessionState] = {}
_REG_LOCK = threading.Lock()


def get_state(session_id: str, *, create: bool = False) -> Optional[SentinelSessionState]:
    """Fetch state for *session_id*; if *create*, allocate when missing."""
    with _REG_LOCK:
        st = _REGISTRY.get(session_id)
        if st is None and create:
            st = SentinelSessionState(session_id=session_id)
            _REGISTRY[session_id] = st
        return st


def drop_state(session_id: str) -> Optional[SentinelSessionState]:
    """Remove and return state for *session_id*, if any."""
    with _REG_LOCK:
        return _REGISTRY.pop(session_id, None)


def current_state() -> Optional[SentinelSessionState]:
    """Return the most-recently-active session state, or None.

    Convenience for tools that don't have a session_id in scope (e.g. the
    overlay API). Picks the most recent by ``started_at``.
    """
    with _REG_LOCK:
        if not _REGISTRY:
            return None
        active = [s for s in _REGISTRY.values() if s.is_active]
        pool = active or list(_REGISTRY.values())
        return max(pool, key=lambda s: (s.started_at or 0))
