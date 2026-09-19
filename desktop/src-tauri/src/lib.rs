//! The desktop shell. It owns one thing: the Python backend process. The
//! measurement logic, the settings contract and the files all live on the
//! Python side; the shell launches it, learns where it is listening, hands
//! that to the webview, and makes sure it goes away when the window does.

mod backend;
mod close;
mod logs;
mod supervisor;

use std::path::PathBuf;
use std::sync::Arc;

use backend::{Backend, BackendInfo, SpawnOptions};
use close::{CloseGate, Verdict};
use serde::Serialize;
use supervisor::{ExitHook, Supervisor};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent};

/// Sent to the webview when the window was asked to close and did not: the
/// webview decides, with the operator if a run is active.
const CLOSE_REQUESTED: &str = "close-requested";

/// Sent to the webview when the backend ends without having been asked to.
const BACKEND_EXITED: &str = "backend-exited";

#[derive(Clone, Serialize)]
struct BackendExited {
    /// The process's exit code; null when a signal ended it.
    code: Option<i32>,
    message: String,
}

/// The URL and token the UI needs to talk to the backend, once it is up; the
/// reason there is no backend when it failed to start.
#[tauri::command]
async fn backend_info(supervisor: State<'_, Arc<Supervisor>>) -> Result<BackendInfo, String> {
    let supervisor = Arc::clone(&supervisor);
    tauri::async_runtime::spawn_blocking(move || supervisor.wait_info())
        .await
        .map_err(|e| format!("the shell stopped waiting for the backend: {e}"))?
}

/// Start a fresh backend after the last one ended, and answer like
/// `backend_info`. One that is somehow still there is shut down in order first.
#[tauri::command]
async fn restart_backend(app: AppHandle, supervisor: State<'_, Arc<Supervisor>>) -> Result<BackendInfo, String> {
    let supervisor = Arc::clone(&supervisor);
    supervisor.restart(starter(&app), exit_reporter(&app));
    tauri::async_runtime::spawn_blocking(move || supervisor.wait_info())
        .await
        .map_err(|e| format!("the shell stopped waiting for the backend: {e}"))?
}

/// The webview has the close question and will answer it.
#[tauri::command]
fn close_request_seen(gate: State<'_, CloseGate>) {
    gate.seen();
}

/// The operator chose to keep the app open.
#[tauri::command]
fn cancel_close(gate: State<'_, CloseGate>) {
    gate.keep();
}

/// Close for real. Exiting runs the ordered shutdown in `run`'s exit handler:
/// the backend stops the run, turns the output off and finalizes the file.
#[tauri::command]
fn confirm_close(app: AppHandle, gate: State<'_, CloseGate>) {
    gate.confirm();
    app.exit(0);
}

/// How the supervisor starts a backend for this app.
fn starter(app: &AppHandle) -> impl FnOnce(ExitHook) -> Result<Backend, String> + Send + 'static {
    let app = app.clone();
    move |on_exit| backend::spawn(spawn_options(&app, on_exit)?)
}

/// How the webview learns that the backend it was using is gone.
fn exit_reporter(app: &AppHandle) -> impl FnOnce(Option<i32>) + Send + 'static {
    let app = app.clone();
    move |code| {
        let message = supervisor::exit_message(code);
        eprintln!("resistamet: {message}");
        let _ = app.emit(BACKEND_EXITED, BackendExited { code, message });
    }
}

/// Where the backend keeps config and data when there is no source checkout:
/// the platform's per-user app data directory.
fn packaged_dirs(app: &AppHandle) -> Result<(PathBuf, PathBuf), String> {
    let data = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app data dir: {e}"))?;
    std::fs::create_dir_all(&data).map_err(|e| format!("cannot create {}: {e}", data.display()))?;
    Ok((data.clone(), data.join("config.json")))
}

/// What to launch, where, and with what, decided afresh for each launch.
fn spawn_options(app: &AppHandle, on_exit: ExitHook) -> Result<SpawnOptions, String> {
    let repo_root = backend::dev_repo_root();
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from));
    let resource_dir = app.path().resource_dir().ok();
    let launch = backend::locate(exe_dir.as_deref(), resource_dir.as_deref(), repo_root.as_deref());
    eprintln!("resistamet: backend via {launch:?}");

    // In a source checkout the backend works where the PySide6 app
    // does, so both see the same config and measurement_data.
    let (cwd, config) = match &repo_root {
        Some(root) => (root.clone(), root.join("config.json")),
        None => packaged_dirs(app)?,
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

    Ok(SpawnOptions { launch, cwd, config, simulate, stderr_log, on_exit })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // Off the main thread, and never as an error from this hook:
            // the window opens at once and shows how the start went.
            let handle = app.handle();
            app.manage(Supervisor::launch(starter(handle), exit_reporter(handle)));
            app.manage(CloseGate::default());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window ends the app, which stops a run. Ask first.
            if let WindowEvent::CloseRequested { api, .. } = event {
                let ready = window.try_state::<Arc<Supervisor>>().is_some_and(|s| s.is_ready());
                let gate = window.state::<CloseGate>();
                if gate.requested(ready, std::time::Instant::now()) == Verdict::Ask {
                    api.prevent_close();
                    let _ = window.emit(CLOSE_REQUESTED, ());
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            backend_info,
            restart_backend,
            close_request_seen,
            cancel_close,
            confirm_close
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            // The run's grace period happens here, before the process exits,
            // so a closing window never leaves the instrument output on.
            if let RunEvent::Exit = event {
                if let Some(supervisor) = app.try_state::<Arc<Supervisor>>() {
                    supervisor.shutdown();
                }
            }
        });
}
