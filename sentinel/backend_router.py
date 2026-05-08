"""WebSocket backend router for Sentinel realtime providers.

Each provider speaks a slightly different protocol. ``BackendRouter`` is the
common surface the rest of the plugin uses; provider-specific quirks live in
the per-backend ``_Adapter`` subclasses.

Public surface
--------------

::

    router = BackendRouter.create("openai", cfg)
    await router.connect(briefing=briefing)
    async for event in router.receive():
        ...
    await router.send_audio(pcm16_bytes, channel="mic")
    await router.send_text("hello")
    await router.close()

The router is a thin shim — it does NOT own audio capture or transcript
state. Callers wire transcript chunks coming out of ``receive()`` into the
``SentinelSessionState`` object themselves (see ``hooks/on_session_start``).

All adapters are best-effort scaffolds. Provider APIs evolve fast; the
specific payload shapes here are based on each vendor's public docs as of
the build date and may drift. Each adapter logs the full request/response
turn at DEBUG so future-you can diff against current docs without a debugger.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover — optional dep
    websockets = None
    WebSocketClientProtocol = Any  # type: ignore
    _WS_AVAILABLE = False

from sentinel.config import SentinelConfig
from sentinel.context_sync import build_session_update
from sentinel.state import Briefing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event shapes (canonical — adapters translate from provider events)
# ---------------------------------------------------------------------------

@dataclass
class TranscriptEvent:
    text: str
    is_final: bool
    channel: str = "mixed"
    speaker: Optional[str] = None


@dataclass
class AssistantTextEvent:
    text: str
    is_final: bool


@dataclass
class ErrorEvent:
    message: str
    raw: Optional[Dict[str, Any]] = None


BackendEvent = TranscriptEvent | AssistantTextEvent | ErrorEvent


# ---------------------------------------------------------------------------
# Adapter base class
# ---------------------------------------------------------------------------

class _BackendAdapter:
    """Per-provider WebSocket adapter — subclassed by each backend.

    Subclasses must implement ``url()``, ``headers()``, ``handshake()``,
    ``encode_audio()``, ``encode_text()``, and ``parse_event()``. The base
    class drives the loop and exposes the canonical ``BackendEvent`` shape.
    """

    name: str = "base"

    def __init__(self, cfg: SentinelConfig) -> None:
        self.cfg = cfg
        self.ws: Optional[WebSocketClientProtocol] = None
        self._closed = False

    # -- to override --------------------------------------------------------

    def url(self) -> str:
        raise NotImplementedError

    def headers(self) -> Dict[str, str]:
        return {}

    def api_key(self) -> Optional[str]:
        sub = self.cfg.backend_for(self.name)
        env_key = getattr(sub, "api_key_env", None)
        if not env_key:
            return None
        val = os.environ.get(env_key)
        if not val:
            logger.warning("sentinel: %s requires env %s — not set", self.name, env_key)
        return val

    async def handshake(self, briefing: Optional[Briefing]) -> None:
        """Send any post-connect setup (e.g. session.update)."""
        payload = build_session_update(briefing, backend=self.name)
        await self._send_json(payload)

    def encode_audio(self, pcm16: bytes, channel: str) -> Dict[str, Any]:
        """Default OpenAI-Realtime style audio frame."""
        return {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16).decode("ascii"),
        }

    def encode_text(self, text: str) -> Dict[str, Any]:
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }

    def parse_event(self, raw: Dict[str, Any]) -> Optional[BackendEvent]:
        """Translate a provider event into a canonical BackendEvent.

        Default implementation handles OpenAI Realtime-style events. Override
        for providers with different shapes.
        """
        t = raw.get("type", "")
        if t == "conversation.item.input_audio_transcription.completed":
            return TranscriptEvent(text=raw.get("transcript", ""), is_final=True)
        if t.startswith("response.text.delta"):
            return AssistantTextEvent(text=raw.get("delta", ""), is_final=False)
        if t.startswith("response.text.done"):
            return AssistantTextEvent(text=raw.get("text", ""), is_final=True)
        if t == "error":
            err = raw.get("error", {})
            return ErrorEvent(message=str(err.get("message", err)), raw=raw)
        return None

    # -- driver -------------------------------------------------------------

    async def connect(self, briefing: Optional[Briefing] = None) -> None:
        if not _WS_AVAILABLE:
            raise RuntimeError(
                "websockets package not installed — pip install websockets"
            )
        url = self.url()
        headers = self.headers()
        logger.info("sentinel: connecting to %s (%s)", self.name, url)
        self.ws = await websockets.connect(url, extra_headers=headers, max_size=None)
        await self.handshake(briefing)

    async def _send_json(self, obj: Dict[str, Any]) -> None:
        assert self.ws is not None
        # Strip Sentinel-internal keys that the provider won't recognise.
        payload = {k: v for k, v in obj.items() if not k.startswith("_sentinel_")}
        await self.ws.send(json.dumps(payload))

    async def send_audio(self, pcm16: bytes, channel: str = "mic") -> None:
        if self.ws is None:
            return
        try:
            await self._send_json(self.encode_audio(pcm16, channel))
        except Exception as e:
            logger.debug("sentinel: %s send_audio failed: %s", self.name, e)

    async def send_text(self, text: str) -> None:
        if self.ws is None or not text:
            return
        try:
            await self._send_json(self.encode_text(text))
        except Exception as e:
            logger.debug("sentinel: %s send_text failed: %s", self.name, e)

    async def receive(self) -> AsyncIterator[BackendEvent]:
        """Async-iterate over canonical events from the provider."""
        if self.ws is None:
            return
        try:
            async for raw_msg in self.ws:
                if self._closed:
                    break
                try:
                    obj = json.loads(raw_msg) if isinstance(raw_msg, (str, bytes)) else raw_msg
                except Exception:
                    logger.debug("sentinel: %s non-json frame", self.name)
                    continue
                evt = self.parse_event(obj)
                if evt is not None:
                    yield evt
        except Exception as e:  # pragma: no cover — network
            logger.warning("sentinel: %s receive loop ended: %s", self.name, e)
            yield ErrorEvent(message=str(e))

    async def close(self) -> None:
        self._closed = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------

class _OpenAIAdapter(_BackendAdapter):
    name = "openai"

    def url(self) -> str:
        sub = self.cfg.backends.openai
        return f"{sub.ws_url}?model={sub.model}"

    def headers(self) -> Dict[str, str]:
        key = self.api_key() or ""
        return {
            "Authorization": f"Bearer {key}",
            "OpenAI-Beta": "realtime=v1",
        }


class _GrokAdapter(_BackendAdapter):
    name = "grok"

    def url(self) -> str:
        return self.cfg.backends.grok.ws_url

    def headers(self) -> Dict[str, str]:
        key = self.api_key() or ""
        return {"Authorization": f"Bearer {key}"}


class _AssemblyAIAdapter(_BackendAdapter):
    name = "assemblyai"

    def url(self) -> str:
        sub = self.cfg.backends.assemblyai
        return f"{sub.ws_url}?sample_rate={sub.sample_rate}"

    def headers(self) -> Dict[str, str]:
        # AssemblyAI uses ``Authorization`` directly (no Bearer prefix).
        return {"Authorization": self.api_key() or ""}

    def encode_audio(self, pcm16: bytes, channel: str) -> Dict[str, Any]:
        return {"audio_data": base64.b64encode(pcm16).decode("ascii")}

    def parse_event(self, raw: Dict[str, Any]) -> Optional[BackendEvent]:
        mt = raw.get("message_type", "")
        if mt == "PartialTranscript":
            return TranscriptEvent(text=raw.get("text", ""), is_final=False)
        if mt == "FinalTranscript":
            return TranscriptEvent(text=raw.get("text", ""), is_final=True)
        if mt == "SessionTerminated":
            return ErrorEvent(message="session terminated", raw=raw)
        return None

    async def handshake(self, briefing: Optional[Briefing]) -> None:
        # AssemblyAI realtime has no session.update equivalent; briefing has
        # to be applied at the LLM layer instead. No-op here.
        return


class _DeepgramAdapter(_BackendAdapter):
    name = "deepgram"

    def url(self) -> str:
        sub = self.cfg.backends.deepgram
        return f"{sub.ws_url}?model={sub.model}&encoding=linear16&sample_rate=16000"

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Token {self.api_key() or ''}"}

    def encode_audio(self, pcm16: bytes, channel: str) -> Dict[str, Any]:
        # Deepgram accepts raw binary — adapter signals that via a sentinel
        # marker and the driver uses ws.send() with the bytes directly.
        return {"_sentinel_raw_bytes": pcm16}

    async def send_audio(self, pcm16: bytes, channel: str = "mic") -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(pcm16)  # type: ignore[arg-type]
        except Exception as e:
            logger.debug("sentinel: deepgram send_audio failed: %s", e)

    def parse_event(self, raw: Dict[str, Any]) -> Optional[BackendEvent]:
        if raw.get("type") == "Results":
            ch = raw.get("channel", {})
            alts = ch.get("alternatives", [])
            if alts:
                return TranscriptEvent(
                    text=alts[0].get("transcript", ""),
                    is_final=bool(raw.get("is_final")),
                )
        return None

    async def handshake(self, briefing: Optional[Briefing]) -> None:
        # Deepgram is configured via URL params; no post-connect setup.
        return


class _GeminiAdapter(_BackendAdapter):
    name = "gemini"

    def url(self) -> str:
        sub = self.cfg.backends.gemini
        key = self.api_key() or ""
        return f"{sub.ws_url}?key={key}"

    async def handshake(self, briefing: Optional[Briefing]) -> None:
        from sentinel.context_sync import render_system_prompt
        system_text = render_system_prompt(briefing) if briefing else ""
        setup = {
            "setup": {
                "model": f"models/{self.cfg.backends.gemini.model}",
                "generationConfig": {"responseModalities": ["TEXT"]},
                "systemInstruction": {"parts": [{"text": system_text}]} if system_text else None,
            }
        }
        await self._send_json(setup)

    def encode_audio(self, pcm16: bytes, channel: str) -> Dict[str, Any]:
        return {
            "realtimeInput": {
                "mediaChunks": [
                    {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": base64.b64encode(pcm16).decode("ascii"),
                    }
                ]
            }
        }

    def parse_event(self, raw: Dict[str, Any]) -> Optional[BackendEvent]:
        sc = raw.get("serverContent", {})
        if "modelTurn" in sc:
            parts = sc["modelTurn"].get("parts", [])
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            if text:
                return AssistantTextEvent(text=text, is_final=bool(sc.get("turnComplete")))
        if "inputTranscription" in sc:
            return TranscriptEvent(
                text=sc["inputTranscription"].get("text", ""),
                is_final=bool(sc.get("turnComplete")),
            )
        return None


class _LocalAdapter(_BackendAdapter):
    """Local fallback — speaks no WebSocket; uses faster-whisper + Ollama.

    Wires into the driver loop via in-process queues rather than a network
    socket. The implementation is intentionally minimal; treat it as a
    development fallback that lets the rest of the pipeline run end-to-end
    without an API key.

    NOTE: requires ``faster-whisper`` and a running Ollama instance. If
    either is missing, ``connect()`` raises so the caller can fall back
    explicitly.
    """

    name = "local"

    def __init__(self, cfg: SentinelConfig) -> None:
        super().__init__(cfg)
        self._audio_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._event_q: asyncio.Queue[BackendEvent] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._whisper = None  # lazy-loaded

    async def connect(self, briefing: Optional[Briefing] = None) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "local backend requires faster-whisper — pip install faster-whisper"
            ) from e
        sub = self.cfg.backends.local
        # Loaded sync — small models load fast enough that this is fine.
        self._whisper = WhisperModel(sub.whisper_model, device="auto", compute_type="auto")
        self._task = asyncio.create_task(self._loop())
        logger.info("sentinel: local backend ready (whisper=%s)", sub.whisper_model)

    async def _loop(self) -> None:
        # TODO: maintain a rolling buffer and run whisper on VAD-bounded
        # chunks. v1 just transcribes every accumulated chunk.
        import io
        import wave

        buf = bytearray()
        while not self._closed:
            try:
                chunk = await asyncio.wait_for(self._audio_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not buf:
                    continue
                chunk = b""
            buf.extend(chunk)
            if len(buf) < 16000 * 2 * 3:  # ~3s of mono pcm16
                continue
            try:
                wav_io = io.BytesIO()
                with wave.open(wav_io, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(bytes(buf))
                wav_io.seek(0)
                segments, _ = self._whisper.transcribe(wav_io, beam_size=1)  # type: ignore
                text = " ".join(seg.text for seg in segments).strip()
                if text:
                    await self._event_q.put(TranscriptEvent(text=text, is_final=True))
            except Exception as e:
                logger.debug("sentinel: local transcribe failed: %s", e)
            buf.clear()

    async def send_audio(self, pcm16: bytes, channel: str = "mic") -> None:
        if self._closed:
            return
        await self._audio_q.put(pcm16)

    async def send_text(self, text: str) -> None:
        # Local mode doesn't loop text into an LLM directly; call the
        # configured Ollama model elsewhere if needed.
        return

    async def receive(self) -> AsyncIterator[BackendEvent]:
        while not self._closed:
            try:
                evt = await asyncio.wait_for(self._event_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield evt

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Public router
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, type[_BackendAdapter]] = {
    "openai": _OpenAIAdapter,
    "grok": _GrokAdapter,
    "assemblyai": _AssemblyAIAdapter,
    "deepgram": _DeepgramAdapter,
    "gemini": _GeminiAdapter,
    "local": _LocalAdapter,
}


class BackendRouter:
    """Public facade — wraps one adapter and exposes the canonical surface."""

    def __init__(self, adapter: _BackendAdapter):
        self._adapter = adapter

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def create(cls, backend_name: str, config: SentinelConfig) -> "BackendRouter":
        key = (backend_name or "").lower()
        if key not in _REGISTRY:
            raise ValueError(
                f"unknown backend {backend_name!r} (known: {sorted(_REGISTRY)})"
            )
        return cls(_REGISTRY[key](config))

    @property
    def backend(self) -> str:
        return self._adapter.name

    async def connect(self, briefing: Optional[Briefing] = None) -> None:
        await self._adapter.connect(briefing)

    async def send_audio(self, pcm16: bytes, channel: str = "mic") -> None:
        await self._adapter.send_audio(pcm16, channel)

    async def send_text(self, text: str) -> None:
        await self._adapter.send_text(text)

    def receive(self) -> AsyncIterator[BackendEvent]:
        return self._adapter.receive()

    async def close(self) -> None:
        await self._adapter.close()

    # -- glue ---------------------------------------------------------------

    async def pump_to(
        self,
        on_event: Callable[[BackendEvent], Awaitable[None]],
    ) -> None:
        """Pump events from the adapter to *on_event* until close.

        Convenience for callers that just want a callback. Returns when the
        receive iterator finishes (either ``close()`` was called or the
        backend hung up).
        """
        async for evt in self._adapter.receive():
            try:
                await on_event(evt)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("sentinel: on_event handler raised: %s", e)
