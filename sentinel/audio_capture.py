"""Dual-channel audio capture for Sentinel.

Two implementations live here:

* :class:`RustAudioCapture` — spawns the Rust ``sentinel-audio`` binary as a
  subprocess and reads framed PCM chunks from stdout. Preferred for stealth
  (system-audio loopback works cross-platform via ``cpal``) and latency
  (<500ms target).

* :class:`PythonAudioCapture` — pure-Python fallback using ``sounddevice``.
  Mic-only; no system-audio loopback because portable Python audio loopback
  on Linux/macOS/Windows isn't a solved problem without OS-specific routing
  (PulseAudio null-sinks, BlackHole, WASAPI loopback).

:func:`open_capture` picks based on ``audio.source`` in ``SentinelConfig``.

The capture surface is async-iterable: ``async for chunk in capture: ...``
yields :class:`AudioChunk`. Backpressure is bounded by an internal
``asyncio.Queue``; if the consumer falls behind, oldest chunks are dropped
before the queue is unbounded — sentinel never wants to OOM the agent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from sentinel.config import AudioConfig, SentinelConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------

@dataclass
class AudioChunk:
    """One frame of captured audio."""

    pcm16: bytes        # raw little-endian PCM16 mono
    channel: str        # "mic" | "system" | "mixed"
    timestamp_ms: int   # capture timestamp from the source clock


# Bound the in-memory queue so a stalled consumer can't grow forever.
_QUEUE_MAX = 200


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _CaptureBase:
    """Common queue + lifecycle plumbing."""

    def __init__(self, cfg: AudioConfig):
        self.cfg = cfg
        self._queue: asyncio.Queue[Optional[AudioChunk]] = asyncio.Queue(_QUEUE_MAX)
        self._closed = False
        self._tasks: list[asyncio.Task] = []

    async def _put(self, chunk: AudioChunk) -> None:
        if self._closed:
            return
        if self._queue.full():
            try:
                _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(chunk)

    async def __aiter__(self) -> AsyncIterator[AudioChunk]:
        while not self._closed:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if chunk is None:
                break
            yield chunk

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        self._closed = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Rust subprocess
# ---------------------------------------------------------------------------

class RustAudioCapture(_CaptureBase):
    """Capture via the Rust ``sentinel-audio`` subprocess.

    Wire format (stdout, repeated):

        u32 le  chunk_id
        u64 le  timestamp_ms
        u8      channel_tag (0=mic, 1=system, 2=mixed)
        u32 le  payload_len
        bytes   payload (PCM16 little-endian mono)
    """

    _CHANNEL_TAGS = {0: "mic", 1: "system", 2: "mixed"}

    def __init__(self, cfg: AudioConfig, binary_path: str):
        super().__init__(cfg)
        self.binary_path = binary_path
        self.proc: Optional[asyncio.subprocess.Process] = None

    async def start(self) -> None:
        args = [
            self.binary_path,
            "--channels", str(self.cfg.channels),
            "--sample-rate", str(self.cfg.sample_rate),
            "--chunk-ms", str(self.cfg.chunk_ms),
        ]
        if self.cfg.vad_enabled:
            args.append("--vad")

        logger.info("sentinel: spawning rust audio: %s", " ".join(args))
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._tasks.append(asyncio.create_task(self._read_stdout()))
        self._tasks.append(asyncio.create_task(self._drain_stderr()))

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        reader = self.proc.stdout
        while not self._closed:
            try:
                header = await reader.readexactly(4 + 8 + 1 + 4)
            except (asyncio.IncompleteReadError, Exception):
                break
            chunk_id, ts_ms, channel_tag, payload_len = struct.unpack("<IQBI", header)
            try:
                payload = await reader.readexactly(payload_len)
            except (asyncio.IncompleteReadError, Exception):
                break
            channel = self._CHANNEL_TAGS.get(int(channel_tag), "mixed")
            await self._put(AudioChunk(pcm16=payload, channel=channel, timestamp_ms=int(ts_ms)))

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        async for line in self.proc.stderr:
            logger.debug("sentinel-audio[err]: %s", line.decode("utf-8", errors="replace").rstrip())

    async def stop(self) -> None:
        await super().stop()
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self.proc.kill()
            except Exception as e:
                logger.debug("sentinel: rust audio stop error: %s", e)
            self.proc = None


# ---------------------------------------------------------------------------
# Python sounddevice fallback
# ---------------------------------------------------------------------------

class PythonAudioCapture(_CaptureBase):
    """Pure-Python fallback. Mic-only — no portable system-audio loopback."""

    def __init__(self, cfg: AudioConfig):
        super().__init__(cfg)
        self._stream = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        try:
            import sounddevice  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "python audio capture requires sounddevice — pip install sounddevice"
            ) from e
        import sounddevice as sd
        import numpy as np

        self._loop = asyncio.get_running_loop()
        sample_rate = self.cfg.sample_rate
        chunk_ms = max(10, self.cfg.chunk_ms)
        chunk_frames = int(sample_rate * chunk_ms / 1000)

        def _cb(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                logger.debug("sentinel: sounddevice status: %s", status)
            if self._closed or self._loop is None:
                return
            # Mono float32 → int16 little-endian.
            mono = indata[:, 0] if indata.ndim > 1 else indata
            pcm = (mono * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
            ts_ms = int((time_info.inputBufferAdcTime if hasattr(time_info, "inputBufferAdcTime") else 0) * 1000)
            chunk = AudioChunk(pcm16=pcm, channel="mic", timestamp_ms=ts_ms)
            try:
                self._loop.call_soon_threadsafe(asyncio.create_task, self._put(chunk))
            except RuntimeError:
                # Loop closed mid-callback — drop quietly.
                pass

        try:
            self._stream = sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=chunk_frames,
                callback=_cb,
            )
            self._stream.start()
            logger.info(
                "sentinel: python audio capture started (mic only, %dHz)", sample_rate
            )
        except Exception as e:
            raise RuntimeError(f"sounddevice failed to start: {e}") from e

    async def stop(self) -> None:
        await super().stop()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _resolve_rust_binary(explicit: Optional[str]) -> Optional[str]:
    """Find the sentinel-audio binary. Returns None if not found."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    bin_name = "sentinel-audio.exe" if sys.platform == "win32" else "sentinel-audio"
    candidates.append(_PLUGIN_DIR / "rust-audio" / "target" / "release" / bin_name)
    candidates.append(_PLUGIN_DIR / "rust-audio" / "target" / "debug" / bin_name)
    on_path = shutil.which(bin_name)
    if on_path:
        candidates.append(Path(on_path))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def open_capture(cfg: SentinelConfig) -> _CaptureBase:
    """Return an unstarted capture instance per ``cfg.audio.source``.

    Args:
        cfg: full SentinelConfig.

    Returns:
        Either ``RustAudioCapture`` or ``PythonAudioCapture``. Caller must
        ``await capture.start()``.

    Raises:
        RuntimeError: when source='rust' is forced but the binary is missing.
    """
    src = (cfg.audio.source or "auto").lower()
    rust = _resolve_rust_binary(cfg.audio.rust_binary_path)

    if src == "rust":
        if not rust:
            raise RuntimeError(
                "audio.source=rust requested but sentinel-audio binary not "
                "found. Build it: scripts/build-rust.sh"
            )
        return RustAudioCapture(cfg.audio, rust)

    if src == "python":
        return PythonAudioCapture(cfg.audio)

    # auto
    if rust:
        return RustAudioCapture(cfg.audio, rust)
    logger.info("sentinel: rust audio binary not found — falling back to python sounddevice")
    return PythonAudioCapture(cfg.audio)
