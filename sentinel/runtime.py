"""Runtime glue between audio capture, backend router, and session state.

The tool handlers call into ``start_session``, ``stop_session``,
``post_extract``, and ``generate_suggestion``. Those four functions are the
only things this module exposes — everything else is private.

Sessions run on a dedicated background thread that owns its asyncio loop.
This isolates the WebSocket + audio pipeline from the agent's main loop so
a stalled backend can't block the agent from finishing a turn.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from sentinel import config as cfgmod
from sentinel.audio_capture import open_capture
from sentinel.backend_router import (
    AssistantTextEvent,
    BackendRouter,
    ErrorEvent,
    TranscriptEvent,
)
from sentinel.context_sync import render_system_prompt
from sentinel.state import SentinelSessionState, TranscriptChunk

logger = logging.getLogger(__name__)


_TRANSCRIPT_DIR = Path.home() / ".hermes" / "plugins" / "sentinel" / "transcripts"


# ---------------------------------------------------------------------------
# Per-session worker
# ---------------------------------------------------------------------------

class _SessionWorker:
    """Owns one event loop on one thread for one sentinel session."""

    def __init__(self, state: SentinelSessionState, backend: str, channels: int):
        self.state = state
        self.backend_name = backend
        self.channels = channels
        self.cfg = cfgmod.load()
        self.cfg.audio.channels = channels
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.router: Optional[BackendRouter] = None
        self.capture = None
        self._stop_evt = threading.Event()
        self._ready_evt = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._thread_main,
            name=f"sentinel-{self.state.session_id[:8]}",
            daemon=True,
        )
        self.thread.start()
        # Wait briefly so callers know whether connect succeeded.
        if not self._ready_evt.wait(timeout=10):
            logger.warning("sentinel: session worker not ready within 10s — proceeding anyway")

    def stop(self) -> None:
        self._stop_evt.set()
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            except Exception as e:
                logger.debug("sentinel: shutdown schedule failed: %s", e)
        if self.thread:
            self.thread.join(timeout=5)

    # -- thread body --------------------------------------------------------

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run())
        except Exception as e:
            logger.exception("sentinel session loop crashed: %s", e)
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _run(self) -> None:
        # 1. Build router and connect.
        try:
            self.router = BackendRouter.create(self.backend_name, self.cfg)
            await self.router.connect(briefing=self.state.briefing)
            self.state.backend_connected = True
        except Exception as e:
            logger.error("sentinel: backend connect failed: %s", e)
            self.state.backend_connected = False
            self._ready_evt.set()
            return

        # 2. Open audio capture.
        try:
            self.capture = open_capture(self.cfg)
            await self.capture.start()
            self.state.audio_running = True
        except Exception as e:
            logger.error("sentinel: audio start failed: %s", e)
            await self._shutdown()
            self._ready_evt.set()
            return

        self._ready_evt.set()

        await asyncio.gather(
            self._pump_audio(),
            self._pump_events(),
            return_exceptions=True,
        )

    async def _pump_audio(self) -> None:
        assert self.capture is not None and self.router is not None
        async for chunk in self.capture:
            if self._stop_evt.is_set():
                break
            try:
                await self.router.send_audio(chunk.pcm16, channel=chunk.channel)
            except Exception as e:
                logger.debug("sentinel: send_audio failed: %s", e)

    async def _pump_events(self) -> None:
        assert self.router is not None
        try:
            from sentinel.overlay_api import broadcast_transcript
        except Exception:
            broadcast_transcript = None  # type: ignore[assignment]

        async for evt in self.router.receive():
            if self._stop_evt.is_set():
                break
            if isinstance(evt, TranscriptEvent):
                chunk = TranscriptChunk(
                    text=evt.text,
                    channel=evt.channel,
                    speaker=evt.speaker,
                    is_final=evt.is_final,
                )
                self.state.add_transcript(chunk)
                if broadcast_transcript:
                    try:
                        await broadcast_transcript(chunk)
                    except Exception as e:
                        logger.debug("sentinel: overlay broadcast failed: %s", e)
            elif isinstance(evt, AssistantTextEvent):
                logger.debug("sentinel assistant: %s", evt.text[:120])
            elif isinstance(evt, ErrorEvent):
                logger.warning("sentinel backend error: %s", evt.message)

    async def _shutdown(self) -> None:
        if self.capture is not None:
            try:
                await self.capture.stop()
            except Exception:
                pass
            self.capture = None
        if self.router is not None:
            try:
                await self.router.close()
            except Exception:
                pass
            self.router = None
        self.state.audio_running = False
        self.state.backend_connected = False


# ---------------------------------------------------------------------------
# Public surface — called by tool handlers
# ---------------------------------------------------------------------------

_WORKERS: Dict[str, _SessionWorker] = {}


def start_session(*, state: SentinelSessionState, backend: str, channels: int) -> None:
    worker = _SessionWorker(state=state, backend=backend, channels=channels)
    _WORKERS[state.session_id] = worker
    worker.start()
    state._backend_task = worker  # opaque handle


def stop_session(*, state: SentinelSessionState) -> Optional[Path]:
    worker = _WORKERS.pop(state.session_id, None)
    if worker is None and isinstance(state._backend_task, _SessionWorker):
        worker = state._backend_task
    if worker is not None:
        worker.stop()
    state.mark_ended()
    return _persist_transcript(state)


def post_extract(*, state: SentinelSessionState, create_skill: bool = False) -> Dict[str, Any]:
    """Lightweight post-meeting extraction.

    v1 just produces a heuristic summary + bullet action items from the
    transcript text. The agent can call its own LLM tools afterwards if it
    wants a higher-quality output; this function deliberately doesn't make
    network calls so it can't fail when offline.
    """
    text = state.transcript_text()
    summary = _heuristic_summary(text)
    actions = _extract_actions(text)

    out: Dict[str, Any] = {
        "summary": summary,
        "action_items": actions,
        "transcript_chars": len(text),
    }

    cfg = cfgmod.load()
    if cfg.post_meeting.save_to_yaowpedia:
        try:
            yp = _save_yaowpedia(state.meeting_id or "unknown", summary, text, actions)
            out["yaowpedia_path"] = str(yp)
        except Exception as e:
            logger.warning("sentinel: yaowpedia save failed: %s", e)
            out["yaowpedia_error"] = str(e)

    if create_skill or cfg.post_meeting.auto_create_skills:
        out["skill_created"] = False
        out["skill_note"] = "skill creation deferred — implement runtime hook"
    return out


def generate_suggestion(*, state: SentinelSessionState, query: str, mode: str) -> str:
    """Best-effort suggestion synthesis.

    v1: heuristic templating from the briefing + last transcript chunks. A
    real implementation would fan this out to an LLM, but keeping it local
    means the overlay still feels alive when the model is rate-limited or
    offline.
    """
    briefing = state.briefing
    briefing_text = render_system_prompt(briefing) if briefing else ""
    tail = state.transcript_text(last=20)

    header = {
        "talking_points": "Talking points",
        "reply": "Suggested reply",
        "recap": "Recap",
        "objection": "Objection handler",
        "freeform": "Suggestion",
    }.get(mode, "Suggestion")

    parts = [f"## {header}", f"Query: {query}"]
    if briefing and briefing.goal:
        parts.append(f"Goal: {briefing.goal}")
    if briefing and briefing.persona:
        parts.append(f"Persona: {briefing.persona}")
    if tail:
        parts.append("Last said:")
        parts.append(_truncate(tail, 600))
    parts.append("(Heuristic stub — wire an LLM in runtime.generate_suggestion for production.)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _persist_transcript(state: SentinelSessionState) -> Optional[Path]:
    text = state.transcript_text()
    if not text:
        return None
    _TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{state.meeting_id or state.session_id}-{int(time.time())}.txt"
    p = _TRANSCRIPT_DIR / name
    try:
        p.write_text(text, encoding="utf-8")
    except OSError as e:
        logger.warning("sentinel: failed to persist transcript: %s", e)
        return None
    return p


def _save_yaowpedia(meeting_id: str, summary: str, transcript: str, actions: list[str]) -> Path:
    """Save a structured note. v1 writes to ~/.hermes/yaowpedia/sentinel/.

    If the user's actual yaowpedia integration looks different (alternate
    path, structured frontmatter, etc.) they can override by editing this
    function — we keep it self-contained.
    """
    yp = Path.home() / ".hermes" / "yaowpedia" / "sentinel"
    yp.mkdir(parents=True, exist_ok=True)
    p = yp / f"{meeting_id}.md"
    actions_list = [f"- {a}" for a in actions] if actions else ["_(none extracted)_"]
    body = [
        f"# Meeting: {meeting_id}",
        "",
        "## Summary",
        summary or "_(no transcript captured)_",
        "",
        "## Action items",
    ] + actions_list + [
        "",
        "## Transcript",
        transcript[:20000] + ("\n…" if len(transcript) > 20000 else ""),
    ]
    p.write_text("\n".join(body), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Heuristic NLP — kept dumb on purpose
# ---------------------------------------------------------------------------

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _heuristic_summary(text: str) -> str:
    if not text:
        return ""
    sentences = [s.strip() for s in text.replace("\n", " ").split(". ") if s.strip()]
    if not sentences:
        return _truncate(text, 600)
    head = sentences[: min(3, len(sentences))]
    tail = sentences[-min(2, len(sentences)) :] if len(sentences) > 5 else []
    bits = head + (["…"] if tail else []) + tail
    return _truncate(". ".join(bits), 800)


_ACTION_HINTS = (
    "i'll ",
    "i will ",
    "we'll ",
    "we will ",
    "todo",
    "action item",
    "follow up",
    "follow-up",
    "next step",
    "by friday",
    "by monday",
    "by eod",
)


def _extract_actions(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in text.replace(".", ".\n").split("\n"):
        low = line.lower()
        if any(h in low for h in _ACTION_HINTS):
            clean = line.strip(" .-")
            if clean and clean not in seen:
                out.append(_truncate(clean, 240))
                seen.add(clean)
        if len(out) >= 12:
            break
    return out
