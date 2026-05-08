#!/usr/bin/env bash
# Build the sentinel-audio Rust binary for the current platform.
#
# After build, the binary is at rust-audio/target/release/sentinel-audio
# (or sentinel-audio.exe on Windows). The Python audio_capture module
# auto-detects this path and uses it in preference to the sounddevice
# fallback when audio.source == "auto".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if ! command -v cargo >/dev/null; then
  echo "error: cargo not found — install Rust from https://rustup.rs" >&2
  exit 1
fi

cd "${PLUGIN_DIR}/rust-audio"

if [[ "${1:-}" == "--debug" ]]; then
  cargo build
  BIN_DIR="target/debug"
else
  cargo build --release
  BIN_DIR="target/release"
fi

echo
echo "Built: ${PLUGIN_DIR}/rust-audio/${BIN_DIR}/sentinel-audio"
echo "Try:   ${PLUGIN_DIR}/rust-audio/${BIN_DIR}/sentinel-audio --help"
