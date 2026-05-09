// Library half of the Tauri overlay.
use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WindowEvent,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OverlayPosition { pub x: i32, pub y: i32, pub width: u32, pub height: u32 }

#[tauri::command]
fn overlay_show(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show(); let _ = win.set_always_on_top(true);
    }
    Ok(())
}

#[tauri::command]
fn overlay_hide(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") { let _ = win.hide(); }
    Ok(())
}

#[tauri::command]
fn overlay_toggle(app: AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let visible = win.is_visible().unwrap_or(false);
        if visible { let _ = win.hide(); } else { let _ = win.show(); let _ = win.set_focus(); }
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
fn overlay_set_opacity(_app: AppHandle, _opacity: f64) -> Result<(), String> { Ok(()) }

/// Create a 32x32 RGBA icon for tray (correct: 32*32*4 = 4096 bytes)
fn make_icon() -> tauri::image::Image<'static> {
    let size: u32 = 32;
    let mut rgba = Vec::with_capacity((size * size * 4) as usize);
    for y in 0..size {
        for x in 0..size {
            rgba.push(30u8);  // R
            rgba.push((80u8 as u32 + y * 4) as u8);  // G
            rgba.push(200u8);  // B
            rgba.push(255u8);  // A
        }
    }
    tauri::image::Image::new_owned(rgba, size, size)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            overlay_show, overlay_hide, overlay_toggle, overlay_position, overlay_set_opacity
        ])
        .setup(|app| {
            #[cfg(target_os = "macos")]
            { app.set_activation_policy(tauri::ActivationPolicy::Accessory); }

            if let Some(win) = app.get_webview_window("main") {
                let _ = win.set_always_on_top(true);
                let _ = win.set_skip_taskbar(true);
            }

            let menu = Menu::with_items(app, &[
                &MenuItem::with_id(app, "show", "Show overlay", true, None::<&str>)?,
                &MenuItem::with_id(app, "hide", "Hide overlay", true, None::<&str>)?,
                &MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?,
            ])?;

            let icon = make_icon();

            let icon_bytes = include_bytes!("../icons/icon.png");
            let icon = Image::from_bytes(icon_bytes).expect("icon must be valid PNG");
            let _ = TrayIconBuilder::new()
                .icon(icon)
                .menu(&menu)
                .on_menu_event(|app, event| {
                    match event.id.as_ref() {
                        "show" => if let Some(w) = app.get_webview_window("main") { let _ = w.show(); },
                        "hide" => if let Some(w) = app.get_webview_window("main") { let _ = w.hide(); },
                        "quit" => app.exit(0),
                        _ => {}
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::DoubleClick { .. } = event {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            if w.is_visible().unwrap_or(false) { let _ = w.hide(); } else { let _ = w.show(); }
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
