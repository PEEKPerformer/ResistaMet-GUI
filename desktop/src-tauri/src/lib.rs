//! The desktop shell. It owns one thing: the Python backend process. The
//! measurement logic, the settings contract and the files all live on the
//! Python side; the shell launches it, learns where it is listening, hands
//! that to the webview, and makes sure it goes away when the window does.

mod backend;
mod logs;

use std::path::PathBuf;

use backend::{Backend, BackendInfo, SpawnOptions};
use tauri::{Manager, RunEvent, State};

/// The URL and token the UI needs to talk to the backend.
#[tauri::command]
fn backend_info(backend: State<'_, Backend>) -> BackendInfo {
    backend.info.clone()
}

/// Where the backend keeps config and data when there is no source checkout:
/// the platform's per-user app data directory.
fn packaged_dirs(app: &tauri::AppHandle) -> Result<(PathBuf, PathBuf), String> {
    let data = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app data dir: {e}"))?;
    std::fs::create_dir_all(&data).map_err(|e| format!("cannot create {}: {e}", data.display()))?;
    Ok((data.clone(), data.join("config.json")))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let repo_root = backend::dev_repo_root();
            let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from));
            let resource_dir = app.path().resource_dir().ok();
            let launch = backend::locate(exe_dir.as_deref(), resource_dir.as_deref(), repo_root.as_deref());
            eprintln!("resistamet: backend via {launch:?}");

            // In a source checkout the backend works where the PySide6 app
            // does, so both see the same config and measurement_data.
            let (cwd, config) = match &repo_root {
                Some(root) => (root.clone(), root.join("config.json")),
                None => packaged_dirs(app.handle())?,
            };
            let simulate = std::env::var("RESISTAMET_SIMULATE").map(|v| v == "1").unwrap_or(false);

            // Development keeps the backend's log in the terminal. A packaged
            // app has no terminal, so each launch writes its own file.
            let stderr_log = if cfg!(debug_assertions) {
                None
            } else {
                let dir = app.path().app_log_dir().map_err(|e| format!("no app log dir: {e}"))?;
                let (path, file) = logs::open_backend_log(&dir)?;
                eprintln!("resistamet: backend log at {}", path.display());
                Some(file)
            };

            let backend = backend::spawn(SpawnOptions { launch, cwd, config, simulate, stderr_log })
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
            app.manage(backend);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            // The run's grace period happens here, before the process exits,
            // so a closing window never leaves the instrument output on.
            if let RunEvent::Exit = event {
                if let Some(backend) = app.try_state::<Backend>() {
                    backend.shutdown();
                }
            }
        });
}
