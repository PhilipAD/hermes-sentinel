// Sentinel overlay — Tauri shell entry point.
//
// Responsibilities:
//   * boot the always-on-top, transparent overlay window
//   * register a global hotkey (Cmd/Ctrl+K) to toggle visibility
//   * register a system-tray icon with show/hide/quit
//   * on macOS, hide the dock icon; on Windows/Linux, skip the taskbar
//
// The actual UI is in tauri-overlay/src/ (Svelte). This binary is a thin
// wrapper.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    sentinel_overlay_lib::run();
}
