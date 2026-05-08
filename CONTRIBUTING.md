# Contributing to Hermes Sentinel

Thanks for your interest in improving Sentinel. This document describes the
ground rules so a PR doesn't get stuck on style or unmet prereqs.

## Project shape

```
sentinel/             Python plugin code (config, state, tools, hooks, runtime)
rust-audio/           Native dual-channel capture binary (cpal)
tauri-overlay/        Desktop stealth overlay
remote-overlay/       Browser overlays (standalone + full webui)
dashboard/            Hermes dashboard tab
config/               Annotated config template
scripts/              Build + install helpers
```

## Local setup

```bash
git clone https://github.com/<you>/hermes-sentinel ~/.hermes/plugins/sentinel
cd ~/.hermes/plugins/sentinel

# Python — Hermes uses 3.11+
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' || pip install pyyaml pydantic websockets fastapi uvicorn sounddevice

# Optional native pieces
./scripts/build-rust.sh
./scripts/build-tauri.sh

# Symlink + enable
./scripts/install.sh
hermes plugins enable sentinel
hermes sentinel doctor
```

## Coding standards

- **Python 3.11+**, type hints on every public function.
- **Pydantic v2** for config models, **dataclasses** for state objects.
- **asyncio** for all I/O — never block the event loop with sync requests.
- **No print** — use `logging.getLogger(__name__)`.
- **Never log API keys or transcript text at INFO level**. DEBUG is fine.
- **Imports** must be absolute (`from sentinel.foo import bar`) — no
  wildcard imports.
- **Docstrings** in Google style (`Args:`, `Returns:`, `Raises:`).
- **Ruff** + **black** are the formatters of record. Run before submitting.

## Testing

- `pytest tests/` for the Python plugin code.
- `cargo test --manifest-path rust-audio/Cargo.toml` for the audio engine.
- `cargo test --manifest-path tauri-overlay/src-tauri/Cargo.toml` for the
  Tauri shell.
- For overlay UI work, smoke-test by running `start-remote.sh` locally and
  opening `http://127.0.0.1:18765` in two browsers (with and without the
  api_key set) to verify auth.

PRs must keep the existing 8-tool surface working. If you add a new tool,
update both `sentinel/tools/schemas.py` and `sentinel/tools/handlers.py`,
register it in `__init__.py`, and document it in `SKILL.md`.

## Backend adapters

Adding a new realtime backend? Each adapter:

1. Lives in `sentinel/backend_router.py` as a subclass of `_Adapter`.
2. Reads its API key from `os.environ[backend_cfg.api_key_env]` — never
   from the YAML.
3. Translates provider events into the canonical `TranscriptEvent` /
   `AssistantTextEvent` / `ErrorEvent` types.
4. Logs the full first request/response cycle at DEBUG.
5. Comes with a default config block in `config/sentinel.example.yaml`.

## Pull request checklist

- [ ] Code passes `ruff check .` and `black --check .`.
- [ ] Tests added or updated.
- [ ] CHANGELOG note (one line under the "Unreleased" section).
- [ ] If you added/changed a tool: SKILL.md updated.
- [ ] If you added a new backend: README backends table updated.
- [ ] No secrets in fixtures / examples / commit messages.

## Reporting bugs

Open an issue with:

- Hermes version (`hermes --version`)
- Sentinel version (`sentinel/_version.py`)
- Output of `hermes sentinel doctor`
- The first error log line plus a few surrounding lines (redact API keys
  and transcript text!)

## License

By submitting a PR you agree your contribution is licensed under AGPL-3.0
(see [LICENSE](./LICENSE)).
