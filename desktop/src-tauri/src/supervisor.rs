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

pub struct Supervisor {
    lifecycle: Mutex<Lifecycle>,
    changed: Condvar,
}

impl Supervisor {
    pub fn new() -> Arc<Self> {
        Arc::new(Self { lifecycle: Mutex::new(Lifecycle::Pending), changed: Condvar::new() })
    }

    // A thread that panicked while holding the lock left a valid value
    // behind: every write is a single assignment.
    fn lock(&self) -> MutexGuard<'_, Lifecycle> {
        self.lifecycle.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Run `start` on a new thread and publish what came of it.
    pub fn start<F>(self: &Arc<Self>, start: F)
    where
        F: FnOnce() -> Result<Backend, String> + Send + 'static,
    {
        let supervisor = Arc::clone(self);
        std::thread::spawn(move || {
            let outcome = start();
            let mut lifecycle = supervisor.lock();
            if !matches!(*lifecycle, Lifecycle::Pending) {
                // The app began closing while the backend was starting.
                drop(lifecycle);
                if let Ok(backend) = outcome {
                    backend.shutdown();
                }
                return;
            }
            *lifecycle = match outcome {
                Ok(backend) => Lifecycle::Ready(backend),
                Err(reason) => Lifecycle::Failed(reason),
            };
            supervisor.changed.notify_all();
        });
    }

    /// Block until the backend is up or has failed to come up.
    pub fn wait_info(&self) -> Result<BackendInfo, String> {
        let mut lifecycle = self.lock();
        loop {
            match &*lifecycle {
                Lifecycle::Pending => {
                    lifecycle = self.changed.wait(lifecycle).unwrap_or_else(|poisoned| poisoned.into_inner());
                }
                Lifecycle::Ready(backend) => return Ok(backend.info.clone()),
                Lifecycle::Failed(reason) => return Err(reason.clone()),
            }
        }
    }

    /// Give the backend its ordered shutdown. For the app's exit.
    pub fn shutdown(&self) {
        let was = std::mem::replace(&mut *self.lock(), Lifecycle::Failed("the application is closing".into()));
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
        let supervisor = Supervisor::new();
        supervisor.start(|| Err("could not start the measurement backend: no such file".into()));
        assert_eq!(
            supervisor.wait_info().unwrap_err(),
            "could not start the measurement backend: no such file"
        );
    }

    #[test]
    fn asking_waits_for_a_slow_start() {
        let supervisor = Supervisor::new();
        let asked = Instant::now();
        supervisor.start(|| {
            std::thread::sleep(Duration::from_millis(300));
            Err("late".into())
        });
        assert_eq!(supervisor.wait_info().unwrap_err(), "late");
        assert!(asked.elapsed() >= Duration::from_millis(300));
    }

    #[test]
    fn closing_releases_anyone_still_waiting() {
        let supervisor = Supervisor::new();
        let waiter = {
            let supervisor = Arc::clone(&supervisor);
            std::thread::spawn(move || supervisor.wait_info())
        };
        std::thread::sleep(Duration::from_millis(50));
        supervisor.shutdown();
        assert!(waiter.join().unwrap().is_err());
    }
}
