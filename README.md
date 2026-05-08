# Hermes Sentinel

Always-on meeting intelligence plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.12.0+.

Sentinel turns any meeting into a context-aware, self-improving intelligence layer:

- **Dual-channel stealth audio capture** (system audio + microphone) with VAD,
  via a native Rust binary or pure-Python fallback.
- **Multiple realtime backends** — OpenAI Realtime, xAI Grok, AssemblyAI,
  Deepgram, Gemini Live, or a fully local whisper + Ollama path.
- **Pre-meeting context curation** — load briefing docs, set goal/persona,
  inject everything into the realtime session before the first audio frame.
- **Real-time contextual suggestions** delivered to a stealth Tauri overlay
  (always-on-top, translucent, hidden from dock and screen-share).
- **Post-meeting auto-extract** — transcript, summary, action items, and
  optionally a new Hermes skill, saved into yaowpedia.

## Architecture (one-liner)

```
mic+system audio ──► sentinel-audio (Rust)  ──► BackendRouter (WS)  ──► realtime model
                                                       │
                            briefing (curate) ─────────┤
                                                       ▼
                                             SentinelSessionState
                                                       │
                              ┌────────────────────────┼─────────────────────────┐
                              ▼                        ▼                         ▼
                       FastAPI overlay         pre_llm_call hook          on_session_end
                    (REST + WebSocket)       (transcript injected)      (post-extract → yaowpedia)
                              │
                              ▼
              ┌──────────────────────────────────┐
              │ Tauri stealth overlay  /  any browser │
              └──────────────────────────────────┘
```

## Install

### Quick (via Hermes plugin manager)

```bash
hermes plugins install ~/.hermes/plugins/sentinel       # or a Git URL
hermes plugins enable sentinel
hermes plugins list                                       # confirm sentinel is enabled
hermes sentinel doctor                                    # dependency check
```

### Manual

```bash
# 1. Drop or symlink the plugin into ~/.hermes/plugins/sentinel
git clone https://github.com/philipadsouza/hermes-sentinel ~/.hermes/plugins/sentinel

# 2. Enable in ~/.hermes/config.yaml
#    plugins:
#      enabled:
#        - sentinel
```

### Optional native components

```bash
# Native audio engine (preferred — pure-Python sounddevice fallback if absent)
./scripts/build-rust.sh

# Tauri stealth overlay (browser overlay works without this)
./scripts/build-tauri.sh
```

## Configure

Copy `config/sentinel.example.yaml` to `~/.hermes/plugins/sentinel/sentinel.yaml`
and edit. Backend API keys come from environment variables — never paste keys
into the YAML.

```yaml
realtime_backend: "openai"        # default backend
overlay:
  host: "127.0.0.1"               # set "0.0.0.0" for remote access
  port: 18765
  api_key: ""                     # optional auth
audio:
  channels: 2                     # 1 = mic only, 2 = system + mic
post_meeting:
  auto_extract_actions: true
```

Set keys in `~/.env`:

```dotenv
OPENAI_API_KEY=...
XAI_API_KEY=...
ASSEMBLYAI_API_KEY=...
DEEPGRAM_API_KEY=...
GEMINI_API_KEY=...
```

## Use

In a Hermes session, the agent gets eight new tools:

| Tool                | Purpose                                                            |
|---------------------|--------------------------------------------------------------------|
| `sentinel_curate`   | Pre-meeting: load docs, set goal/persona, compress into briefing.  |
| `sentinel_start`    | Start audio capture + connect realtime backend.                    |
| `sentinel_stop`     | Stop capture, finalize transcript, optionally auto-extract.        |
| `sentinel_status`   | Live session state — backend, audio, transcript line count.       |
| `sentinel_suggest`  | Request a contextual suggestion (talking points / reply / recap…). |
| `sentinel_post`     | Run post-meeting extraction (summary + action items + skill).      |
| `sentinel_history`  | Search past transcripts.                                           |
| `sentinel_overlay`  | Show / hide / toggle / position the stealth overlay window.        |

From the terminal:

```bash
hermes sentinel doctor
hermes sentinel start openai --meeting-id q3-roadmap
hermes sentinel suggest "what's the strongest counter to the latency objection?" --mode objection
hermes sentinel stop
hermes sentinel history "pricing" --limit 5
```

## Overlay — three flavours, same brain

The overlay is REMOTE-FIRST. The Sentinel backend runs an embedded FastAPI
server (default `127.0.0.1:18765`) with a WebSocket at `/ws` and REST at
`/api/sentinel/*`. The overlay UI can be:

1. **Tauri desktop app** — `tauri-overlay/`. Native, always-on-top,
   transparent, screenshot-resistant. Connects to any sentinel backend via
   `?sentinel=http://host:port&api_key=…`.
2. **Standalone browser overlay** — `remote-overlay/index.html`. Opens in
   any browser; works equally well as a popout from the Hermes dashboard or
   served from a static host.
3. **Full webui dashboard** — `remote-overlay/webui/`. Three-card layout
   (curation, live monitor, history, suggestion feed) for desktop use.

To run a remote overlay host (overlay UI on machine B, agent on A):

```bash
HOST=0.0.0.0 PORT=18765 API_KEY=$(openssl rand -hex 16) \
  ./remote-overlay/start-remote.sh
# Then on any device:
#   open http://<machine-B>:18765?api_key=<the key>
```

## Backends

| Name        | Audio in | Text out | Notes                                                |
|-------------|----------|----------|------------------------------------------------------|
| `openai`    | yes      | yes      | gpt-4o-realtime-preview, text-only modality.         |
| `grok`      | yes      | yes      | xAI Grok Voice. URL/format may evolve — see logs.    |
| `assemblyai`| yes      | partial+final transcripts | Pure transcription; no LLM loop.        |
| `deepgram`  | yes (raw)| Nova-3 transcripts        | Raw WS frames, no JSON envelope.        |
| `gemini`    | yes      | yes      | Gemini 2.0 Live API.                                 |
| `local`     | yes      | yes (heuristic) | Whisper for transcripts; Ollama for LLM (optional). |

## Hooks

Sentinel registers three Hermes lifecycle hooks:

- `on_session_start` — boots the overlay server and seeds session state.
- `on_session_end` — stops audio, finalizes transcript, runs post-extract.
- `pre_llm_call` — injects briefing + last-N transcript chunks into every
  LLM request so the agent always knows what's happening in the meeting.

## Privacy & consent

Sentinel records audio. Always announce that recording is happening to the
other participants. The plugin does not auto-start capture — only an
explicit `sentinel_start` (tool call or `hermes sentinel start`) begins
recording. Transcripts persist locally under
`~/.hermes/plugins/sentinel/transcripts/`. Nothing is uploaded except to
the realtime backend you configure.

## Develop

Layout:

```
sentinel/                # Python package — config, state, tools, hooks
  ├── audio_capture.py     # Rust subprocess + sounddevice fallback
  ├── backend_router.py    # WebSocket router + 6 provider adapters
  ├── context_sync.py      # Briefing compression, message injection
  ├── overlay_api.py       # FastAPI overlay server
  ├── runtime.py           # Session worker, post-extract heuristics
  ├── cli.py               # `hermes sentinel ...` subcommand
  ├── hooks/               # on_session_start/end + pre_llm_call
  └── tools/               # JSON schemas + handlers for the 8 tools
rust-audio/              # Native dual-channel capture (cpal)
tauri-overlay/           # Stealth desktop overlay
remote-overlay/          # Browser-only overlays (standalone + full webui)
dashboard/               # Hermes dashboard tab (HTMX + vanilla JS)
config/                  # Annotated config template
scripts/                 # build-rust.sh, build-tauri.sh, install.sh
```

Run the dependency check:

```bash
hermes sentinel doctor
```

## OS-Specific Audio Setup

### macOS
System-audio loopback requires a virtual audio driver:
- **[BlackHole](https://github.com/ExistentialAudio/BlackHole)** (free) — `brew install blackhole-2ch`, route system audio through it in Audio MIDI Setup.
- **Alternative:** [Loopback by Rogue Amoeba](https://rogueamoeba.com/loopback/) (paid, easier).
- **Entitlements:** macOS 14+ grants mic permission via popup. The Tauri wrapper includes `com.apple.security.device.audio-input`.

### Windows
System-audio loopback uses **WASAPI loopback** (built into Windows 10/11, supported by `cpal`).
- No additional driver needed.
- If hardware doesn't support loopback, install [VB-Cable](https://vb-audio.com/Cable/) as a virtual device.

### Linux
- **PipeWire** (default on modern distros): Loopback built-in via `cpal`'s `Monitor` stream. No setup needed.
- **PulseAudio** (older distros): `pactl load-module module-null-sink sink_name=loopback` then route apps to it.
- **ALSA-only:** Loopback unsupported — falls back to mic-only.

## Privacy & Consent

User-controlled recording. The overlay includes an optional **consent banner** (`overlay.consent_banner: true` in config):
- Shows "This session records system audio and microphone. Ensure all participants have consented."
- Capture starts only after the user clicks "I Consent."
- Customize text via `overlay.consent_text`.

## Feature Parity

| Feature | Hermes Sentinel | Cluely/Convo | Natively | Otter.ai |
|---------|:---------------:|:-----------:|:--------:|:--------:|
| Stealth dual-channel audio | ✅ | ❌ bot-join | ✅ Rust | ❌ |
| Remote overlay (separate machine) | ✅ FastAPI/WS | ❌ | ❌ | ❌ |
| Pre-meeting curation | ✅ docs/goal/persona | ✅ | ❌ | ❌ |
| 6 realtime backends | ✅ | ✅ OpenAI | ❌ | ❌ |
| Per-app audio isolation | ✅ checkbox UI | ❌ | ❌ | ❌ |
| Post-meeting skill creation | ✅ Hermes native | ❌ | ❌ | ❌ |
| Privacy-first local mode | ✅ Whisper+Ollama | ❌ | ✅ local | ❌ |
| Open source | ✅ AGPL-3.0 | ❌ | ✅ MIT | ❌ |

## Roadmap

- **v1.0** — Core + 3 backends + remote overlay + post-meeting extract (current)
- **v1.1** — Screenshot OCR + vision injection via Gemini Live, SQLite-vec local RAG fallback
- **v2.0** — Multi-user team mode, shared memory provider, plugin registry publication

## License

AGPL-3.0-only. Hermes Agent itself is separately licensed; see the Hermes
Agent repo for its terms.
