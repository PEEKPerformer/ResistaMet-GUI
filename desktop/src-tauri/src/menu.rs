//! The macOS application menu, with a Quit that asks first.
//!
//! The stock Quit item sends `terminate:` to the application, which ends the
//! process without any event the shell could hold back: Cmd+Q would stop a
//! run with no question, past the close-request handling. Here the stock item
//! is replaced by an ordinary one with the same shortcut, which asks the
//! window to close and so takes the same path as the close button.
//!
//! Windows and Linux get no menu from Tauri by default, so there is nothing
//! to replace there.

use tauri::menu::{Menu, MenuItem, MenuItemKind};
use tauri::{AppHandle, Manager};

pub const QUIT_ID: &str = "resistamet-quit";

/// Tauri's default menu with its Quit item swapped. The default's first
/// submenu is the application menu and Quit is its last item; if that ever
/// stops being true the default menu is returned as it came.
pub fn build(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::default(app)?;
    let Some(MenuItemKind::Submenu(app_menu)) = menu.items()?.into_iter().next() else {
        return Ok(menu);
    };
    let items = app_menu.items()?;
    let Some(stock_quit) = items.last().filter(|item| item.as_predefined_menuitem().is_some()) else {
        return Ok(menu);
    };
    app_menu.remove(stock_quit)?;
    let name = &app.package_info().name;
    app_menu.append(&MenuItem::with_id(app, QUIT_ID, format!("Quit {name}"), true, Some("CmdOrCtrl+Q"))?)?;
    Ok(menu)
}

/// Quit was chosen: ask the window to close, which asks the operator when a
/// run is active. With no window left there is nothing to ask through.
pub fn quit_chosen(app: &AppHandle) {
    match app.get_webview_window("main") {
        Some(window) => {
            let _ = window.close();
        }
        None => app.exit(0),
    }
}
