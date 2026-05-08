"""Briefing compression + context injection.

Pre-meeting curation pipeline:

  1. Caller passes in a list of doc inputs (path / inline text / URL)
  2. ``compress_briefing`` reads each, dedup-summarizes them, packs them under
     a token budget, and produces a ``Briefing``
  3. ``build_session_update`` renders the briefing into the
     ``session.update``-style payload that ``BackendRouter`` injects on
     connect, so the realtime model already knows the goal/persona/docs
     before the first audio frame.

Compression here is intentionally *cheap* — character truncation with
section weighting — not LLM-driven. If the user wants a smart summary, they
can pre-process docs with their normal Hermes tools and pass the summary in
as ``inline``. Keeping this synchronous means we can call it from
``on_session_start`` without an extra LLM round-trip on the hot path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sentinel.state import Briefing

logger = logging.getLogger(__name__)


# Rough char-per-token used for budgeting (English text). Avoids needing a
# tokenizer dependency in the plugin core.
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Doc loaders
# ---------------------------------------------------------------------------

def _load_doc(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Materialize one doc spec into ``{title, body, source}``.

    Supported spec shapes::

        {"path": "/abs/path.md", "title": "..."}
        {"inline": "raw text", "title": "..."}
        {"url": "https://...", "title": "..."}    # not fetched here

    URL fetching is out of scope for this module — the caller should fetch
    via Hermes' web tools and pass the result back as ``inline``.
    """
    title = str(spec.get("title") or "").strip() or "Untitled"
    if "inline" in spec and spec["inline"] is not None:
        return {"title": title, "body": str(spec["inline"]), "source": "inline"}
    if "path" in spec and spec["path"]:
        p = Path(str(spec["path"])).expanduser()
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("sentinel: failed to read briefing doc %s: %s", p, e)
            body = ""
        return {"title": title, "body": body, "source": str(p)}
    if "url" in spec and spec["url"]:
        # Caller is responsible for fetch; we record the URL for traceability.
        return {"title": title, "body": "", "source": str(spec["url"])}
    return {"title": title, "body": "", "source": "unknown"}


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------

def _truncate(body: str, char_budget: int) -> str:
    body = body.strip()
    if len(body) <= char_budget:
        return body
    head = body[: int(char_budget * 0.7)].rstrip()
    tail = body[-int(char_budget * 0.25):].lstrip()
    return f"{head}\n…\n{tail}"


def compress_briefing(
    *,
    meeting_id: str,
    docs: Iterable[Dict[str, Any]],
    goal: str = "",
    persona: str = "",
    max_tokens: int = 2000,
) -> Briefing:
    """Compress doc inputs + goal + persona into a token-budgeted Briefing.

    Args:
        meeting_id: stable ID for the meeting.
        docs: iterable of doc spec dicts (see :func:`_load_doc`).
        goal: free-text statement of what the user wants out of the meeting.
        persona: free-text persona to adopt (e.g. "skeptical CTO").
        max_tokens: budget for the rendered briefing summary.

    Returns:
        Briefing with summary text suitable for injection.
    """
    loaded = [_load_doc(d) for d in (docs or [])]

    # Reserve ~25% of the budget for goal+persona; rest is split across docs.
    char_budget = max_tokens * _CHARS_PER_TOKEN
    head_budget = int(char_budget * 0.25)
    doc_budget_total = char_budget - head_budget
    n_docs = max(1, sum(1 for d in loaded if d["body"]))
    per_doc = max(200, doc_budget_total // n_docs)

    parts: List[str] = []
    if goal:
        parts.append(f"## Goal\n{_truncate(goal, head_budget // 2)}")
    if persona:
        parts.append(f"## Persona\n{_truncate(persona, head_budget // 2)}")

    for d in loaded:
        body = (d.get("body") or "").strip()
        if not body:
            # Skip empty docs (e.g. URL-only specs); still keep title for
            # traceability so the model knows what was *intended*.
            parts.append(f"## {d['title']}\n(source: {d['source']}; body not loaded)")
            continue
        parts.append(f"## {d['title']}\n{_truncate(body, per_doc)}")

    summary = "\n\n".join(parts).strip()
    # Clamp again at the absolute budget in case section overhead ran over.
    if len(summary) > char_budget:
        summary = _truncate(summary, char_budget)

    token_estimate = max(1, len(summary) // _CHARS_PER_TOKEN)

    return Briefing(
        meeting_id=meeting_id,
        goal=goal,
        persona=persona,
        docs=[{"title": d["title"], "source": d["source"]} for d in loaded],
        summary=summary,
        token_estimate=token_estimate,
    )


# ---------------------------------------------------------------------------
# Injection helpers
# ---------------------------------------------------------------------------

def render_system_prompt(briefing: Briefing) -> str:
    """Render the briefing as a system-prompt string for backends that take one."""
    header = (
        "You are Hermes Sentinel, a real-time meeting copilot. "
        "Use the briefing below to provide concise, contextual suggestions. "
        "Keep responses tight (1–3 sentences) unless asked to expand."
    )
    if not briefing.summary:
        return header
    return f"{header}\n\n## Briefing (meeting: {briefing.meeting_id})\n{briefing.summary}"


def build_session_update(briefing: Optional[Briefing], *, backend: str) -> Dict[str, Any]:
    """Build a backend-shaped ``session.update`` payload.

    OpenAI-Realtime-style providers accept a ``session.update`` with
    ``instructions``; other providers vary, so the router translates this
    canonical shape to provider-specific keys at send time.
    """
    instructions = render_system_prompt(briefing) if briefing else (
        "You are Hermes Sentinel, a real-time meeting copilot."
    )
    return {
        "type": "session.update",
        "session": {
            "instructions": instructions,
            "modalities": ["text"],
            "input_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
        },
        "_sentinel_meta": {
            "backend": backend,
            "briefing_present": bool(briefing and briefing.summary),
            "token_estimate": briefing.token_estimate if briefing else 0,
        },
    }


def merge_into_pre_llm_call(
    *,
    messages: List[Dict[str, Any]],
    briefing: Optional[Briefing],
    transcript_tail: str,
) -> List[Dict[str, Any]]:
    """Inject briefing + transcript tail into a Hermes ``messages`` list.

    Used by the ``pre_llm_call`` hook to make sure the agent sees what is
    happening in the meeting on every turn without the user having to paste
    the transcript manually.
    """
    if not briefing and not transcript_tail:
        return messages

    blocks: List[str] = []
    if briefing and briefing.summary:
        blocks.append(f"<sentinel-briefing>\n{briefing.summary}\n</sentinel-briefing>")
    if transcript_tail:
        blocks.append(
            "<sentinel-transcript-tail>\n"
            f"{transcript_tail.strip()}\n"
            "</sentinel-transcript-tail>"
        )
    if not blocks:
        return messages

    injected = {"role": "system", "content": "\n\n".join(blocks)}

    # Place after any existing system messages, before the first non-system
    # message. Preserves caller's prompt structure.
    out: List[Dict[str, Any]] = []
    inserted = False
    for m in messages:
        if not inserted and m.get("role") != "system":
            out.append(injected)
            inserted = True
        out.append(m)
    if not inserted:
        out.append(injected)
    return out
