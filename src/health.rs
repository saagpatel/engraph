use std::collections::HashSet;
use std::path::Path;

use anyhow::Result;

use crate::store::Store;

/// Full vault health report.
#[derive(Debug, Clone, serde::Serialize)]
pub struct HealthReport {
    pub orphans: Vec<String>,
    /// Wikilinks whose target exists nowhere on disk. These are the actionable
    /// ones: something is genuinely missing or misspelled.
    pub broken_links: Vec<BrokenLink>,
    /// Wikilinks whose target EXISTS on disk but is not in the index, because
    /// it sits under a configured `exclude` pattern or a vault `.ignore`.
    ///
    /// Nothing is wrong with these links, and no amount of reindexing will
    /// clear them, so reporting them as broken made the whole check cry wolf.
    /// Measured on the live vault before this split: 252 of 260 unresolved
    /// links pointed at files that were present the whole time.
    pub excluded_links: Vec<BrokenLink>,
    /// Whether the split above actually ran. False means no vault path was
    /// available, `excluded_links` is empty for that reason rather than
    /// because there were none, and `broken_links` is unpartitioned.
    pub disk_reconciled: bool,
    pub stale_notes: Vec<String>,
    pub inbox_pending: Vec<String>,
    pub tag_issues: Vec<TagIssue>,
    pub index_age_seconds: u64,
    pub total_files: usize,
}

/// A wikilink that could not be resolved to any indexed file.
#[derive(Debug, Clone, serde::Serialize)]
pub struct BrokenLink {
    pub source: String,
    pub target: String,
}

/// A tag-related problem in a file.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TagIssue {
    pub file: String,
    pub issue: String,
}

/// Configuration controlling which folders are excluded from health checks.
pub struct HealthConfig {
    pub daily_folder: Option<String>,
    pub inbox_folder: Option<String>,
    /// Vault root, used to tell "this target does not exist" apart from "this
    /// target exists but is not in the index".
    ///
    /// When `None` the two cannot be separated, every unresolved link stays in
    /// `broken_links`, and the report sets `disk_reconciled: false` so the
    /// number is never read as more certain than it is.
    pub vault_path: Option<std::path::PathBuf>,
}

/// Find files with no edges (neither incoming nor outgoing).
///
/// Excludes files whose path starts with the configured daily or inbox folder
/// prefixes — those are expected to be unlinked.
pub fn find_orphans(store: &Store, config: &HealthConfig) -> Result<Vec<String>> {
    let mut exclude = Vec::new();
    if let Some(ref daily) = config.daily_folder {
        exclude.push(daily.as_str());
    }
    if let Some(ref inbox) = config.inbox_folder {
        exclude.push(inbox.as_str());
    }
    let isolated = store.find_isolated_files(&exclude)?;
    Ok(isolated.into_iter().map(|f| f.path).collect())
}

/// Find wikilink references that could not be resolved to any indexed file.
///
/// These are recorded in the `unresolved_links` table during indexing.
pub fn find_broken_links(store: &Store) -> Result<Vec<BrokenLink>> {
    let unresolved = store.get_unresolved_links()?;
    Ok(unresolved
        .into_iter()
        .map(|(source, target)| BrokenLink { source, target })
        .collect())
}

/// Split unresolved links into genuinely-missing and merely-not-indexed.
///
/// The index only knows what it was allowed to index. A wikilink pointing into
/// an excluded layer can never resolve, so it was recorded as broken and stayed
/// broken through every reindex — the "163 broken links unchanged after 50+
/// reindexes" report. Reindexing was never going to help: the target was
/// excluded by configuration, not absent.
///
/// So the question "is this link broken?" is a question about the DISK, and it
/// gets asked here rather than against index membership.
///
/// Matching deliberately mirrors `indexer::resolve_link_target`: exact relative
/// path with or without the `.md` extension, else case-insensitive basename. If
/// the two ever disagree, a link would be called broken while the indexer would
/// happily resolve it.
pub fn reconcile_links_with_disk(
    links: Vec<BrokenLink>,
    vault_path: &Path,
) -> Result<(Vec<BrokenLink>, Vec<BrokenLink>)> {
    // Walk with NO exclusions and no gitignore filtering: the whole point is to
    // see files the indexer was told to skip.
    let files = crate::indexer::walk_vault(vault_path, &[], false)?;

    let mut rel_paths: HashSet<String> = HashSet::new();
    let mut by_stem: HashSet<String> = HashSet::new();
    for p in &files {
        if let Ok(r) = p.strip_prefix(vault_path) {
            let r = r.to_string_lossy().to_string();
            rel_paths.insert(r.trim_end_matches(".md").to_string());
            rel_paths.insert(r);
        }
        if let Some(stem) = p.file_stem() {
            by_stem.insert(stem.to_string_lossy().to_lowercase());
        }
    }

    let exists_on_disk = |target: &str| -> bool {
        let bare = target.trim_end_matches(".md");
        if rel_paths.contains(bare) || rel_paths.contains(target) {
            return true;
        }
        let basename = bare.rsplit('/').next().unwrap_or(bare);
        by_stem.contains(&basename.to_lowercase())
    };

    let (excluded, broken): (Vec<_>, Vec<_>) =
        links.into_iter().partition(|l| exists_on_disk(&l.target));
    Ok((broken, excluded))
}

/// Find notes that haven't been updated in the given number of days.
///
/// Stub — returns an empty vec for now. A full implementation would check
/// `mtime` or a `reviewed_at` frontmatter field.
pub fn find_stale_notes(_store: &Store, _days: u32) -> Result<Vec<String>> {
    Ok(Vec::new())
}

/// Generate a combined health report for the vault.
pub fn generate_health_report(store: &Store, config: &HealthConfig) -> Result<HealthReport> {
    let orphans = find_orphans(store, config)?;
    let unresolved = find_broken_links(store)?;

    // Ask the disk, not the index. Without a vault path we cannot ask, so we
    // keep every link in `broken_links` and flag that the split did not run
    // rather than emitting a confidently wrong empty `excluded_links`.
    // A failed walk must not take the whole report down: an unreadable vault
    // makes the link split unavailable, not the orphan counts or tag issues.
    // It degrades to the unreconciled view and says so, rather than 500ing or
    // silently claiming zero excluded links.
    let (broken_links, excluded_links, disk_reconciled) = match config.vault_path.as_deref() {
        Some(vault) => match reconcile_links_with_disk(unresolved.clone(), vault) {
            Ok((broken, excluded)) => (broken, excluded, true),
            Err(e) => {
                tracing::warn!("link disk-reconciliation failed (non-fatal): {e:#}");
                (unresolved, Vec::new(), false)
            }
        },
        None => (unresolved, Vec::new(), false),
    };

    let stale_notes = find_stale_notes(store, 90)?;

    // Inbox pending: files in the inbox folder.
    let inbox_pending = if let Some(ref inbox) = config.inbox_folder {
        store
            .find_files_by_prefix(&format!("{}%", inbox))?
            .into_iter()
            .map(|f| f.path)
            .collect()
    } else {
        Vec::new()
    };

    let all_files = store.get_all_files()?;
    let total_files = all_files.len();

    // Tag issues: find work notes missing required tags.
    let tag_issues = all_files
        .iter()
        .filter(|f| f.path.contains("Work/") || f.path.contains("01-Projects/Work/"))
        .filter(|f| !f.tags.iter().any(|t| t == "work"))
        .map(|f| TagIssue {
            file: f.path.clone(),
            issue: "work note missing 'work' tag".to_string(),
        })
        .collect();

    // Index age: seconds since the most recent indexed_at timestamp.
    let index_age_seconds = {
        let last = all_files
            .iter()
            .filter_map(|f| f.indexed_at.parse::<u64>().ok())
            .max()
            .unwrap_or(0);
        if last == 0 {
            0
        } else {
            use std::time::SystemTime;
            let now = SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            now.saturating_sub(last)
        }
    };

    Ok(HealthReport {
        orphans,
        broken_links,
        excluded_links,
        disk_reconciled,
        stale_notes,
        inbox_pending,
        tag_issues,
        index_age_seconds,
        total_files,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    /// Build a throwaway vault on disk. `files` are paths relative to the root.
    fn vault_with(files: &[&str]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for f in files {
            let p = dir.path().join(f);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(&p, "# note\n").unwrap();
        }
        dir
    }

    fn link(target: &str) -> BrokenLink {
        BrokenLink {
            source: "src.md".to_string(),
            target: target.to_string(),
        }
    }

    #[test]
    fn excluded_target_on_disk_is_not_broken() {
        // The whole defect in one test. `raw/` is excluded from indexing, so
        // this link can never resolve and stayed "broken" through 50+
        // reindexes. The file was there the entire time.
        let vault = vault_with(&["raw/paper.md", "notes/real.md"]);
        let (broken, excluded) =
            reconcile_links_with_disk(vec![link("raw/paper")], vault.path()).unwrap();

        assert!(broken.is_empty(), "target exists; nothing is broken");
        assert_eq!(excluded.len(), 1);
        assert_eq!(excluded[0].target, "raw/paper");
    }

    #[test]
    fn absent_target_is_genuinely_broken() {
        let vault = vault_with(&["notes/real.md"]);
        let (broken, excluded) =
            reconcile_links_with_disk(vec![link("notes/ghost")], vault.path()).unwrap();

        assert_eq!(broken.len(), 1, "nothing on disk matches");
        assert!(excluded.is_empty());
    }

    #[test]
    fn reconciliation_mirrors_the_indexers_matching_rules() {
        // If these drift apart, health calls a link broken that the indexer
        // would happily resolve. Covers: bare basename, explicit .md, and a
        // nested path referenced by basename only.
        let vault = vault_with(&["deep/nested/Target.md"]);
        for target in ["Target", "Target.md", "deep/nested/Target"] {
            let (broken, excluded) =
                reconcile_links_with_disk(vec![link(target)], vault.path()).unwrap();
            assert!(broken.is_empty(), "{target} should have matched on disk");
            assert_eq!(excluded.len(), 1, "{target}");
        }
    }

    #[test]
    fn basename_match_is_case_insensitive() {
        let vault = vault_with(&["Notes/MyNote.md"]);
        let (broken, _) = reconcile_links_with_disk(vec![link("mynote")], vault.path()).unwrap();
        assert!(
            broken.is_empty(),
            "indexer matches basenames case-insensitively"
        );
    }

    #[test]
    fn report_flags_when_the_split_did_not_run() {
        // Without a vault path the two classes cannot be separated. The report
        // must say so rather than emit an empty excluded_links that reads as
        // "there were none".
        let store = setup_health_store();
        store
            .insert_unresolved_link("note.md", "somewhere.md")
            .unwrap();
        let config = HealthConfig {
            daily_folder: None,
            inbox_folder: None,
            vault_path: None,
        };
        let report = generate_health_report(&store, &config).unwrap();

        assert!(!report.disk_reconciled, "must not claim it reconciled");
        assert!(report.excluded_links.is_empty());
        assert_eq!(
            report.broken_links.len(),
            1,
            "unpartitioned, not silently dropped"
        );
    }

    #[test]
    fn unreadable_vault_degrades_instead_of_failing() {
        let store = setup_health_store();
        store
            .insert_unresolved_link("note.md", "somewhere.md")
            .unwrap();
        let config = HealthConfig {
            daily_folder: None,
            inbox_folder: None,
            vault_path: Some(std::path::PathBuf::from("/nonexistent/vault/xyz")),
        };
        let report = generate_health_report(&store, &config).unwrap();

        assert!(!report.disk_reconciled);
        assert_eq!(report.broken_links.len(), 1, "report still produced");
    }

    fn setup_health_store() -> Store {
        let store = Store::open_memory().unwrap();
        // Insert files with edges to test orphan detection.
        let linked_id = store
            .insert_file("linked.md", "aaa111", 100, &[], "aaa111", None, None)
            .unwrap();
        let orphan_id = store
            .insert_file("orphan.md", "bbb222", 100, &[], "bbb222", None, None)
            .unwrap();
        let _daily_id = store
            .insert_file(
                "daily/2026-03-26.md",
                "ccc333",
                100,
                &[],
                "ccc333",
                None,
                None,
            )
            .unwrap();
        // Add edge: linked.md → orphan.md (both files are "connected")
        store.insert_edge(linked_id, orphan_id, "wikilink").unwrap();
        store
    }

    #[test]
    fn test_find_orphans_excludes_daily() {
        let store = setup_health_store();
        let config = HealthConfig {
            daily_folder: Some("daily/".to_string()),
            inbox_folder: None,
            vault_path: None,
        };
        let orphans = find_orphans(&store, &config).unwrap();
        // linked.md has outgoing edge, orphan.md has incoming edge — both connected.
        // daily note is excluded by prefix. Result should be empty.
        assert!(orphans.is_empty());
    }

    #[test]
    fn test_find_orphans_detects_isolated() {
        let store = Store::open_memory().unwrap();
        store
            .insert_file("connected.md", "h1", 100, &[], "d1", None, None)
            .unwrap();
        let iso_id = store
            .insert_file("island.md", "h2", 100, &[], "d2", None, None)
            .unwrap();
        let other_id = store
            .insert_file("other.md", "h3", 100, &[], "d3", None, None)
            .unwrap();
        store.insert_edge(iso_id, other_id, "wikilink").unwrap();

        let config = HealthConfig {
            daily_folder: None,
            inbox_folder: None,
            vault_path: None,
        };
        let orphans = find_orphans(&store, &config).unwrap();
        // connected.md has no edges at all — it's the orphan.
        assert_eq!(orphans.len(), 1);
        assert_eq!(orphans[0], "connected.md");
    }

    #[test]
    fn test_find_broken_links() {
        let store = setup_health_store();
        // Record an unresolved link (wikilink target that doesn't exist).
        store
            .insert_unresolved_link("linked.md", "nonexistent.md")
            .unwrap();
        let broken = find_broken_links(&store).unwrap();
        assert_eq!(broken.len(), 1);
        assert_eq!(broken[0].source, "linked.md");
        assert_eq!(broken[0].target, "nonexistent.md");
    }

    #[test]
    fn test_find_broken_links_empty_when_none() {
        let store = setup_health_store();
        let broken = find_broken_links(&store).unwrap();
        assert!(broken.is_empty());
    }

    #[test]
    fn test_generate_health_report() {
        let store = Store::open_memory().unwrap();
        store
            .insert_file("note.md", "h1", 100, &[], "d1", None, None)
            .unwrap();
        store
            .insert_file("00-Inbox/unsorted.md", "h2", 100, &[], "d2", None, None)
            .unwrap();
        store
            .insert_unresolved_link("note.md", "missing.md")
            .unwrap();

        let config = HealthConfig {
            daily_folder: Some("daily/".to_string()),
            inbox_folder: Some("00-Inbox/".to_string()),
            vault_path: None,
        };
        let report = generate_health_report(&store, &config).unwrap();
        assert_eq!(report.total_files, 2);
        // note.md has no edges and is not in daily/ or inbox/ — it's an orphan.
        assert_eq!(report.orphans.len(), 1);
        assert_eq!(report.orphans[0], "note.md");
        // One broken link recorded.
        assert_eq!(report.broken_links.len(), 1);
        // One file in inbox.
        assert_eq!(report.inbox_pending.len(), 1);
        assert_eq!(report.inbox_pending[0], "00-Inbox/unsorted.md");
    }
}
