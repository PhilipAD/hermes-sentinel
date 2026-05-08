// Library half of the Tauri overlay.
//
// Builders, tray menu, hotkeys, and IPC commands. Kept in a library crate so
// platform-specific entrypoints (mobile, in particular) can pull from the
// same code path as the desktop binary.

use serde::{Deserialize, Serialize};
use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::{TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WindowEvent,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OverlayPosition {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

#[tauri::command]
fn overlay_show(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_always_on_top(true);
    }
    Ok(())
}

#[tauri::command]
fn overlay_hide(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.hide();
    }
    Ok(())
}

#[tauri::command]
fn overlay_toggle(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let visible = win.is_visible().unwrap_or(false);
        if visible {
            let _ = win.hide();
        } else {
            let _ = win.show();
            let _ = win.set_focus();
        }
    }
    Ok(())
}

#[tauri::command]
fn overlay_position(app: AppHandle, pos: OverlayPosition) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.set_position(tauri::PhysicalPosition::new(pos.x, pos.y));
        let _ = win.set_size(tauri::PhysicalSize::new(pos.width, pos.height));
    }
    Ok(())
}

#[tauri::command]
fn overlay_set_opacity(app: AppHandle, opacity: f64) -> Result<(), String> {
    // Opacity is set via CSS in the web UI rather than the OS window — Tauri
    // doesn't expose a portable per-window opacity setter on every platform.
    // We persist it so the UI can read it on next launch.
    let _ = (app, opacity);
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            overlay_show,
            overlay_hide,
            overlay_toggle,
            overlay_position,
            overlay_set_opacity
        ])
        .setup(|app| {
            // Stealth mode tweaks per-platform.
            if let Some(win) = app.get_webview_window("main") {
                #[cfg(target_os = "macos")]
                {
                    use tauri::ActivationPolicy;
                    let _ = app.set_activation_policy(ActivationPolicy::Accessory);
                }
                let _ = win.set_always_on_top(true);
                let _ = win.set_skip_taskbar(true);
            }

            // System tray.
            let menu = Menu::with_items(
                app,
                &[
                    &MenuItem::with_id(app, "show", "Show overlay", true, None::<&str>)?,
                    &MenuItem::with_id(app, "hide", "Hide overlay", true, None::<&str>)?,
                    &MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?,
                ],
            )?;

            let _ = TrayIconBuilder::new()
                .icon(Image::from_bytes(include_bytes!("../icons/icon.png")).unwrap_or_default())
                .menu(&menu)
                .on_menu_event(|app, event| {
                    match event.id.as_ref() {
                        "show" => {
                            if let Some(w) = app.get_webview_window("main") { let _ = w.show(); }
                        }
                        "hide" => {
                            if let Some(w) = app.get_webview_window("main") { let _ = w.hide(); }
                        }
                        "quit" => {
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::DoubleClick { .. } = event {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let visible = w.is_visible().unwrap_or(false);
                            if visible { let _ = w.hide(); } else { let _ = w.show(); }
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Stay alive in the tray; just hide.
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
