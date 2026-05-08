#!/usr/bin/env bash
# Build the Tauri stealth overlay for the current platform.
#
# Prereqs:
#   * Node 18+ with pnpm or npm
#   * Rust toolchain (cargo)
#   * Tauri prerequisites (https://tauri.app/start/prerequisites/)
#
# Output: tauri-overlay/src-tauri/target/release/bundle/<platform>/...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TAURI_DIR="${PLUGIN_DIR}/tauri-overlay"

if [[ ! -d "${TAURI_DIR}" ]]; then
  echo "error: ${TAURI_DIR} missing" >&2
  exit 1
fi

cd "${TAURI_DIR}"

if ! command -v cargo >/dev/null; then
  echo "error: cargo not found — install Rust from https://rustup.rs" >&2
  exit 1
fi

if [[ -f package.json ]]; then
  if command -v pnpm >/dev/null; then PKG=pnpm; elif command -v npm >/dev/null; then PKG=npm; else
    echo "error: need pnpm or npm" >&2; exit 1
  fi
  ${PKG} install --silent || ${PKG} install
  if [[ "${1:-}" == "--dev" ]]; then
    exec ${PKG} run tauri dev
  fi
  exec ${PKG} run tauri build
fi

# No package.json yet — fall back to building the Rust crate alone so users
# can verify the toolchain before scaffolding the Svelte frontend.
cd src-tauri
cargo build --release
echo "Built: ${TAURI_DIR}/src-tauri/target/release/sentinel-overlay"
