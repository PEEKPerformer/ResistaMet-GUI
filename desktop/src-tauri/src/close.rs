//! Whether a request to close the window closes it.
//!
//! Closing the window ends the app, and the app ending stops the run. With a
//! backend up, the shell therefore does not close on request: it asks the
//! webview, which knows whether a run is active and can ask the operator.
//! The webview says it has the question (`seen`), and later answers it
//! (`confirm`, or `keep` to stay open).
//!
//! A webview that never says it has the question is not going to ask anyone,
//! and a window that cannot be closed is its own hazard, so a second request
//! after `UNANSWERED` closes. The exit path still stops the run in order.

use std::sync::Mutex;
use std::time::{Duration, Instant};

/// How long the webview gets to acknowledge a close request.
const UNANSWERED: Duration = Duration::from_secs(3);

#[derive(Debug, PartialEq, Eq)]
pub enum Verdict {
    /// Let the window close.
    Close,
    /// Keep it open and put the question to the webview.
    Ask,
}

#[derive(Default)]
struct State {
    confirmed: bool,
    /// When the question now open was first put to the webview.
    asked_at: Option<Instant>,
    seen: bool,
}

#[derive(Default)]
pub struct CloseGate {
    state: Mutex<State>,
}

impl CloseGate {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The window was asked to close at `now`. Without a backend there is no
    /// run to protect and nothing to ask about.
    pub fn requested(&self, backend_ready: bool, now: Instant) -> Verdict {
        let mut state = self.lock();
        if state.confirmed || !backend_ready {
            return Verdict::Close;
        }
        match state.asked_at {
            Some(asked) if !state.seen && now.duration_since(asked) > UNANSWERED => Verdict::Close,
            Some(_) => Verdict::Ask,
            None => {
                state.asked_at = Some(now);
                state.seen = false;
                Verdict::Ask
            }
        }
    }

    /// The webview has the question and will answer it.
    pub fn seen(&self) {
        self.lock().seen = true;
    }

    /// The operator chose to stay: the question is closed, the window is not.
    pub fn keep(&self) {
        let mut state = self.lock();
        state.asked_at = None;
        state.seen = false;
    }

    /// Closing was agreed to; the next request goes through.
    pub fn confirm(&self) {
        self.lock().confirmed = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn with_a_backend_up_the_window_asks_instead_of_closing() {
        let gate = CloseGate::default();
        assert_eq!(gate.requested(true, Instant::now()), Verdict::Ask);
    }

    #[test]
    fn without_a_backend_there_is_nothing_to_ask_about() {
        let gate = CloseGate::default();
        assert_eq!(gate.requested(false, Instant::now()), Verdict::Close);
    }

    #[test]
    fn an_open_question_keeps_the_window_open_however_long_it_stands() {
        let gate = CloseGate::default();
        let t0 = Instant::now();
        assert_eq!(gate.requested(true, t0), Verdict::Ask);
        gate.seen();
        assert_eq!(gate.requested(true, t0 + Duration::from_secs(3600)), Verdict::Ask);
    }

    #[test]
    fn a_webview_that_never_took_the_question_cannot_hold_the_window_shut() {
        let gate = CloseGate::default();
        let t0 = Instant::now();
        assert_eq!(gate.requested(true, t0), Verdict::Ask);
        assert_eq!(gate.requested(true, t0 + Duration::from_secs(1)), Verdict::Ask);
        assert_eq!(gate.requested(true, t0 + Duration::from_secs(4)), Verdict::Close);
    }

    #[test]
    fn keeping_the_run_closes_the_question_and_the_next_request_asks_afresh() {
        let gate = CloseGate::default();
        let t0 = Instant::now();
        gate.requested(true, t0);
        gate.seen();
        gate.keep();
        // Unseen so far, but newly asked: not the unanswered case.
        assert_eq!(gate.requested(true, t0 + Duration::from_secs(60)), Verdict::Ask);
    }

    #[test]
    fn a_confirmed_close_goes_through() {
        let gate = CloseGate::default();
        gate.requested(true, Instant::now());
        gate.seen();
        gate.confirm();
        assert_eq!(gate.requested(true, Instant::now()), Verdict::Close);
    }
}
