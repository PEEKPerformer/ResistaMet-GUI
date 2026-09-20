//! The Python backend as a child process.
//!
//! `python -m resistamet_gui.api` prints one JSON line on stdout — url, token,
//! pid — and then serves until its stdin closes. This module starts it, reads
//! that line, keeps stdin open for the life of the window, and on exit closes
//! stdin and waits for the process to finish its own shutdown (stop the run,
//! output off, finalize the file) before killing it as a last resort.
//!
//! Where the interpreter comes from, in order:
//!   1. `RESISTAMET_PYTHON` — an explicit override, in development builds only.
//!   2. A bundled sidecar next to the executable (`resistamet-api[.exe]`),
//!      which step 3 of the migration adds.
//!   3. The repo's `.venv`, when a development build runs from its checkout.
//!   4. `python3` / `python` on PATH.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
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
    /// Whether this backend was started against the simulator. Not part of
    /// the handshake: the shell knows because it passed `--simulate`, and
    /// the UI must say so, since the simulator answers like the instrument.
    #[serde(default)]
    pub simulated: bool,
}

/// The running backend. Held in Tauri state for the life of the app.
pub struct Backend {
    /// Shared with the watcher thread. Whoever takes the child out owns its
    /// end: `shutdown` for an exit the shell asked for, the watcher otherwise.
    child: Arc<Mutex<Option<Child>>>,
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
pub fn locate(
    explicit: Option<&Path>,
    exe_dir: Option<&Path>,
    resource_dir: Option<&Path>,
    repo_root: Option<&Path>,
) -> Launch {
    if let Some(explicit) = explicit {
        return Launch::Interpreter(explicit.to_path_buf());
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
///
/// Development builds only, and compiled out of the rest: a release binary
/// run on the machine that built it would otherwise find the checkout and
/// work in it — its config.json, its measurement_data — instead of the app
/// data directory every other machine uses.
pub fn dev_repo_root() -> Option<PathBuf> {
    #[cfg(debug_assertions)]
    {
        // desktop/src-tauri -> desktop -> repo
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
        let root = manifest.parent()?.parent()?.to_path_buf();
        if root.join("resistamet_gui").is_dir() { Some(root) } else { None }
    }
    #[cfg(not(debug_assertions))]
    None
}

/// What a developer can switch from the environment. A packaged app honours
/// none of it: a variable left set on a lab PC must not point the app at
/// another interpreter, or at the simulator with nothing on screen to say so.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct DevOverrides {
    /// `RESISTAMET_PYTHON`: the interpreter to run the backend with.
    pub python: Option<PathBuf>,
    /// `RESISTAMET_SIMULATE=1`: run against the in-package simulator.
    pub simulate: bool,
}

/// Read the overrides through `env`, or none of them when `dev` is false.
pub fn dev_overrides(dev: bool, env: impl Fn(&str) -> Option<String>) -> DevOverrides {
    if !dev {
        return DevOverrides::default();
    }
    DevOverrides {
        python: env("RESISTAMET_PYTHON").map(PathBuf::from),
        simulate: env("RESISTAMET_SIMULATE").is_some_and(|v| v == "1"),
    }
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
    /// Called once, with the exit code, when a backend that completed its
    /// handshake ends without the shell having asked it to. Not called for a
    /// failed start (`spawn` returns that) or after `Backend::shutdown`.
    pub on_exit: Box<dyn FnOnce(Option<i32>) + Send>,
}

/// How often the watcher looks at a backend whose stdout has closed.
const EXIT_POLL: Duration = Duration::from_millis(100);

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
    let (watch_tx, watch_rx) = mpsc::channel::<Arc<Mutex<Option<Child>>>>();
    let on_exit = options.on_exit;
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
        // stdout closed: the backend is going or gone. Nothing else in the
        // shell would notice, and the UI would show a dead backend as an
        // outage with a run still "running". A failed start never sends the
        // child here, so only a backend that was handed to the app is watched.
        let Ok(child) = watch_rx.recv() else { return };
        loop {
            {
                let mut guard = child.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                // Taken by `shutdown`: an exit the shell asked for.
                let Some(running) = guard.as_mut() else { return };
                match running.try_wait() {
                    Ok(Some(status)) => {
                        guard.take();
                        drop(guard);
                        on_exit(status.code());
                        return;
                    }
                    Ok(None) => {}
                    Err(_) => return,
                }
            }
            std::thread::sleep(EXIT_POLL);
        }
    });

    // A backend the shell cannot talk to is ended and reaped here, on every
    // path: the app stays up after a failed start, so its own exit no longer
    // tidies up behind it.
    let mut abandon = |reason: String| {
        let _ = child.kill();
        let _ = child.wait();
        reason
    };
    let line = match rx.recv_timeout(HANDSHAKE_TIMEOUT) {
        Ok(Ok(line)) => line,
        Ok(Err(e)) => return Err(abandon(e)),
        Err(_) => return Err(abandon("backend did not answer within 30 s".into())),
    };
    let info: BackendInfo = match serde_json::from_str::<BackendInfo>(line.trim()) {
        Ok(info) => BackendInfo { simulated: options.simulate, ..info },
        Err(e) => return Err(abandon(format!("backend handshake was not JSON ({e}): {line}"))),
    };

    let child = Arc::new(Mutex::new(Some(child)));
    let _ = watch_tx.send(Arc::clone(&child));
    Ok(Backend {
        child,
        stdin: Mutex::new(stdin),
        info,
    })
}

impl Backend {
    /// Ask the backend to shut down and wait for it. Kills only as a last resort.
    pub fn shutdown(&self) {
        // Take the child before anything makes it exit, so the watcher knows
        // this exit was asked for and stays quiet.
        let taken = self.child.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).take();
        // Closing stdin is the watchdog signal the backend listens for.
        if let Ok(mut guard) = self.stdin.lock() {
            if let Some(mut stdin) = guard.take() {
                let _ = stdin.flush();
                drop(stdin);
            }
        }
        let Some(mut child) = taken else { return };
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

#[cfg(test)]
mod override_tests {
    use super::*;

    fn lab_pc_env(name: &str) -> Option<String> {
        match name {
            "RESISTAMET_PYTHON" => Some("/somewhere/python".into()),
            "RESISTAMET_SIMULATE" => Some("1".into()),
            _ => None,
        }
    }

    #[test]
    fn a_packaged_app_ignores_the_development_variables() {
        assert_eq!(dev_overrides(false, lab_pc_env), DevOverrides { python: None, simulate: false });
    }

    #[test]
    fn a_development_build_honours_them() {
        let overrides = dev_overrides(true, lab_pc_env);
        assert_eq!(overrides.python.as_deref(), Some(Path::new("/somewhere/python")));
        assert!(overrides.simulate);
        assert!(!dev_overrides(true, |_| None).simulate);
    }

    #[test]
    fn an_explicit_interpreter_outranks_everything_found_on_disk() {
        let launch = locate(Some(Path::new("/somewhere/python")), None, None, None);
        assert!(matches!(launch, Launch::Interpreter(p) if p == Path::new("/somewhere/python")));
    }
}

#[cfg(all(test, unix))]
pub(crate) mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// A stand-in backend: a shell script run the way the sidecar is.
    pub(crate) fn fake_backend(name: &str, body: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("resistamet-shell-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("resistamet-api");
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    pub(crate) fn options(script: &Path) -> SpawnOptions {
        let dir = script.parent().unwrap().to_path_buf();
        SpawnOptions {
            launch: Launch::Executable(script.to_path_buf()),
            config: dir.join("config.json"),
            cwd: dir,
            simulate: false,
            stderr_log: None,
            on_exit: Box::new(|_| {}),
        }
    }

    /// Options whose `on_exit` reports into the returned channel.
    fn watched(script: &Path) -> (SpawnOptions, mpsc::Receiver<Option<i32>>) {
        let (tx, rx) = mpsc::channel();
        let mut opts = options(script);
        opts.on_exit = Box::new(move |code| {
            let _ = tx.send(code);
        });
        (opts, rx)
    }

    #[test]
    fn a_backend_that_dies_on_its_own_is_reported_with_its_status() {
        let script = fake_backend("dies", &format!("{HANDSHAKE}\nexit 3"));
        let (opts, exited) = watched(&script);
        let _backend = spawn(opts).unwrap();
        assert_eq!(exited.recv_timeout(Duration::from_secs(5)), Ok(Some(3)));
        let _ = std::fs::remove_dir_all(script.parent().unwrap());
    }

    #[test]
    fn a_shutdown_the_shell_asked_for_is_not_reported() {
        let script = fake_backend("asked", &format!("{HANDSHAKE}\ncat >/dev/null"));
        let (opts, exited) = watched(&script);
        let backend = spawn(opts).unwrap();
        backend.shutdown();
        // The watcher ends without calling back, which drops the sender.
        assert_eq!(exited.recv_timeout(Duration::from_secs(5)), Err(mpsc::RecvTimeoutError::Disconnected));
        let _ = std::fs::remove_dir_all(script.parent().unwrap());
    }

    #[test]
    fn a_failed_start_is_not_reported_as_an_exit() {
        let script = fake_backend("nostart", "exit 1");
        let (opts, exited) = watched(&script);
        assert!(spawn(opts).is_err());
        assert_eq!(exited.recv_timeout(Duration::from_secs(5)), Err(mpsc::RecvTimeoutError::Disconnected));
        let _ = std::fs::remove_dir_all(script.parent().unwrap());
    }

    pub(crate) const HANDSHAKE: &str = r#"echo '{"url":"http://127.0.0.1:1","token":"t","pid":1}'"#;

    #[test]
    fn a_simulated_backend_is_marked_as_one() {
        let script = fake_backend("simulated", &format!("echo \"$*\" > args\n{HANDSHAKE}\ncat >/dev/null"));
        let mut opts = options(&script);
        opts.simulate = true;
        let backend = spawn(opts).unwrap();
        assert!(backend.info.simulated);
        backend.shutdown();
        let args = std::fs::read_to_string(script.parent().unwrap().join("args")).unwrap();
        assert!(args.contains("--simulate"), "{args}");

        // And a real one is not, whatever a handshake might claim.
        let real = fake_backend(
            "notsimulated",
            r#"echo '{"url":"http://127.0.0.1:1","token":"t","pid":1,"simulated":true}'
cat >/dev/null"#,
        );
        let backend = spawn(options(&real)).unwrap();
        assert!(!backend.info.simulated);
        backend.shutdown();
        for script in [script, real] {
            let _ = std::fs::remove_dir_all(script.parent().unwrap());
        }
    }

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

    fn alive(pid: &str) -> bool {
        Command::new("kill").arg("-0").arg(pid).stderr(Stdio::null()).status().unwrap().success()
    }

    #[test]
    fn a_handshake_that_is_not_json_does_not_leave_the_process_behind() {
        // The stand-in ignores its stdin, so only a kill ends it.
        let script = fake_backend("badjson", "echo $$ > pid\necho 'not a handshake'\nexec sleep 30");
        let error = spawn(options(&script)).err().expect("a bad handshake is an error");
        assert!(error.contains("not JSON"), "{error}");

        let pid = std::fs::read_to_string(script.parent().unwrap().join("pid")).unwrap();
        assert!(!alive(pid.trim()), "the backend process is still there");
        let _ = std::fs::remove_dir_all(script.parent().unwrap());
    }
}
