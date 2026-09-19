//! The backend's lifecycle as the rest of the shell sees it.
//!
//! Starting the backend takes seconds and can fail: no sidecar, no handshake,
//! a handshake that is not one. None of that may happen on the main thread or
//! surface as an error from Tauri's setup hook, which panics on one — and a
//! release build aborts on panic, so the app would vanish without a word.
//! Instead the backend starts on its own thread, and whoever asks for it
//! waits for the outcome and gets the reason when there is no backend.

use std::sync::{Arc, Condvar, Mutex, MutexGuard};

use crate::backend::{Backend, BackendInfo};

enum Lifecycle {
    /// Being started; the outcome is not known yet.
    Pending,
    Ready(Backend),
    /// No backend, and why, in words the operator can be shown.
    Failed(String),
}

struct Inner {
    lifecycle: Lifecycle,
    /// Counts launches, so an exit is only ever charged to the backend it
    /// belongs to and not to the one that replaced it.
    launch: u64,
}

pub struct Supervisor {
    inner: Mutex<Inner>,
    changed: Condvar,
}

/// Reports a backend's unasked-for exit. Handed to `start`, which passes it
/// down to the process watcher.
pub type ExitHook = Box<dyn FnOnce(Option<i32>) + Send>;

/// What the operator is told when the backend ends on its own. A backend
/// that did not shut down in order did not turn the output off in order.
pub fn exit_message(code: Option<i32>) -> String {
    let how = match code {
        Some(code) => format!("exited (code {code})"),
        None => "was killed".to_string(),
    };
    format!(
        "The measurement backend {how}. The instrument's output state is unknown: check the front panel."
    )
}

impl Supervisor {
    /// Begin starting a backend: `start` runs on a new thread and is given the
    /// hook to report that backend's exit with. `exited` is told, once, when
    /// the backend this launch produced ends on its own.
    pub fn launch<F, N>(start: F, exited: N) -> Arc<Self>
    where
        F: FnOnce(ExitHook) -> Result<Backend, String> + Send + 'static,
        N: FnOnce(Option<i32>) + Send + 'static,
    {
        let supervisor = Arc::new(Self {
            inner: Mutex::new(Inner { lifecycle: Lifecycle::Pending, launch: 1 }),
            changed: Condvar::new(),
        });
        supervisor.run(1, None, start, exited);
        supervisor
    }

    /// Replace the backend with a fresh one. The old one, if it is still
    /// there, gets its ordered shutdown first. Does nothing while a start is
    /// already under way; ask `wait_info` for the outcome either way.
    pub fn restart<F, N>(self: &Arc<Self>, start: F, exited: N)
    where
        F: FnOnce(ExitHook) -> Result<Backend, String> + Send + 'static,
        N: FnOnce(Option<i32>) + Send + 'static,
    {
        let (launch, old) = {
            let mut inner = self.lock();
            if matches!(inner.lifecycle, Lifecycle::Pending) {
                return;
            }
            inner.launch += 1;
            (inner.launch, std::mem::replace(&mut inner.lifecycle, Lifecycle::Pending))
        };
        self.run(launch, Some(old), start, exited);
    }

    // A thread that panicked while holding the lock left a valid value
    // behind: every write is a single assignment.
    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn run<F, N>(self: &Arc<Self>, launch: u64, old: Option<Lifecycle>, start: F, exited: N)
    where
        F: FnOnce(ExitHook) -> Result<Backend, String> + Send + 'static,
        N: FnOnce(Option<i32>) + Send + 'static,
    {
        let supervisor = Arc::clone(self);
        std::thread::spawn(move || {
            if let Some(Lifecycle::Ready(old)) = old {
                old.shutdown();
            }
            let watcher = Arc::clone(&supervisor);
            let outcome = start(Box::new(move |code| {
                if watcher.backend_exited(launch, code) {
                    exited(code);
                }
            }));
            let mut inner = supervisor.lock();
            if !matches!(inner.lifecycle, Lifecycle::Pending) {
                // The app began closing while the backend was starting.
                drop(inner);
                if let Ok(backend) = outcome {
                    backend.shutdown();
                }
                return;
            }
            inner.lifecycle = match outcome {
                Ok(backend) => Lifecycle::Ready(backend),
                Err(reason) => Lifecycle::Failed(reason),
            };
            supervisor.changed.notify_all();
        });
    }

    /// The backend of launch `launch` ended on its own. True when that is the
    /// backend the app was using, which is now recorded as gone.
    fn backend_exited(&self, launch: u64, code: Option<i32>) -> bool {
        let mut inner = self.lock();
        // It can die between its handshake and being published as ready.
        while inner.launch == launch && matches!(inner.lifecycle, Lifecycle::Pending) {
            inner = self.changed.wait(inner).unwrap_or_else(|poisoned| poisoned.into_inner());
        }
        if inner.launch != launch || !matches!(inner.lifecycle, Lifecycle::Ready(_)) {
            return false;
        }
        inner.lifecycle = Lifecycle::Failed(exit_message(code));
        self.changed.notify_all();
        true
    }

    /// Block until the backend is up or has failed to come up.
    pub fn wait_info(&self) -> Result<BackendInfo, String> {
        let mut inner = self.lock();
        loop {
            match &inner.lifecycle {
                Lifecycle::Pending => {
                    inner = self.changed.wait(inner).unwrap_or_else(|poisoned| poisoned.into_inner());
                }
                Lifecycle::Ready(backend) => return Ok(backend.info.clone()),
                Lifecycle::Failed(reason) => return Err(reason.clone()),
            }
        }
    }

    /// Give the backend its ordered shutdown. For the app's exit.
    pub fn shutdown(&self) {
        let closing = Lifecycle::Failed("the application is closing".into());
        let was = std::mem::replace(&mut self.lock().lifecycle, closing);
        self.changed.notify_all();
        if let Lifecycle::Ready(backend) = was {
            backend.shutdown();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn a_backend_that_fails_to_start_is_reported_not_fatal() {
        let supervisor = Supervisor::launch(
            |_| Err("could not start the measurement backend: no such file".into()),
            |_| {},
        );
        assert_eq!(
            supervisor.wait_info().unwrap_err(),
            "could not start the measurement backend: no such file"
        );
    }

    #[test]
    fn asking_waits_for_a_slow_start() {
        let asked = Instant::now();
        let supervisor = Supervisor::launch(
            |_| {
                std::thread::sleep(Duration::from_millis(300));
                Err("late".into())
            },
            |_| {},
        );
        assert_eq!(supervisor.wait_info().unwrap_err(), "late");
        assert!(asked.elapsed() >= Duration::from_millis(300));
    }

    #[test]
    fn closing_releases_anyone_still_waiting() {
        let (release, held) = std::sync::mpsc::channel::<()>();
        let supervisor = Supervisor::launch(
            move |_| {
                let _ = held.recv();
                Err("never published".into())
            },
            |_| {},
        );
        let waiter = {
            let supervisor = Arc::clone(&supervisor);
            std::thread::spawn(move || supervisor.wait_info())
        };
        std::thread::sleep(Duration::from_millis(50));
        supervisor.shutdown();
        assert_eq!(waiter.join().unwrap().unwrap_err(), "the application is closing");
        drop(release);
    }

    #[cfg(unix)]
    mod with_a_process {
        use super::*;
        use crate::backend::tests::{fake_backend, options, HANDSHAKE};
        use crate::backend::spawn;
        use std::sync::mpsc;

        #[test]
        fn a_backend_that_dies_becomes_a_failure_with_the_front_panel_warning() {
            let script = fake_backend("sup-dies", &format!("{HANDSHAKE}\nsleep 0.2\nexit 3"));
            let (tx, exited) = mpsc::channel();
            let supervisor = Supervisor::launch(
                {
                    let script = script.clone();
                    move |on_exit| {
                        let mut opts = options(&script);
                        opts.on_exit = on_exit;
                        spawn(opts)
                    }
                },
                move |code| {
                    let _ = tx.send(code);
                },
            );

            assert_eq!(exited.recv_timeout(Duration::from_secs(5)), Ok(Some(3)));
            let error = supervisor.wait_info().unwrap_err();
            assert!(error.contains("exited (code 3)"), "{error}");
            assert!(error.contains("check the front panel"), "{error}");
            let _ = std::fs::remove_dir_all(script.parent().unwrap());
        }

        #[test]
        fn restart_replaces_a_dead_backend_with_a_live_one() {
            let dead = fake_backend("sup-dead", &format!("{HANDSHAKE}\nexit 0"));
            let live = fake_backend(
                "sup-live",
                r#"echo '{"url":"http://127.0.0.1:2","token":"second","pid":2}'
cat >/dev/null"#,
            );
            let (tx, exited) = mpsc::channel();
            let supervisor = Supervisor::launch(
                {
                    let dead = dead.clone();
                    move |on_exit| {
                        let mut opts = options(&dead);
                        opts.on_exit = on_exit;
                        spawn(opts)
                    }
                },
                move |code| {
                    let _ = tx.send(code);
                },
            );
            assert_eq!(exited.recv_timeout(Duration::from_secs(5)), Ok(Some(0)));
            assert!(supervisor.wait_info().is_err());

            supervisor.restart(
                {
                    let live = live.clone();
                    move |on_exit| {
                        let mut opts = options(&live);
                        opts.on_exit = on_exit;
                        spawn(opts)
                    }
                },
                |_| panic!("the live backend was reported as exited"),
            );
            assert_eq!(supervisor.wait_info().unwrap().token, "second");

            supervisor.shutdown();
            for script in [dead, live] {
                let _ = std::fs::remove_dir_all(script.parent().unwrap());
            }
        }
    }
}
