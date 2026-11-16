//! The data folder: the directory an installed backend runs in.
//!
//! A profile's `data_directory` is `measurement_data` by default, a relative
//! path, so the backend's working directory decides where every file lands.
//! That used to be the per-user app data directory, which nobody looks in and
//! no backup covers. It is now a folder the operator chooses, kept in
//! `data-folder.json` in the app data directory; `Documents/ResistaMet` until
//! they have chosen. `config.json`, logs and locks do not move.
//!
//! A source checkout has none of this: its backend runs at the repository root.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

/// The file in the app data directory that holds the choice.
const STORE_FILE: &str = "data-folder.json";
/// The default folder's name inside the user's Documents.
const DEFAULT_NAME: &str = "ResistaMet";
/// What a relative default `data_directory` made inside the app data
/// directory before there was a data folder (2.0.0-1 to 2.0.0-3).
const LEGACY_NAME: &str = "measurement_data";

#[derive(Serialize, Deserialize)]
struct Stored {
    path: PathBuf,
}

/// Which folder a backend launch runs in, and how that was arrived at.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    /// The backend's working directory.
    pub folder: PathBuf,
    /// Whether `folder` is the operator's stored choice.
    pub chosen: bool,
    /// The folder used when nothing is chosen.
    pub default_folder: PathBuf,
    /// The default is the app data directory because Documents could not be
    /// found or written to.
    pub default_is_fallback: bool,
    /// A stored choice that could not be used. Data is not sent somewhere
    /// else in silence: the webview is told, and asks again.
    pub unavailable: Option<PathBuf>,
}

/// Decide the folder. `usable` says whether a directory exists (or can be
/// made) and can be written to; it is only asked about a folder that would
/// then be used, so a launch with a working choice leaves Documents alone.
pub fn decide(
    stored: Option<&Path>,
    documents: Option<&Path>,
    app_data: &Path,
    usable: impl Fn(&Path) -> bool,
) -> Decision {
    let preferred = documents.map(|d| d.join(DEFAULT_NAME));
    if let Some(stored) = stored {
        // A relative choice would resolve against whatever the shell's own
        // working directory happens to be.
        if stored.is_absolute() && usable(stored) {
            return Decision {
                folder: stored.to_path_buf(),
                chosen: true,
                default_is_fallback: preferred.is_none(),
                default_folder: preferred.unwrap_or_else(|| app_data.to_path_buf()),
                unavailable: None,
            };
        }
    }
    let (default_folder, default_is_fallback) = match preferred {
        Some(preferred) if usable(&preferred) => (preferred, false),
        _ => (app_data.to_path_buf(), true),
    };
    Decision {
        folder: default_folder.clone(),
        chosen: false,
        default_folder,
        default_is_fallback,
        unavailable: stored.map(Path::to_path_buf),
    }
}

/// The stored choice, if there is one that can be read.
pub fn read_choice(app_data: &Path) -> Option<PathBuf> {
    let text = std::fs::read_to_string(app_data.join(STORE_FILE)).ok()?;
    let stored: Stored = serde_json::from_str(&text).ok()?;
    if stored.path.as_os_str().is_empty() { None } else { Some(stored.path) }
}

/// Store the choice: written beside the file and renamed over it, so a
/// crash leaves the old choice or the new one.
pub fn write_choice(app_data: &Path, folder: &Path) -> Result<(), String> {
    let target = app_data.join(STORE_FILE);
    let partial = app_data.join(format!("{STORE_FILE}.tmp"));
    let text = serde_json::to_string_pretty(&Stored { path: folder.to_path_buf() })
        .map_err(|e| format!("cannot encode the data folder choice: {e}"))?;
    std::fs::write(&partial, text).map_err(|e| format!("cannot write {}: {e}", partial.display()))?;
    std::fs::rename(&partial, &target).map_err(|e| format!("cannot write {}: {e}", target.display()))
}

/// Create `folder` if need be and prove a file can be written in it.
pub fn ensure_writable(folder: &Path) -> Result<(), String> {
    std::fs::create_dir_all(folder).map_err(|e| format!("cannot create {}: {e}", folder.display()))?;
    let probe = folder.join(format!(".resistamet-write-test-{}", std::process::id()));
    std::fs::write(&probe, b"").map_err(|e| format!("cannot write in {}: {e}", folder.display()))?;
    let _ = std::fs::remove_file(&probe);
    Ok(())
}

/// Where earlier versions put the files, when anything is still there.
pub fn legacy_location(app_data: &Path) -> Option<PathBuf> {
    let legacy = app_data.join(LEGACY_NAME);
    if holds_a_file(&legacy, 4) { Some(legacy) } else { None }
}

/// Whether `dir` or a directory at most `depth` levels below it has a file.
/// The layout is `measurement_data/<operator>/<file>`, so this is shallow.
fn holds_a_file(dir: &Path, depth: u32) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else { return false };
    entries.flatten().any(|entry| {
        let path = entry.path();
        path.is_file() || (depth > 0 && path.is_dir() && holds_a_file(&path, depth - 1))
    })
}

/// The decision the running backend was started with. The answer to "where
/// is data going" is that launch's folder, not what the same question would
/// give now: a drive plugged in since then has not moved the backend.
#[derive(Default)]
pub struct InUse(Mutex<Option<Decision>>);

impl InUse {
    pub fn get(&self) -> Option<Decision> {
        self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).clone()
    }

    pub fn set(&self, decision: Decision) {
        *self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(decision);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Absolute on the platform the tests run on.
    fn abs(name: &str) -> PathBuf {
        if cfg!(windows) { PathBuf::from(format!(r"C:\{name}")) } else { PathBuf::from(format!("/{name}")) }
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("resistamet-data-folder-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn with_no_choice_the_default_is_in_documents() {
        let decision = decide(None, Some(&abs("Documents")), &abs("appdata"), |_| true);
        assert_eq!(
            decision,
            Decision {
                folder: abs("Documents").join("ResistaMet"),
                chosen: false,
                default_folder: abs("Documents").join("ResistaMet"),
                default_is_fallback: false,
                unavailable: None,
            }
        );
    }

    #[test]
    fn without_a_documents_directory_the_app_data_directory_stands_in_and_says_so() {
        let decision = decide(None, None, &abs("appdata"), |_| true);
        assert_eq!(decision.folder, abs("appdata"));
        assert!(decision.default_is_fallback);
        assert!(!decision.chosen);
    }

    #[test]
    fn a_documents_directory_that_cannot_be_written_falls_back_the_same_way() {
        let documents = abs("Documents");
        let decision = decide(None, Some(&documents), &abs("appdata"), |p| !p.starts_with(&documents));
        assert_eq!(decision.folder, abs("appdata"));
        assert_eq!(decision.default_folder, abs("appdata"));
        assert!(decision.default_is_fallback);
    }

    #[test]
    fn a_usable_choice_is_used_and_documents_is_not_touched() {
        let asked = std::cell::RefCell::new(Vec::new());
        let decision = decide(Some(&abs("lab")), Some(&abs("Documents")), &abs("appdata"), |p| {
            asked.borrow_mut().push(p.to_path_buf());
            true
        });
        assert_eq!(decision.folder, abs("lab"));
        assert!(decision.chosen);
        assert_eq!(decision.unavailable, None);
        assert_eq!(decision.default_folder, abs("Documents").join("ResistaMet"));
        assert_eq!(*asked.borrow(), vec![abs("lab")]);
    }

    #[test]
    fn choosing_the_default_folder_counts_as_chosen() {
        let default = abs("Documents").join("ResistaMet");
        let decision = decide(Some(&default), Some(&abs("Documents")), &abs("appdata"), |_| true);
        assert!(decision.chosen);
        assert_eq!(decision.folder, default);
    }

    #[test]
    fn an_unavailable_choice_runs_in_the_default_and_is_reported() {
        let gone = abs("unplugged");
        let decision = decide(Some(&gone), Some(&abs("Documents")), &abs("appdata"), |p| p != gone);
        assert_eq!(decision.folder, abs("Documents").join("ResistaMet"));
        assert!(!decision.chosen);
        assert_eq!(decision.unavailable, Some(gone));
    }

    #[test]
    fn a_relative_choice_is_not_used() {
        let decision = decide(Some(Path::new("data")), Some(&abs("Documents")), &abs("appdata"), |_| true);
        assert!(!decision.chosen);
        assert_eq!(decision.unavailable, Some(PathBuf::from("data")));
    }

    #[test]
    fn the_choice_survives_a_round_trip_and_garbage_reads_as_none() {
        let app_data = scratch("store");
        assert_eq!(read_choice(&app_data), None);

        write_choice(&app_data, &abs("lab")).unwrap();
        assert_eq!(read_choice(&app_data), Some(abs("lab")));

        std::fs::write(app_data.join(STORE_FILE), "not json").unwrap();
        assert_eq!(read_choice(&app_data), None);
        std::fs::write(app_data.join(STORE_FILE), r#"{"path": ""}"#).unwrap();
        assert_eq!(read_choice(&app_data), None);
        let _ = std::fs::remove_dir_all(&app_data);
    }

    #[test]
    fn a_writable_folder_is_created_and_left_empty() {
        let root = scratch("writable");
        let folder = root.join("a").join("b");
        ensure_writable(&folder).unwrap();
        assert_eq!(std::fs::read_dir(&folder).unwrap().count(), 0);

        // A file where the folder should be cannot become one.
        let file = root.join("file");
        std::fs::write(&file, "x").unwrap();
        assert!(ensure_writable(&file.join("below")).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_legacy_location_counts_only_when_it_holds_a_file() {
        let app_data = scratch("legacy");
        assert_eq!(legacy_location(&app_data), None);

        let operator = app_data.join(LEGACY_NAME).join("operator");
        std::fs::create_dir_all(&operator).unwrap();
        assert_eq!(legacy_location(&app_data), None, "empty folders are not data");

        std::fs::write(operator.join("run.csv"), "x").unwrap();
        assert_eq!(legacy_location(&app_data), Some(app_data.join(LEGACY_NAME)));
        let _ = std::fs::remove_dir_all(&app_data);
    }
}
