"""FastAPI overlay server — REST + WebSocket bridge between Hermes and the overlay.

Design goals:

* **Remote-first.** ``overlay.host`` defaults to ``127.0.0.1`` for local-only
  use; setting it to ``0.0.0.0`` lets an overlay running on a different
  machine connect over LAN/WAN.
* **Optional auth.** ``overlay.api_key`` (when set) is required as either a
  ``Authorization: Bearer <key>`` header or an ``?api_key=...`` query string.
* **Static UI.** Serves the contents of ``remote-overlay/`` at ``/`` so the
  user can just point a browser at the host and use the same UI Tauri wraps.
* **Graceful degrade.** When ``fastapi`` / ``uvicorn`` are missing we log a
  one-time warning and noop — the rest of the plugin keeps working without
  the overlay.

The server runs in a daemon thread; ``ensure_overlay_server`` is idempotent.
Tool handlers / runtime push events through :func:`broadcast_suggestion`,
:func:`broadcast_transcript`, and :func:`broadcast_status`, which fan out to
all connected WebSocket clients.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from sentinel.config import SentinelConfig
from sentinel.state import Suggestion, TranscriptChunk, _REGISTRY, current_state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server state (process-wide singleton)
# ---------------------------------------------------------------------------

_SERVER_LOCK = threading.Lock()
_SERVER_THREAD: Optional[threading.Thread] = None
_SERVER_LOOP: Optional[asyncio.AbstractEventLoop] = None
_WS_CLIENTS: Set[Any] = set()
_OVERLAY_CFG: Optional[SentinelConfig] = None

# Per-client preferences (keyed by id(ws)) — last selected session, context
# toggles, settings sent from the overlay. Used so the server can route
# remote audio chunks to the right sentinel session.
_CLIENT_PREFS: Dict[int, Dict[str, Any]] = {}


_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_REMOTE_OVERLAY_DIR = _PLUGIN_DIR / "remote-overlay"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_json(obj: Any) -> str:
    if dataclasses.is_dataclass(obj):
        obj = dataclasses.asdict(obj)
    return json.dumps(obj, ensure_ascii=False, default=str)


async def _broadcast(payload: Dict[str, Any]) -> None:
    """Send *payload* to all connected WebSocket clients."""
    if not _WS_CLIENTS:
        return
    msg = json.dumps(payload, default=str)
    dead: List[Any] = []
    for ws in list(_WS_CLIENTS):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _WS_CLIENTS.discard(ws)


def _schedule(coro) -> None:  # noqa: ANN001
    if _SERVER_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, _SERVER_LOOP)
    except Exception as e:
        logger.debug("sentinel: overlay schedule failed: %s", e)


# ---------------------------------------------------------------------------
# Public broadcast helpers (callable from any thread / loop)
# ---------------------------------------------------------------------------

async def broadcast_suggestion(s: Suggestion) -> None:
    payload = {
        "kind": "suggestion",
        "id": s.id,
        "mode": s.mode,
        "text": s.text,
        "ts": s.ts,
    }
    if _SERVER_LOOP and asyncio.get_event_loop() is _SERVER_LOOP:
        await _broadcast(payload)
    else:
        _schedule(_broadcast(payload))


async def broadcast_transcript(t: TranscriptChunk) -> None:
    payload = {
        "kind": "transcript",
        "text": t.text,
        "channel": t.channel,
        "speaker": t.speaker,
        "ts": t.ts,
        "is_final": t.is_final,
    }
    if _SERVER_LOOP and asyncio.get_event_loop() is _SERVER_LOOP:
        await _broadcast(payload)
    else:
        _schedule(_broadcast(payload))


async def broadcast_status() -> None:
    state = current_state()
    payload = {"kind": "status", "state": state.snapshot() if state else None}
    if _SERVER_LOOP and asyncio.get_event_loop() is _SERVER_LOOP:
        await _broadcast(payload)
    else:
        _schedule(_broadcast(payload))


async def broadcast_audio_sources(sources: List[Dict[str, Any]]) -> None:
    """Push the current per-application audio source list to all overlays.

    ``sources`` is the canonical shape produced by the Rust audio engine:
    ``[{id, name, icon, enabled}, ...]``. The list is cached on the active
    session state so subsequent ``status`` snapshots include it as well.
    """
    state = current_state()
    if state is not None:
        with state._lock:
            state.audio_sources = list(sources or [])
    payload = {
        "kind": "audio_sources_list",
        "type": "audio_sources_list",
        "sources": list(sources or []),
    }
    if _SERVER_LOOP and asyncio.get_event_loop() is _SERVER_LOOP:
        await _broadcast(payload)
    else:
        _schedule(_broadcast(payload))


# ---------------------------------------------------------------------------
# Overlay command (show/hide/toggle/position)
# ---------------------------------------------------------------------------

async def overlay_command(action: str, position: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Broadcast an overlay-control message to all WS clients.

    The Tauri overlay (or any web client) listens for ``overlay_command``
    and reacts. We don't directly drive the OS window from Python — that's
    Tauri's job.
    """
    payload: Dict[str, Any] = {"kind": "overlay_command", "action": action}
    if position:
        payload["position"] = position
    await _broadcast(payload)
    return {"clients_notified": len(_WS_CLIENTS)}


# ---------------------------------------------------------------------------
# Remote audio + control message handling
# ---------------------------------------------------------------------------

def _list_sessions_payload() -> Dict[str, Any]:
    """Return a JSON-safe sessions list for the overlay client."""
    sessions: List[Dict[str, Any]] = []
    for sid, st in list(_REGISTRY.items()):
        sessions.append({
            "session_id": sid,
            "meeting_id": st.meeting_id,
            "is_active": st.is_active,
            "backend": st.backend,
            "started_at": st.started_at,
            "transcript_lines": len(st.transcript),
        })
    return {"kind": "sessions", "sessions": sessions}


async def _send_sessions(ws: Any) -> None:
    try:
        await ws.send_text(json.dumps(_list_sessions_payload(), default=str))
    except Exception as e:
        logger.debug("sentinel: send_sessions failed: %s", e)


async def _route_remote_audio(ws: Any, obj: Dict[str, Any]) -> None:
    """Forward a remote audio chunk into the active session's backend.

    Schema:
        {"kind": "audio_chunk", "channel": "mic"|"system",
         "data": "<base64 PCM16 little-endian mono>",
         "sample_rate": 16000, "ts": <epoch_ms>}

    Resolution:
      1. Use the client's selected session (if any).
      2. Otherwise pick the most-recently-active sentinel session.
      3. If no session exists, drop quietly with a debug log.
    """
    prefs = _CLIENT_PREFS.get(id(ws), {})
    sess_id = prefs.get("session_id")
    state = None
    if sess_id:
        state = _REGISTRY.get(sess_id)
    if state is None:
        state = current_state()
    if state is None or not state.is_active:
        # No live session — log once and drop the chunk.
        return

    raw_b64 = obj.get("data") or ""
    if not raw_b64:
        return
    try:
        pcm = base64.b64decode(raw_b64)
    except Exception as e:
        logger.debug("sentinel: bad audio_chunk base64: %s", e)
        return
    channel = str(obj.get("channel") or "mic")

    # Hand the PCM to the running session worker's router. We deliberately
    # don't import _SessionWorker symbols here to avoid a circular import;
    # _backend_task is the opaque handle stashed in state.
    worker = getattr(state, "_backend_task", None)
    router = getattr(worker, "router", None) if worker is not None else None
    if router is None:
        return

    try:
        coro = router.send_audio(pcm, channel=channel)
    except Exception as e:
        logger.debug("sentinel: send_audio dispatch failed: %s", e)
        return
    # Schedule on the worker's loop so we don't block the overlay loop.
    worker_loop = getattr(worker, "loop", None)
    if worker_loop and worker_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(coro, worker_loop)
        except Exception as e:
            logger.debug("sentinel: cross-loop dispatch failed: %s", e)
    else:
        # Worker has no loop — drop and let the coroutine GC.
        try:
            coro.close()
        except Exception:
            pass


async def _dispatch_tool_call(ws: Any, obj: Dict[str, Any]) -> None:
    """Run a sentinel_* tool requested over WS, send the result back."""
    tool = (obj.get("tool") or "").strip()
    args = obj.get("args") or {}
    if not tool.startswith("sentinel_"):
        await ws.send_text(json.dumps({"kind": "error", "message": f"unknown tool {tool!r}"}))
        return
    try:
        from sentinel.tools import handlers as _h
        handler = getattr(_h, "handle_" + tool, None)
    except Exception as e:
        await ws.send_text(json.dumps({"kind": "error", "message": f"handler import failed: {e}"}))
        return
    if handler is None:
        await ws.send_text(json.dumps({"kind": "error", "message": f"no handler for {tool}"}))
        return
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, lambda: handler(args))
        # Tool handlers return JSON strings — parse so the client gets a dict.
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except Exception:
            parsed = {"raw": result}
        await ws.send_text(json.dumps({"kind": "tool_result", "tool": tool, "result": parsed}, default=str))
    except Exception as e:
        await ws.send_text(json.dumps({"kind": "error", "message": f"{tool} failed: {e}"}))


async def _handle_client_message(ws: Any, obj: Dict[str, Any]) -> None:
    kind = (obj.get("kind") or obj.get("type") or "").lower()
    prefs = _CLIENT_PREFS.setdefault(id(ws), {})

    if kind == "ping":
        await ws.send_text(json.dumps({"kind": "pong"}))
    elif kind == "request_status":
        await broadcast_status()
    elif kind == "list_sessions":
        await _send_sessions(ws)
    elif kind in ("session_switch", "select_session"):
        prefs["session_id"] = (obj.get("session_id") or "") or None
        await broadcast_status()
    elif kind == "context_toggle":
        ctx = obj.get("contexts") or {}
        if isinstance(ctx, dict):
            prefs["contexts"] = {**prefs.get("contexts", {}), **{k: bool(v) for k, v in ctx.items()}}
            state = current_state()
            if state is not None:
                with state._lock:
                    state.context_flags.update({k: bool(v) for k, v in ctx.items()})
    elif kind == "toggle_context":
        # Single-context toggle from the overlay: {context: "docs", enabled: true}.
        ctx_key = (obj.get("context") or "").strip()
        if ctx_key:
            enabled = bool(obj.get("enabled", True))
            prefs.setdefault("contexts", {})[ctx_key] = enabled
            state = current_state()
            if state is not None:
                with state._lock:
                    state.context_flags[ctx_key] = enabled
    elif kind == "settings_update":
        s = obj.get("settings") or {}
        if isinstance(s, dict):
            prefs["settings"] = {**prefs.get("settings", {}), **s}
    elif kind == "audio_mute":
        # {channel: "mic"|"system", muted: bool} — update state + broadcast.
        channel = (obj.get("channel") or "").lower()
        muted = bool(obj.get("muted", True))
        state = current_state()
        if state is not None:
            with state._lock:
                if channel == "mic":
                    state.mic_muted = muted
                elif channel == "system":
                    state.system_muted = muted
        # Forward to audio capture if it exposes a hook.
        try:
            from sentinel import audio_capture as _ac
            apply = getattr(_ac, "set_channel_mute", None)
            if callable(apply):
                apply(channel=channel, muted=muted)
        except Exception as e:
            logger.debug("sentinel: audio mute apply failed: %s", e)
        await broadcast_status()
    elif kind == "audio_source_filter":
        sources = obj.get("sources") or {}
        if isinstance(sources, dict):
            normalised = {str(k): bool(v) for k, v in sources.items()}
            state = current_state()
            if state is not None:
                with state._lock:
                    state.audio_source_filter = normalised
                    # Mirror enabled/disabled into the cached source list.
                    for s in state.audio_sources:
                        sid = str(s.get("id") or s.get("name") or "")
                        if sid in normalised:
                            s["enabled"] = normalised[sid]
            try:
                from sentinel import audio_capture as _ac
                apply = getattr(_ac, "set_source_filter", None)
                if callable(apply):
                    apply(normalised)
            except Exception as e:
                logger.debug("sentinel: source filter apply failed: %s", e)
            await broadcast_status()
    elif kind == "audio_sources_request":
        # Re-emit the cached source list (or empty) so the overlay refreshes.
        state = current_state()
        sources = list(state.audio_sources) if state is not None else []
        try:
            from sentinel import audio_capture as _ac
            enumerate_fn = getattr(_ac, "enumerate_sources", None)
            if callable(enumerate_fn):
                fresh = enumerate_fn()
                if fresh:
                    sources = list(fresh)
        except Exception as e:
            logger.debug("sentinel: enumerate_sources failed: %s", e)
        await broadcast_audio_sources(sources)
    elif kind == "set_backend":
        new_backend = (obj.get("backend") or "").strip().lower()
        prefs["pending_backend"] = new_backend
        state = current_state()
        if new_backend and state is not None:
            with state._lock:
                state.backend = new_backend
        await broadcast_status()
    elif kind == "set_vad_sensitivity":
        try:
            level = float(obj.get("level", 0.5))
        except (TypeError, ValueError):
            level = 0.5
        level = max(0.0, min(1.0, level))
        state = current_state()
        if state is not None:
            with state._lock:
                state.vad_sensitivity = level
        await broadcast_status()
    elif kind in ("audio_chunk",):
        await _route_remote_audio(ws, obj)
    elif kind == "remote_audio_start":
        prefs["remote_audio"] = True
        prefs["audio_sample_rate"] = int(obj.get("sample_rate") or 16000)
    elif kind == "remote_audio_stop":
        prefs["remote_audio"] = False
    elif kind == "tool_call":
        await _dispatch_tool_call(ws, obj)
    elif kind == "suggest":
        # Convenience shortcut: maps to sentinel_suggest.
        await _dispatch_tool_call(ws, {
            "tool": "sentinel_suggest",
            "args": {"query": obj.get("query", ""), "mode": obj.get("mode", "freeform")},
        })
    elif kind == "suggestion_action":
        # No server-side action yet; log and ack so clients can rely on the round-trip.
        logger.debug("sentinel: suggestion_action %s id=%s", obj.get("action"), obj.get("id"))
        await ws.send_text(json.dumps({"kind": "ack", "action": obj.get("action"), "id": obj.get("id")}))
    elif kind == "overlay_hello":
        # Client identification — store and noop.
        prefs["client"] = obj.get("client", "unknown")


# ---------------------------------------------------------------------------
# FastAPI app construction
# ---------------------------------------------------------------------------

def _build_app(cfg: SentinelConfig):
    """Build the FastAPI app. Imported lazily — fastapi may not be installed."""
    from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Hermes Sentinel Overlay", version="1.0.0")
    api_key = (cfg.overlay.api_key or "").strip() if hasattr(cfg.overlay, "api_key") else ""

    def _check_auth(authorization: Optional[str], api_key_q: Optional[str]) -> None:
        if not api_key:
            return
        provided: Optional[str] = None
        if authorization:
            authorization = authorization.strip()
            if authorization.lower().startswith("bearer "):
                provided = authorization[7:].strip()
            else:
                provided = authorization
        if not provided and api_key_q:
            provided = api_key_q
        if api_key and provided != api_key:
            raise HTTPException(status_code=401, detail="invalid or missing api key")

    # ---------------- REST routes ----------------

    @app.get("/api/sentinel/status")
    async def status(
        authorization: Optional[str] = Header(default=None),
        api_key_q: Optional[str] = Query(default=None, alias="api_key"),
    ):
        _check_auth(authorization, api_key_q)
        st = current_state()
        return {"ok": True, "state": st.snapshot() if st else None}

    @app.post("/api/sentinel/suggest")
    async def suggest(
        authorization: Optional[str] = Header(default=None),
        api_key_q: Optional[str] = Query(default=None, alias="api_key"),
        body: dict = None,
    ):
        _check_auth(authorization, api_key_q)
        from sentinel.tools.handlers import handle_sentinel_suggest
        if body is None:
            raise HTTPException(status_code=400, detail="request body required")
        result_str = await asyncio.get_running_loop().run_in_executor(
            None, lambda: handle_sentinel_suggest(body)
        )
        return JSONResponse(json.loads(result_str))

    @app.post("/api/sentinel/overlay")
    async def overlay(
        authorization: Optional[str] = Header(default=None),
        api_key_q: Optional[str] = Query(default=None, alias="api_key"),
        body: dict = None,
    ):
        _check_auth(authorization, api_key_q)
        if body is None:
            raise HTTPException(status_code=400, detail="request body required")
        return await overlay_command(body.get("action", "toggle"), body.get("position"))

    @app.get("/api/sentinel/transcript")
    async def transcript(
        last: int = 50,
        authorization: Optional[str] = Header(default=None),
        api_key_q: Optional[str] = Query(default=None, alias="api_key"),
    ):
        _check_auth(authorization, api_key_q)
        st = current_state()
        if not st:
            return {"ok": True, "transcript": []}
        with st._lock:
            chunks = list(st.transcript[-last:])
        return {
            "ok": True,
            "transcript": [
                {
                    "text": c.text,
                    "channel": c.channel,
                    "speaker": c.speaker,
                    "ts": c.ts,
                    "is_final": c.is_final,
                }
                for c in chunks
            ],
        }

    # ---------------- WebSocket ----------------

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        # Auth via subprotocol or query string for browser clients.
        provided = ws.query_params.get("api_key")
        if not provided:
            auth_header = ws.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
        if api_key and provided != api_key:
            await ws.close(code=4401)
            return
        await ws.accept()
        _WS_CLIENTS.add(ws)
        client_id = id(ws)
        _CLIENT_PREFS[client_id] = {
            "session_id": None,
            "contexts": {"docs": True, "persona": True, "goal": True, "history": True},
            "settings": {},
            "remote_audio": False,
            "audio_sample_rate": 16000,
        }
        try:
            # Send initial status so the client renders immediately.
            await broadcast_status()
            await _send_sessions(ws)
            while True:
                try:
                    msg = await ws.receive_text()
                except WebSocketDisconnect:
                    break
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                await _handle_client_message(ws, obj)
        finally:
            _WS_CLIENTS.discard(ws)
            _CLIENT_PREFS.pop(client_id, None)

    # ---------------- Static UI ----------------

    # Mount AFTER WebSocket so /ws never matches StaticFiles fallthrough.
    # The root "/" route intercepts index.html before StaticFiles tries to
    # serve it as a plain file.

    if _REMOTE_OVERLAY_DIR.is_dir():
        webui = _REMOTE_OVERLAY_DIR / "webui"
        if webui.is_dir():
            app.mount("/webui", StaticFiles(directory=str(webui), html=True), name="webui")

            @app.get("/webui", response_class=RedirectResponse)
            async def webui_index():
                return RedirectResponse(url="/webui/", status_code=302)

        @app.get("/", response_class=HTMLResponse)
        async def root_index():
            idx = _REMOTE_OVERLAY_DIR / "index.html"
            if idx.is_file():
                return FileResponse(str(idx))
            return HTMLResponse("<h1>Hermes Sentinel</h1><p>Overlay UI not built.</p>")

    return app


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def ensure_overlay_server(cfg: SentinelConfig) -> bool:
    """Idempotent — start the overlay server in a daemon thread.

    Returns True if the server is (now) running, False if startup was
    skipped (e.g. fastapi/uvicorn missing or already running on this port).
    """
    global _SERVER_THREAD, _SERVER_LOOP, _OVERLAY_CFG

    with _SERVER_LOCK:
        if _SERVER_THREAD and _SERVER_THREAD.is_alive():
            return True

        try:
            import fastapi  # noqa: F401
            import uvicorn
        except ImportError:
            logger.warning(
                "sentinel: fastapi/uvicorn not installed — overlay disabled. "
                "pip install fastapi uvicorn"
            )
            return False

        _OVERLAY_CFG = cfg
        ready = threading.Event()

        def _run() -> None:
            global _SERVER_LOOP
            _SERVER_LOOP = asyncio.new_event_loop()
            asyncio.set_event_loop(_SERVER_LOOP)
            try:
                app = _build_app(cfg)
                uv_cfg = uvicorn.Config(
                    app=app,
                    host=cfg.overlay.host,
                    port=cfg.overlay.port,
                    log_level="warning",
                    loop="asyncio",
                )
                server = uvicorn.Server(uv_cfg)
                logger.info(
                    "sentinel: overlay server listening on http://%s:%d",
                    cfg.overlay.host, cfg.overlay.port,
                )
                ready.set()
                _SERVER_LOOP.run_until_complete(server.serve())
            except Exception as e:
                logger.warning("sentinel: overlay server crashed: %s", e)
                ready.set()

        _SERVER_THREAD = threading.Thread(target=_run, name="sentinel-overlay", daemon=True)
        _SERVER_THREAD.start()
        ready.wait(timeout=5)
        return True
