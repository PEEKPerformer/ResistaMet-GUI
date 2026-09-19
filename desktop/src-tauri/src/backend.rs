//! The Python backend as a child process.
//!
//! `python -m resistamet_gui.api` prints one JSON line on stdout — url, token,
//! pid — and then serves until its stdin closes. This module starts it, reads
//! that line, keeps stdin open for the life of the window, and on exit closes
//! stdin and waits for the process to finish its own shutdown (stop the run,
//! output off, finalize the file) before killing it as a last resort.
//!
//! Where the interpreter comes from, in order:
//!   1. `RESISTAMET_PYTHON` — an explicit override, for development.
//!   2. A bundled sidecar next to the executable (`resistamet-api[.exe]`),
//!      which step 3 of the migration adds.
//!   3. The repo's `.venv`, when running from a source checkout.
//!   4. `python3` / `python` on PATH.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// How long the backend gets to stop a run and finalize its file on exit.
/// Matches the sidecar's own SHUTDOWN_GRACE_S with a little margin.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(40);
/// How long to wait for the handshake line before giving up on the process.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackendInfo {
    pub url: String,
    pub token: String,
    pub pid: u32,
}

/// The running backend. Held in Tauri state for the life of the app.
pub struct Backend {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    pub info: BackendInfo,
}

#[derive(Debug)]
pub enum Launch {
    /// A bundled sidecar or an explicit interpreter: run it directly.
    Executable(PathBuf),
    /// A Python interpreter: run the module.
    Interpreter(PathBuf),
}

/// Decide what to execute. See the module docs for the order.
///
/// `resource_dir` is where Tauri unpacks bundled resources; the PyInstaller
/// one-dir build ships there as `resistamet-api/resistamet-api[.exe]`.
pub fn locate(exe_dir: Option<&Path>, resource_dir: Option<&Path>, repo_root: Option<&Path>) -> Launch {
    if let Ok(explicit) = std::env::var("RESISTAMET_PYTHON") {
        return Launch::Interpreter(PathBuf::from(explicit));
    }
    let binary = if cfg!(windows) { "resistamet-api.exe" } else { "resistamet-api" };
    for dir in [resource_dir.map(|d| d.join("resistamet-api")), exe_dir.map(PathBuf::from)]
        .into_iter()
        .flatten()
    {
        let sidecar = dir.join(binary);
        if sidecar.exists() {
            return Launch::Executable(sidecar);
        }
    }
    if let Some(root) = repo_root {
        let venv = if cfg!(windows) {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python")
        };
        if venv.exists() {
            return Launch::Interpreter(venv);
        }
    }
    Launch::Interpreter(PathBuf::from(if cfg!(windows) { "python" } else { "python3" }))
}

/// The source checkout this binary was built from, when it still exists.
/// Development only: a packaged app has no repo and falls through.
pub fn dev_repo_root() -> Option<PathBuf> {
    // desktop/src-tauri -> desktop -> repo
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let root = manifest.parent()?.parent()?.to_path_buf();
    if root.join("resistamet_gui").is_dir() { Some(root) } else { None }
}

pub struct SpawnOptions {
    pub launch: Launch,
    /// Working directory: relative `data_directory` values in profiles resolve here.
    pub cwd: PathBuf,
    /// Explicit config path, so the backend never guesses from its cwd.
    pub config: PathBuf,
    pub simulate: bool,
    /// Where the backend's stderr goes. `None` inherits the shell's, which is
    /// the terminal in development; a packaged app has none and passes a file.
    pub stderr_log: Option<std::fs::File>,
}

/// Windows `CREATE_NO_WINDOW`: start a console program without a console.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Start the backend and wait for its handshake.
pub fn spawn(mut options: SpawnOptions) -> Result<Backend, String> {
    let mut command = match &options.launch {
        Launch::Executable(path) => Command::new(path),
        Launch::Interpreter(python) => {
            let mut c = Command::new(python);
            c.arg("-m").arg("resistamet_gui.api");
            c
        }
    };
    command
        .arg("--port").arg("0")
        .arg("--config").arg(&options.config)
        .current_dir(&options.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        // The backend logs to stderr.
        .stderr(match options.stderr_log.take() {
            Some(file) => Stdio::from(file),
            None => Stdio::inherit(),
        });
    // The frozen backend is a console program, and the release shell is not:
    // Windows would give the child a console window of its own, and closing
    // that window kills the backend without its shutdown path, so the run is
    // not finalized and the source output stays on.
    // A development shell has a terminal, and the child shares it.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        if !cfg!(debug_assertions) {
            command.creation_flags(CREATE_NO_WINDOW);
        }
    }
    if options.simulate {
        command.arg("--simulate");
    }

    let mut child = command
        .spawn()
        .map_err(|e| format!("could not start the measurement backend ({:?}): {e}", options.launch))?;
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().ok_or("backend stdout was not captured")?;

    // Read the handshake on a helper thread so a backend that never prints
    // one cannot hang the app forever.
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let result = reader
            .read_line(&mut line)
            .map_err(|e| e.to_string())
            .and_then(|n| if n == 0 { Err("backend exited before its handshake".into()) } else { Ok(line) });
        let _ = tx.send(result);
        // Keep draining so the child never blocks on a full stdout pipe.
        let mut sink = String::new();
        while let Ok(n) = reader.read_line(&mut sink) {
            if n == 0 { break; }
            sink.clear();
        }
    });

    let line = match rx.recv_timeout(HANDSHAKE_TIMEOUT) {
        Ok(Ok(line)) => line,
        Ok(Err(e)) => {
            let _ = child.kill();
            return Err(e);
        }
        Err(_) => {
            let _ = child.kill();
            return Err("backend did not answer within 30 s".into());
        }
    };
    let info: BackendInfo = serde_json::from_str(line.trim())
        .map_err(|e| format!("backend handshake was not JSON ({e}): {line}"))?;

    Ok(Backend {
        child: Mutex::new(Some(child)),
        stdin: Mutex::new(stdin),
        info,
    })
}

impl Backend {
    /// Ask the backend to shut down and wait for it. Kills only as a last resort.
    pub fn shutdown(&self) {
        // Closing stdin is the watchdog signal the backend listens for.
        if let Ok(mut guard) = self.stdin.lock() {
            if let Some(mut stdin) = guard.take() {
                let _ = stdin.flush();
                drop(stdin);
            }
        }
        let mut guard = match self.child.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        let Some(mut child) = guard.take() else { return };
        let deadline = std::time::Instant::now() + SHUTDOWN_GRACE;
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) if std::time::Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(100));
                }
                _ => {
                    // Past the grace period: the run had its chance to turn the
                    // output off. Leaving a process holding the instrument is worse.
                    let _ = child.kill();
                    let _ = child.wait();
                    return;
                }
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// A stand-in backend: a shell script run the way the sidecar is.
    fn fake_backend(name: &str, body: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("resistamet-shell-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("resistamet-api");
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn options(script: &Path) -> SpawnOptions {
        let dir = script.parent().unwrap().to_path_buf();
        SpawnOptions {
            launch: Launch::Executable(script.to_path_buf()),
            config: dir.join("config.json"),
            cwd: dir,
            simulate: false,
            stderr_log: None,
        }
    }

    const HANDSHAKE: &str = r#"echo '{"url":"http://127.0.0.1:1","token":"t","pid":1}'"#;

    #[test]
    fn stderr_lands_in_the_log_file() {
        let script = fake_backend("stderr", &format!("echo 'starting up' >&2\n{HANDSHAKE}\ncat >/dev/null"));
        let log = script.parent().unwrap().join("backend.log");
        let mut opts = options(&script);
        opts.stderr_log = Some(std::fs::File::create(&log).unwrap());

        let backend = spawn(opts).unwrap();
        backend.shutdown();

        assert_eq!(std::fs::read_to_string(&log).unwrap(), "starting up\n");
        let _ = std::fs::remove_dir_all(script.parent().unwrap());
    }
}
