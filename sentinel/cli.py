"""``hermes sentinel`` CLI subcommand.

Subcommands:

  * ``hermes sentinel status``           print live session snapshot
  * ``hermes sentinel start <backend>``  start a session from the terminal
  * ``hermes sentinel stop``             stop the active session
  * ``hermes sentinel suggest <text>``   request a suggestion
  * ``hermes sentinel overlay <action>`` show/hide/toggle the overlay
  * ``hermes sentinel history <query>``  search past transcripts
  * ``hermes sentinel doctor``           dependency / config sanity check
  * ``hermes sentinel config show``      print resolved config

Each subcommand calls into the corresponding tool handler so behavior is
uniform between agent-driven and human-driven invocation.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subcommand registration (called by Hermes' CLI plugin loader)
# ---------------------------------------------------------------------------

def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Wire all sentinel subcommands onto *subparser*.

    Args:
        subparser: the ``hermes sentinel`` argparse parser.
    """
    sub = subparser.add_subparsers(dest="sentinel_cmd", metavar="<cmd>")

    sub.add_parser("status", help="Print live session snapshot")

    sp = sub.add_parser("start", help="Start a sentinel session")
    sp.add_argument("backend", choices=["openai", "grok", "assemblyai", "deepgram", "gemini", "local"])
    sp.add_argument("--meeting-id", default=None)
    sp.add_argument("--meeting-url", default=None)
    sp.add_argument("--channels", type=int, choices=[1, 2], default=2)

    stop = sub.add_parser("stop", help="Stop the active session")
    stop.add_argument("--no-extract", action="store_true", help="Skip post-meeting extraction")

    sg = sub.add_parser("suggest", help="Request a suggestion")
    sg.add_argument("text", nargs="+")
    sg.add_argument("--mode", default="freeform",
                    choices=["talking_points", "reply", "recap", "objection", "freeform"])

    ov = sub.add_parser("overlay", help="Show/hide/toggle the overlay")
    ov.add_argument("action", choices=["show", "hide", "toggle", "position"])

    hist = sub.add_parser("history", help="Search past transcripts")
    hist.add_argument("query", nargs="+")
    hist.add_argument("--limit", type=int, default=10)

    sub.add_parser("doctor", help="Run a dependency + config sanity check")

    cfg = sub.add_parser("config", help="Inspect or edit sentinel config")
    cfg_sub = cfg.add_subparsers(dest="config_cmd")
    cfg_sub.add_parser("show", help="Print resolved config")
    cfg_sub.add_parser("path", help="Print resolved config file path")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def sentinel_command(args: argparse.Namespace) -> int:
    """Handler dispatched by ``set_defaults(func=...)``.

    Args:
        args: parsed args.

    Returns:
        Exit code (0 = success).
    """
    cmd = getattr(args, "sentinel_cmd", None)
    if cmd is None:
        print("usage: hermes sentinel <status|start|stop|suggest|overlay|history|doctor|config>")
        return 2

    try:
        if cmd == "status":
            return _cmd_status()
        if cmd == "start":
            return _cmd_start(args)
        if cmd == "stop":
            return _cmd_stop(args)
        if cmd == "suggest":
            return _cmd_suggest(args)
        if cmd == "overlay":
            return _cmd_overlay(args)
        if cmd == "history":
            return _cmd_history(args)
        if cmd == "doctor":
            return _cmd_doctor()
        if cmd == "config":
            return _cmd_config(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    print(f"unknown sentinel subcommand: {cmd}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Subcommand bodies
# ---------------------------------------------------------------------------

def _print_result(s: str) -> int:
    try:
        obj = json.loads(s)
    except Exception:
        print(s)
        return 0
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return 0 if obj.get("success", True) else 1


def _cmd_status() -> int:
    from sentinel.tools.handlers import handle_sentinel_status
    return _print_result(handle_sentinel_status({}))


def _cmd_start(args: argparse.Namespace) -> int:
    from sentinel.tools.handlers import handle_sentinel_start
    payload: Dict[str, Any] = {"backend": args.backend, "channels": args.channels}
    if args.meeting_id:
        payload["meeting_id"] = args.meeting_id
    if args.meeting_url:
        payload["meeting_url"] = args.meeting_url
    return _print_result(handle_sentinel_start(payload))


def _cmd_stop(args: argparse.Namespace) -> int:
    from sentinel.tools.handlers import handle_sentinel_stop
    return _print_result(handle_sentinel_stop({"auto_extract": not args.no_extract}))


def _cmd_suggest(args: argparse.Namespace) -> int:
    from sentinel.tools.handlers import handle_sentinel_suggest
    text = " ".join(args.text)
    return _print_result(handle_sentinel_suggest({"query": text, "mode": args.mode}))


def _cmd_overlay(args: argparse.Namespace) -> int:
    from sentinel.tools.handlers import handle_sentinel_overlay
    return _print_result(handle_sentinel_overlay({"action": args.action}))


def _cmd_history(args: argparse.Namespace) -> int:
    from sentinel.tools.handlers import handle_sentinel_history
    return _print_result(handle_sentinel_history({"query": " ".join(args.query), "limit": args.limit}))


def _cmd_doctor() -> int:
    """Print a dependency + config sanity report."""
    rows = []

    def _check(name: str, ok: bool, hint: str = "") -> None:
        rows.append({"check": name, "ok": ok, "hint": hint if not ok else ""})

    try:
        import yaml  # noqa: F401
        _check("pyyaml", True)
    except Exception:
        _check("pyyaml", False, "pip install pyyaml")
    try:
        import pydantic  # noqa: F401
        _check("pydantic", True)
    except Exception:
        _check("pydantic", False, "pip install 'pydantic>=2'")
    try:
        import websockets  # noqa: F401
        _check("websockets", True)
    except Exception:
        _check("websockets", False, "pip install websockets")
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        _check("fastapi/uvicorn (overlay)", True)
    except Exception:
        _check("fastapi/uvicorn (overlay)", False, "pip install fastapi uvicorn")
    try:
        import sounddevice  # noqa: F401
        _check("sounddevice (python audio)", True)
    except Exception:
        _check("sounddevice (python audio)", False, "pip install sounddevice (optional)")

    from sentinel import config as cfgmod
    cfg = cfgmod.load()
    cfg_path = cfgmod.resolve_config_path()
    print(json.dumps({
        "checks": rows,
        "config_path": str(cfg_path) if cfg_path else "(defaults)",
        "backend": cfg.realtime_backend,
        "overlay": {"host": cfg.overlay.host, "port": cfg.overlay.port, "enabled": cfg.overlay.enabled},
        "audio": {"source": cfg.audio.source, "channels": cfg.audio.channels},
    }, indent=2))
    return 0 if all(r["ok"] for r in rows) else 1


def _cmd_config(args: argparse.Namespace) -> int:
    from sentinel import config as cfgmod
    sub = getattr(args, "config_cmd", None)
    if sub == "path":
        p = cfgmod.resolve_config_path()
        print(str(p) if p else "(defaults — no file found)")
        return 0
    cfg = cfgmod.load()
    print(json.dumps(cfg.model_dump(mode="json"), indent=2))
    return 0
