//! The desktop shell. It owns one thing: the Python backend process. The
//! measurement logic, the settings contract and the files all live on the
//! Python side; the shell launches it, learns where it is listening, hands
//! that to the webview, and makes sure it goes away when the window does.

mod backend;
mod close;
mod data_folder;
mod logs;
#[cfg(target_os = "macos")]
mod menu;
mod supervisor;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use backend::{Backend, BackendInfo, SpawnOptions};
use close::{CloseGate, Verdict};
use serde::Serialize;
use supervisor::{ExitHook, Supervisor};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent};
use tauri_plugin_dialog::DialogExt;

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

/// What the webview is told about the data folder. Paths are as the
/// platform writes them.
#[derive(Clone, Serialize)]
struct DataFolderState {
    /// False in a source checkout: the backend runs at the repository root
    /// and there is nothing to choose.
    managed: bool,
    /// The directory the backend runs in; a profile's relative
    /// `data_directory` resolves against it.
    folder: String,
    /// Whether `folder` is the operator's stored choice.
    chosen: bool,
    default_folder: String,
    /// The default is the app data directory, Documents being unusable.
    default_is_fallback: bool,
    /// A stored choice that could not be used at launch.
    unavailable: Option<String>,
    /// `<app data>/measurement_data`, when earlier versions left files there
    /// and it is not where files go now.
    legacy: Option<String>,
}

fn shown(path: &Path) -> String {
    path.display().to_string()
}

/// The folder a backend launched now would run in.
fn resolve_data_folder(app: &AppHandle, app_data: &Path) -> data_folder::Decision {
    let stored = data_folder::read_choice(app_data);
    let documents = app.path().document_dir().ok();
    data_folder::decide(stored.as_deref(), documents.as_deref(), app_data, |folder| {
        data_folder::ensure_writable(folder).is_ok()
    })
}

fn data_folder_snapshot(app: &AppHandle) -> Result<DataFolderState, String> {
    if let Some(root) = backend::dev_repo_root() {
        return Ok(DataFolderState {
            managed: false,
            folder: shown(&root),
            chosen: false,
            default_folder: shown(&root),
            default_is_fallback: false,
            unavailable: None,
            legacy: None,
        });
    }
    let (app_data, _) = packaged_dirs(app)?;
    // What the running backend was started with; before the first launch has
    // got that far, what it is about to be started with.
    let decision = app
        .state::<data_folder::InUse>()
        .get()
        .unwrap_or_else(|| resolve_data_folder(app, &app_data));
    let legacy = if decision.folder == app_data { None } else { data_folder::legacy_location(&app_data) };
    Ok(DataFolderState {
        managed: true,
        folder: shown(&decision.folder),
        chosen: decision.chosen,
        default_folder: shown(&decision.default_folder),
        default_is_fallback: decision.default_is_fallback,
        unavailable: decision.unavailable.as_deref().map(shown),
        legacy: legacy.as_deref().map(shown),
    })
}

/// Where data goes, and whether the operator chose it.
#[tauri::command]
async fn data_folder_state(app: AppHandle) -> Result<DataFolderState, String> {
    tauri::async_runtime::spawn_blocking(move || data_folder_snapshot(&app))
        .await
        .map_err(|e| format!("the shell stopped looking for the data folder: {e}"))?
}

/// Open the native folder picker, starting in `start` when that is a
/// directory. The picked path, or null when the picker was cancelled.
#[tauri::command]
async fn pick_data_folder(window: tauri::Window, start: Option<String>) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut picker = window.dialog().file().set_title("Data folder").set_parent(&window);
        if let Some(start) = start.filter(|s| Path::new(s).is_dir()) {
            picker = picker.set_directory(start);
        }
        match picker.blocking_pick_folder() {
            Some(picked) => picked
                .into_path()
                .map(|path| Some(shown(&path)))
                .map_err(|e| format!("the picked folder is not a path: {e}")),
            None => Ok(None),
        }
    })
    .await
    .map_err(|e| format!("the shell stopped waiting for the folder picker: {e}"))?
}

/// Make `path` the data folder: create it, prove it can be written to, store
/// the choice, and restart the backend in it when it is not already there.
///
/// The restart stops a run in progress (in order, as closing the window
/// does). The shell cannot see the session, so the webview is the one that
/// refuses this while the session is not idle.
#[tauri::command]
async fn set_data_folder(
    app: AppHandle,
    supervisor: State<'_, Arc<Supervisor>>,
    path: String,
) -> Result<DataFolderState, String> {
    let supervisor = Arc::clone(&supervisor);
    tauri::async_runtime::spawn_blocking(move || {
        if backend::dev_repo_root().is_some() {
            return Err("In a source checkout the data folder is the repository root.".to_string());
        }
        let folder = PathBuf::from(path.trim());
        if !folder.is_absolute() {
            return Err(format!("Not an absolute path: {}", folder.display()));
        }
        data_folder::ensure_writable(&folder)?;
        let (app_data, _) = packaged_dirs(&app)?;
        data_folder::write_choice(&app_data, &folder)?;

        // A start under way would ignore the restart and stay in the old
        // folder: let it finish, however it ends.
        let _ = supervisor.wait_info();
        let in_use = app.state::<data_folder::InUse>();
        if in_use.get().is_some_and(|decision| decision.folder == folder) {
            // Already there (the default, now chosen): nothing to restart.
            in_use.set(resolve_data_folder(&app, &app_data));
        } else {
            supervisor.restart(starter(&app), exit_reporter(&app));
            supervisor
                .wait_info()
                .map_err(|e| format!("The folder was saved, but the backend did not restart: {e}"))?;
        }
        data_folder_snapshot(&app)
    })
    .await
    .map_err(|e| format!("the shell stopped changing the data folder: {e}"))?
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

/// The platform's per-user app data directory and the config file in it:
/// where the backend's config lives when there is no source checkout.
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
    // Development conveniences; a release build has none of them.
    let overrides = backend::dev_overrides(cfg!(debug_assertions), |name| std::env::var(name).ok());
    let repo_root = backend::dev_repo_root();
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from));
    let resource_dir = app.path().resource_dir().ok();
    let launch = backend::locate(
        overrides.python.as_deref(),
        exe_dir.as_deref(),
        resource_dir.as_deref(),
        repo_root.as_deref(),
    );
    eprintln!("resistamet: backend via {launch:?}");

    // In a source checkout the backend works where the PySide6 app
    // does, so both see the same config and measurement_data. Installed, the
    // config stays in the app data directory and the backend works in the
    // data folder, which is where a relative data_directory then lands.
    let (cwd, config) = match &repo_root {
        Some(root) => (root.clone(), root.join("config.json")),
        None => {
            let (app_data, config) = packaged_dirs(app)?;
            let decision = resolve_data_folder(app, &app_data);
            eprintln!("resistamet: data folder {}", decision.folder.display());
            let cwd = decision.folder.clone();
            app.state::<data_folder::InUse>().set(decision);
            (cwd, config)
        }
    };
    let simulate = overrides.simulate;

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
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // Before the backend starts: its launch records the folder here.
            app.manage(data_folder::InUse::default());
            // Off the main thread, and never as an error from this hook:
            // the window opens at once and shows how the start went.
            let handle = app.handle();
            app.manage(Supervisor::launch(starter(handle), exit_reporter(handle)));
            app.manage(CloseGate::default());
            // Never an error from this hook: a menu that cannot be built
            // leaves the default one, it does not take the app down.
            #[cfg(target_os = "macos")]
            match menu::build(handle) {
                Ok(menu) => {
                    let _ = app.set_menu(menu);
                    app.on_menu_event(|app, event| {
                        if event.id() == menu::QUIT_ID {
                            menu::quit_chosen(app);
                        }
                    });
                }
                Err(e) => eprintln!("resistamet: keeping the default menu: {e}"),
            }
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
            confirm_close,
            data_folder_state,
            pick_data_folder,
            set_data_folder
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
