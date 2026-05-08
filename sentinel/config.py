"""SentinelConfig — Pydantic v2 model + load/save helpers for sentinel.yaml.

Layout matches ``config/sentinel.example.yaml``. Default search order:

  1. ``$HERMES_SENTINEL_CONFIG`` env var (explicit override)
  2. ``~/.hermes/plugins/sentinel/sentinel.yaml`` (preferred)
  3. ``<plugin_dir>/sentinel.yaml`` (next to plugin.yaml — for portable installs)
  4. ``<plugin_dir>/config/sentinel.example.yaml`` (fallback — read-only template)

If none exist, ``load()`` returns the model defaults so the plugin can boot
without a config file. ``save()`` writes back to the first writable location.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# --- Backend sub-configs ----------------------------------------------------

class OpenAIBackendConfig(BaseModel):
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-4o-realtime-preview"
    modalities: List[str] = Field(default_factory=lambda: ["text"])
    ws_url: str = "wss://api.openai.com/v1/realtime"


class GrokBackendConfig(BaseModel):
    api_key_env: str = "XAI_API_KEY"
    model: str = "grok-4.3"
    ws_url: str = "wss://api.x.ai/v1/voice"


class AssemblyAIBackendConfig(BaseModel):
    api_key_env: str = "ASSEMBLYAI_API_KEY"
    sample_rate: int = 16000
    ws_url: str = "wss://api.assemblyai.com/v2/realtime"


class DeepgramBackendConfig(BaseModel):
    api_key_env: str = "DEEPGRAM_API_KEY"
    model: str = "nova-3"
    ws_url: str = "wss://api.deepgram.com/v1/listen"


class GeminiBackendConfig(BaseModel):
    api_key_env: str = "GEMINI_API_KEY"
    model: str = "gemini-2.0-flash-exp"
    ws_url: str = (
        "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage."
        "v1alpha.GenerativeService/BidiGenerateContent"
    )


class LocalBackendConfig(BaseModel):
    """Local fallback — faster-whisper + Ollama. No network keys required."""

    whisper_model: str = "base"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"


class BackendsConfig(BaseModel):
    openai: OpenAIBackendConfig = Field(default_factory=OpenAIBackendConfig)
    grok: GrokBackendConfig = Field(default_factory=GrokBackendConfig)
    assemblyai: AssemblyAIBackendConfig = Field(default_factory=AssemblyAIBackendConfig)
    deepgram: DeepgramBackendConfig = Field(default_factory=DeepgramBackendConfig)
    gemini: GeminiBackendConfig = Field(default_factory=GeminiBackendConfig)
    local: LocalBackendConfig = Field(default_factory=LocalBackendConfig)


# --- Audio / overlay / stealth sub-configs ----------------------------------

class AudioConfig(BaseModel):
    source: str = "auto"  # "auto" | "rust" | "python"
    rust_binary_path: Optional[str] = None
    channels: int = 2  # 1=mic only, 2=system+mic
    sample_rate: int = 16000
    chunk_ms: int = 20
    vad_enabled: bool = True


class OverlaySSLConfig(BaseModel):
    enabled: bool = False
    cert_path: str = ""
    key_path: str = ""


class OverlayWebSocketConfig(BaseModel):
    heartbeat_interval: int = 30


class OverlayConfig(BaseModel):
    enabled: bool = True
    port: int = 18765
    host: str = "127.0.0.1"
    api_key: str = ""
    ssl: OverlaySSLConfig = Field(default_factory=OverlaySSLConfig)
    websocket: OverlayWebSocketConfig = Field(default_factory=OverlayWebSocketConfig)


class StealthConfig(BaseModel):
    hide_from_dock: bool = True
    hide_from_taskbar: bool = True
    screenshot_proof: bool = True
    opacity: float = 0.85


class PreMeetingConfig(BaseModel):
    auto_compress: bool = True
    inject_persona: bool = True
    max_briefing_tokens: int = 2000


class PostMeetingConfig(BaseModel):
    auto_extract_actions: bool = True
    auto_create_skills: bool = False
    save_to_yaowpedia: bool = True


# --- Top-level config -------------------------------------------------------

class SentinelConfig(BaseModel):
    """Top-level plugin configuration."""

    version: str = "1.0.0"
    realtime_backend: str = "openai"
    backends: BackendsConfig = Field(default_factory=BackendsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    overlay: OverlayConfig = Field(default_factory=OverlayConfig)
    stealth: StealthConfig = Field(default_factory=StealthConfig)
    pre_meeting: PreMeetingConfig = Field(default_factory=PreMeetingConfig)
    post_meeting: PostMeetingConfig = Field(default_factory=PostMeetingConfig)

    def backend_for(self, name: Optional[str] = None) -> BaseModel:
        """Return the per-backend sub-config for *name* (defaults to active)."""
        key = (name or self.realtime_backend).lower()
        if not hasattr(self.backends, key):
            raise ValueError(f"unknown backend {name!r}")
        return getattr(self.backends, key)


# --- Resolution + IO --------------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_USER_PATH = PLUGIN_DIR / "sentinel.yaml"
EXAMPLE_PATH = PLUGIN_DIR / "config" / "sentinel.example.yaml"


def _candidate_paths() -> List[Path]:
    paths: List[Path] = []
    env = os.environ.get("HERMES_SENTINEL_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    paths.append(DEFAULT_USER_PATH)
    paths.append(EXAMPLE_PATH)
    return paths


def resolve_config_path() -> Optional[Path]:
    """Return the first existing config file path, or None if none found."""
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


def load(path: Optional[str | Path] = None) -> SentinelConfig:
    """Load and validate sentinel.yaml.

    Args:
        path: optional explicit path. If omitted, uses ``resolve_config_path()``.

    Returns:
        Validated ``SentinelConfig``. If no file is found, returns defaults.
    """
    target: Optional[Path]
    if path is not None:
        target = Path(path).expanduser()
    else:
        target = resolve_config_path()

    if target is None or not target.is_file():
        logger.info("sentinel: no config file found — using defaults")
        return SentinelConfig()

    try:
        with target.open("r", encoding="utf-8") as f:
            raw: Dict[str, Any] = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("sentinel: failed to read %s: %s — using defaults", target, e)
        return SentinelConfig()

    try:
        cfg = SentinelConfig.model_validate(raw)
    except Exception as e:
        logger.warning(
            "sentinel: config %s failed validation (%s) — using defaults", target, e
        )
        return SentinelConfig()
    return cfg


def save(cfg: SentinelConfig, path: Optional[str | Path] = None) -> Path:
    """Persist *cfg* as YAML. Writes to *path* if given, else DEFAULT_USER_PATH.

    Returns the path written to. Raises ``OSError`` on filesystem failure.
    """
    target = Path(path).expanduser() if path else DEFAULT_USER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    with target.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    return target
