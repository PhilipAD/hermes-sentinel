---
name: sentinel
description: |
  Always-on meeting intelligence. Use Sentinel when the user is about to
  enter, is currently in, or just finished a meeting and wants context-aware
  copilot help. Wraps live audio capture, pre-meeting briefing curation,
  contextual suggestions, and post-meeting extraction in one plugin.
priority: medium
---

# Sentinel

Hermes Sentinel turns any meeting into a context-aware intelligence layer.

## When to use

Use Sentinel when:

- The user mentions an upcoming meeting and wants prep ("brief me on this
  meeting", "what should I say to X?").
- The user is in a meeting right now and wants live help (talking points,
  replies, objection handling, recaps).
- The user just finished a meeting and wants action items, a summary, or a
  Hermes skill scaffolded from what they learned.

Don't reach for Sentinel for general transcription jobs that don't need
context — `meet_join` (google_meet plugin) is simpler for that.

## Tool playbook

The plugin exposes 8 tools. Typical flow:

```
sentinel_curate ──► sentinel_start ──► sentinel_suggest (n times) ──► sentinel_stop ──► sentinel_post
```

1. **`sentinel_curate`** — call BEFORE the meeting. Pass briefing docs (paths
   or inline text), the goal, and optionally a persona. Sentinel compresses
   everything to a token budget and stages it for injection.
2. **`sentinel_start`** — opens dual-channel capture and connects the
   realtime backend. The staged briefing is sent on-connect.
3. **`sentinel_status`** — poll for backend/audio liveness if the user asks
   "is sentinel running?".
4. **`sentinel_suggest`** — call mid-meeting whenever the user asks for help.
   Modes: `talking_points`, `reply`, `recap`, `objection`, `freeform`. Result
   is also pushed to the overlay.
5. **`sentinel_overlay`** — `show` / `hide` / `toggle` / `position` the
   stealth overlay window.
6. **`sentinel_stop`** — stop capture, persist transcript. By default also
   triggers post-extract.
7. **`sentinel_post`** — manual post-extract (summary, actions, optional
   skill creation, optional yaowpedia save).
8. **`sentinel_history`** — substring search across saved transcripts.

## Backends

When the user asks "which backend should I use?":

- **OpenAI Realtime** — best for general-purpose, text-only suggestion mode.
- **xAI Grok** — when they want Grok's persona; voice agent.
- **AssemblyAI** — transcript-only; pair with Hermes' main LLM for
  suggestions.
- **Deepgram (Nova-3)** — fastest, lowest-latency transcription.
- **Gemini Live** — when the user wants Google's live multimodal model.
- **local** — offline (whisper + Ollama). Use when API keys are unavailable.

## Hook behavior

Sentinel registers `on_session_start`, `on_session_end`, and
`pre_llm_call`. The pre-llm-call hook automatically injects the briefing
and the last 20 transcript chunks into every LLM call once a session is
active — so even ordinary agent turns have meeting context "for free".

## Privacy reminder

Sentinel records audio. The agent should always remind the user to
announce recording to other participants. Capture only starts on an
explicit `sentinel_start` — there's no auto-capture.

## Failure modes

- Backend WebSocket connect failed → check API key env var, run
  `hermes sentinel doctor`.
- "audio.source=rust requested but binary not found" → run
  `scripts/build-rust.sh`, or set `audio.source: python` in sentinel.yaml.
- Overlay not appearing → confirm `overlay.enabled: true`, check
  `hermes sentinel overlay show`, look for the FastAPI server in the logs.
- Empty transcript after stop → the backend never produced
  `TranscriptEvent`s; check `sentinel_status` while live.

## Reference

- Plugin source: `~/.hermes/plugins/sentinel/`
- Config template: `config/sentinel.example.yaml`
- Build scripts: `scripts/build-rust.sh`, `scripts/build-tauri.sh`
- Standalone overlay: `remote-overlay/index.html`
- Full dashboard webui: `remote-overlay/webui/`
