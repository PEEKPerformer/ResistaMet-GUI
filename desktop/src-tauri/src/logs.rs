//! Where the backend's stderr goes in a packaged app.
//!
//! A packaged shell has no terminal, and on Windows the backend is started
//! without a console, so its log would otherwise go nowhere. Each launch of
//! the backend gets its own file in the app's log directory; the newest
//! `KEEP` are kept.

use std::fs::File;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// How many backend logs stay on disk, the new one included.
const KEEP: usize = 10;
const PREFIX: &str = "backend-";
const SUFFIX: &str = ".log";

/// Create this launch's log file in `dir` and drop the oldest beyond `KEEP`.
pub fn open_backend_log(dir: &Path) -> Result<(PathBuf, File), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    // Milliseconds since 1970, zero-padded so names sort in launch order.
    let path = dir.join(format!("{PREFIX}{stamp:015}{SUFFIX}"));
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    prune(dir, KEEP);
    Ok((path, file))
}

/// Remove all but the newest `keep` backend logs. Best effort: a log that
/// cannot be removed is left, and nothing else in the directory is touched.
fn prune(dir: &Path, keep: usize) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    let mut logs: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with(PREFIX) && name.ends_with(SUFFIX))
        })
        .collect();
    logs.sort();
    let excess = logs.len().saturating_sub(keep);
    for old in logs.into_iter().take(excess) {
        let _ = std::fs::remove_file(old);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("resistamet-logs-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn creates_the_directory_and_a_log_in_it() {
        let dir = scratch("create").join("nested");
        let (path, _file) = open_backend_log(&dir).unwrap();
        assert!(path.starts_with(&dir));
        assert!(path.exists());
        let _ = std::fs::remove_dir_all(dir.parent().unwrap());
    }

    #[test]
    fn keeps_the_newest_ten_and_leaves_other_files_alone() {
        let dir = scratch("prune");
        std::fs::create_dir_all(&dir).unwrap();
        for n in 0..14 {
            std::fs::write(dir.join(format!("{PREFIX}{n:015}{SUFFIX}")), b"").unwrap();
        }
        std::fs::write(dir.join("notes.txt"), b"").unwrap();

        let (newest, _file) = open_backend_log(&dir).unwrap();

        let mut left: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        left.sort();
        assert!(left.contains(&"notes.txt".to_string()));
        let logs: Vec<&String> = left.iter().filter(|n| n.starts_with(PREFIX)).collect();
        assert_eq!(logs.len(), KEEP);
        // The five oldest went; the new one stayed.
        assert_eq!(logs[0], &format!("{PREFIX}{:015}{SUFFIX}", 5));
        assert!(newest.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
