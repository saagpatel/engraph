use std::collections::{HashMap, HashSet, hash_map::Entry};

use anyhow::Result;

use crate::fusion::RankedResult;
use crate::store::Store;

/// Is this line a fence marker? Returns `(char, run_length)`.
///
/// CommonMark allows up to three leading spaces and a run of three or more
/// backticks or tildes.
fn fence_marker(trimmed: &str) -> Option<(char, usize)> {
    let c = trimmed.chars().next()?;
    if c != '`' && c != '~' {
        return None;
    }
    let n = trimmed.chars().take_while(|&x| x == c).count();
    if n >= 3 { Some((c, n)) } else { None }
}

/// Blank out inline code spans so their contents cannot be read as links.
///
/// Replaces span contents with spaces rather than deleting them, so byte
/// offsets are unchanged and the caller can scan the result normally. Only
/// single-backtick delimiters are handled; a ``span containing ` a backtick``
/// is rare enough in a vault that the extra machinery is not worth it, and
/// mishandling it can only under-suppress, never invent a link.
fn blank_inline_code(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut in_code = false;
    for ch in line.chars() {
        if ch == '`' {
            in_code = !in_code;
            out.push(' ');
        } else if in_code {
            out.extend(std::iter::repeat_n(' ', ch.len_utf8()));
        } else {
            out.push(ch);
        }
    }
    out
}

/// Extract unique wikilink targets from markdown, ignoring code.
///
/// Handles `[[Target]]`, `[[Target|Display]]`, `[[Target#Heading]]`, and skips
/// embeds (`![[...]]`).
///
/// Text inside fenced code blocks and inline code spans is NOT scanned. A
/// `[[link]]` in a code fence is a code sample, not a link, and treating it as
/// one manufactures broken-link reports that can never be cleared: the target
/// was never meant to exist. In this operator's vault, notes that *document*
/// wikilink syntax produced targets like `wiki/lessons/foo` and the bare word
/// `wikilink`, which no amount of reindexing could resolve.
pub fn extract_wikilink_targets(text: &str) -> Vec<String> {
    let mut targets = Vec::new();
    let mut seen = HashSet::new();
    let mut fence: Option<(char, usize)> = None;

    for raw_line in text.lines() {
        let trimmed = raw_line.trim_start();
        // Only up to three leading spaces may precede a fence.
        let indent = raw_line.len() - trimmed.len();
        if indent <= 3
            && let Some((c, n)) = fence_marker(trimmed)
        {
            match fence {
                None => fence = Some((c, n)),
                // A closing fence matches the opener's char and is at least
                // as long. An info string is only legal on the opener.
                Some((oc, on)) if c == oc && n >= on => fence = None,
                Some(_) => {}
            }
            continue;
        }
        if fence.is_some() {
            continue;
        }
        scan_line_for_links(&blank_inline_code(raw_line), &mut targets, &mut seen);
    }

    targets
}

fn scan_line_for_links(text: &str, targets: &mut Vec<String>, seen: &mut HashSet<String>) {
    let bytes = text.as_bytes();
    let mut i = 0;

    while i + 1 < bytes.len() {
        if bytes[i] == b'[' && bytes[i + 1] == b'[' {
            // Check for embed prefix (! before [[)
            let is_embed = i > 0 && bytes[i - 1] == b'!';
            if let Some(rest) = text.get(i + 2..)
                && let Some(close) = rest.find("]]")
            {
                let inner = &rest[..close];
                if !is_embed && !inner.is_empty() && !inner.contains('\n') {
                    // Obsidian escapes the alias pipe as `\|` inside tables;
                    // unescape it so the `|` separator is recognized.
                    let inner = inner.replace("\\|", "|");
                    // Strip heading: [[Note#Section]] → "Note"
                    let target = inner.split('#').next().unwrap_or(inner.as_str());
                    // Strip display: [[Note|Display]] → "Note"
                    let target = target.split('|').next().unwrap_or(target);
                    let target = target.trim().to_string();
                    if !target.is_empty() && seen.insert(target.clone()) {
                        targets.push(target);
                    }
                }
                i += 2 + close + 2;
                continue;
            }
        }
        i += 1;
    }
}

/// Extract query terms for relevance filtering.
/// Splits on whitespace, lowercases, drops terms shorter than 3 chars.
pub fn extract_query_terms(query: &str) -> Vec<String> {
    query
        .split_whitespace()
        .map(|t| t.to_lowercase())
        .filter(|t| t.len() >= 3)
        .collect()
}

/// Expand search results by following graph connections.
/// Seeds are the top results from semantic + FTS lanes.
/// Returns expanded results suitable for RRF fusion.
pub fn graph_expand(
    store: &Store,
    seeds: &[RankedResult],
    query: &str,
    max_hops: usize,
    max_expansions: usize,
) -> Result<Vec<RankedResult>> {
    let query_terms = extract_query_terms(query);
    let seed_ids: HashSet<i64> = seeds.iter().map(|s| s.file_id).collect();

    // Track best score per expanded file (multi-parent merge: take highest)
    // (file_id) → (best_score, hop_depth, seed_file_path)
    let mut expansions: HashMap<i64, (f64, usize, String)> = HashMap::new();

    for seed in seeds {
        let neighbors = store.get_neighbors(seed.file_id, max_hops)?;

        for (neighbor_id, hop) in neighbors {
            if seed_ids.contains(&neighbor_id) {
                continue;
            }

            let decay = match hop {
                1 => 0.8,
                2 => 0.5,
                _ => 0.3,
            };
            let mut expansion_score = seed.score * decay;

            // Relevance filter: must match a query term via FTS or share tags
            let term_match = query_terms
                .iter()
                .any(|t| store.file_contains_term(neighbor_id, t).unwrap_or(false));

            if !term_match {
                let shared = store
                    .get_shared_tags_files(neighbor_id, 100)
                    .unwrap_or_default();
                if shared.contains(&seed.file_id) {
                    expansion_score *= 0.7;
                } else {
                    continue; // tangential — skip
                }
            }

            // Multi-parent merge: keep highest score
            match expansions.entry(neighbor_id) {
                Entry::Occupied(mut e) => {
                    if expansion_score > e.get().0 {
                        e.insert((expansion_score, hop, seed.file_path.clone()));
                    }
                }
                Entry::Vacant(e) => {
                    e.insert((expansion_score, hop, seed.file_path.clone()));
                }
            }
        }
    }

    // Sort by score descending, cap at max_expansions
    let mut results: Vec<(i64, f64, usize, String)> = expansions
        .into_iter()
        .map(|(fid, (score, hop, seed))| (fid, score, hop, seed))
        .collect();
    results.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });
    results.truncate(max_expansions);

    // Convert to RankedResult
    let mut ranked = Vec::new();
    for (file_id, score, _hop, _seed) in results {
        let file = store.get_file_by_id(file_id)?;
        let (file_path, docid) = match file {
            Some(f) => (f.path, f.docid),
            None => continue,
        };
        let (heading, snippet) = store
            .get_best_chunk_for_file(file_id)?
            .unwrap_or_else(|| (String::new(), String::new()));
        let heading = if heading.is_empty() {
            None
        } else {
            Some(heading)
        };

        ranked.push(RankedResult {
            file_path,
            file_id,
            score,
            heading,
            snippet,
            docid,
        });
    }

    Ok(ranked)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::docid::generate_docid;
    use crate::fusion::RankedResult;
    use crate::store::Store;

    #[test]
    fn test_extract_wikilink_targets() {
        let text =
            "See [[Note One]] and [[Note Two|display]] for details. Also [[Note One]] again.";
        let targets = extract_wikilink_targets(text);
        assert!(targets.contains(&"Note One".to_string()));
        assert!(targets.contains(&"Note Two".to_string()));
        assert_eq!(targets.len(), 2); // deduplicated
    }

    #[test]
    fn test_extract_wikilinks_with_headings() {
        let text = "Link to [[Note#Section]] here.";
        let targets = extract_wikilink_targets(text);
        assert_eq!(targets, vec!["Note"]);
    }

    #[test]
    fn test_extract_wikilinks_empty() {
        assert!(extract_wikilink_targets("no links here").is_empty());
        assert!(extract_wikilink_targets("").is_empty());
    }

    #[test]
    fn test_extract_wikilinks_skip_embeds() {
        let text = "![[embedded image.png]] and [[real link]]";
        let targets = extract_wikilink_targets(text);
        assert_eq!(targets, vec!["real link"]);
    }

    #[test]
    fn test_extract_wikilinks_heading_and_display() {
        let text = "[[Note#Section|Custom Display]]";
        let targets = extract_wikilink_targets(text);
        assert_eq!(targets, vec!["Note"]); // strip both heading and display
    }

    #[test]
    fn fenced_code_is_not_scanned_for_links() {
        // The real shape from the vault: an audit note documenting wikilink
        // syntax. Every target below was reported as a permanently broken
        // link that no reindex could clear, because none was ever a link.
        let text = "\
Wikilinks look like this:

```
[[wiki/lessons/foo]]
[[wiki/ops/X]]
```

But [[Real Note]] is a link.
";
        let targets = extract_wikilink_targets(text);
        assert_eq!(targets, vec!["Real Note"]);
    }

    #[test]
    fn inline_code_is_not_scanned_for_links() {
        let text = "Use `[[wikilink]]` syntax, and see [[Real Note]].";
        let targets = extract_wikilink_targets(text);
        assert_eq!(targets, vec!["Real Note"]);
    }

    #[test]
    fn tilde_fences_and_info_strings_are_handled() {
        let text = "\
~~~markdown
[[inside tildes]]
~~~
```rust
// [[inside rust fence]]
```
[[outside]]
";
        assert_eq!(extract_wikilink_targets(text), vec!["outside"]);
    }

    #[test]
    fn a_longer_closing_fence_closes_and_a_shorter_one_does_not() {
        // CommonMark: the closer must use the same char and be at least as
        // long as the opener. Getting this wrong silently swallows the rest
        // of a document, which would suppress real links.
        let text = "\
````
[[hidden]]
```
[[still hidden]]
````
[[visible]]
";
        assert_eq!(extract_wikilink_targets(text), vec!["visible"]);
    }

    #[test]
    fn four_space_indent_is_not_a_fence_marker() {
        // Only up to three leading spaces may introduce a fence. Treating a
        // deeper indent as one would open a fence that never closes and drop
        // every subsequent link.
        let text = "    ``` not a fence\n[[still a link]]\n";
        assert_eq!(extract_wikilink_targets(text), vec!["still a link"]);
    }

    #[test]
    fn suppression_does_not_leak_past_a_closed_fence() {
        // Guard against over-suppression: the whole risk of this change is
        // silently dropping real links.
        let text = "[[before]]\n```\n[[during]]\n```\n[[after]]\n";
        assert_eq!(extract_wikilink_targets(text), vec!["before", "after"]);
    }

    #[test]
    fn backtick_blanking_preserves_links_later_on_the_line() {
        let text = "Run `engraph search` then open [[Results]].";
        assert_eq!(extract_wikilink_targets(text), vec!["Results"]);
    }

    #[test]
    fn test_extract_wikilinks_escaped_pipe_in_table() {
        // Obsidian escapes the alias pipe as `\|` inside tables; the target
        // must still resolve to the note name, not "Name\".
        let text = "| [[Page Name\\|Page]] | done |";
        let targets = extract_wikilink_targets(text);
        assert_eq!(targets, vec!["Page Name"]);
    }

    #[test]
    fn test_extract_query_terms() {
        let terms = extract_query_terms("BRE-2579 delivery date");
        assert_eq!(terms, vec!["bre-2579", "delivery", "date"]);
    }

    #[test]
    fn test_extract_query_terms_short_words_dropped() {
        let terms = extract_query_terms("a is the big query");
        assert_eq!(terms, vec!["the", "big", "query"]);
    }

    #[test]
    fn test_graph_expand_basic() {
        let store = Store::open_memory().unwrap();
        let f1 = store
            .insert_file(
                "seed.md",
                "h1",
                100,
                &["rust".into()],
                &generate_docid("seed.md"),
                None,
                None,
            )
            .unwrap();
        let f2 = store
            .insert_file(
                "linked.md",
                "h2",
                100,
                &["rust".into()],
                &generate_docid("linked.md"),
                None,
                None,
            )
            .unwrap();
        let _f3 = store
            .insert_file(
                "unlinked.md",
                "h3",
                100,
                &[],
                &generate_docid("unlinked.md"),
                None,
                None,
            )
            .unwrap();

        store.insert_edge(f1, f2, "wikilink").unwrap();
        store
            .insert_chunk(f2, "## Linked", "Linked content about delivery", 10, 20)
            .unwrap();
        store
            .insert_fts_chunk(f2, 0, "Linked content about delivery")
            .unwrap();

        let seeds = vec![RankedResult {
            file_path: "seed.md".into(),
            file_id: f1,
            score: 0.85,
            heading: None,
            snippet: "Seed".into(),
            docid: None,
        }];

        let expanded = graph_expand(&store, &seeds, "delivery", 2, 20).unwrap();
        assert_eq!(expanded.len(), 1);
        assert_eq!(expanded[0].file_path, "linked.md");
        assert!(expanded[0].score > 0.0 && expanded[0].score < 0.85);
    }

    #[test]
    fn test_graph_expand_skips_seeds() {
        let store = Store::open_memory().unwrap();
        let f1 = store
            .insert_file("a.md", "h1", 100, &[], &generate_docid("a.md"), None, None)
            .unwrap();
        let f2 = store
            .insert_file("b.md", "h2", 100, &[], &generate_docid("b.md"), None, None)
            .unwrap();

        store.insert_edge(f1, f2, "wikilink").unwrap();
        store.insert_chunk(f2, "## B", "Content B", 10, 20).unwrap();
        store.insert_fts_chunk(f2, 0, "Content B").unwrap();

        let seeds = vec![
            RankedResult {
                file_path: "a.md".into(),
                file_id: f1,
                score: 0.9,
                heading: None,
                snippet: "A".into(),
                docid: None,
            },
            RankedResult {
                file_path: "b.md".into(),
                file_id: f2,
                score: 0.8,
                heading: None,
                snippet: "B".into(),
                docid: None,
            },
        ];

        let expanded = graph_expand(&store, &seeds, "content", 2, 20).unwrap();
        assert!(expanded.is_empty());
    }

    #[test]
    fn test_graph_expand_multi_parent_takes_highest() {
        let store = Store::open_memory().unwrap();
        let f1 = store
            .insert_file("a.md", "h1", 100, &[], &generate_docid("a.md"), None, None)
            .unwrap();
        let f2 = store
            .insert_file("b.md", "h2", 100, &[], &generate_docid("b.md"), None, None)
            .unwrap();
        let f3 = store
            .insert_file(
                "shared.md",
                "h3",
                100,
                &[],
                &generate_docid("shared.md"),
                None,
                None,
            )
            .unwrap();

        store.insert_edge(f1, f3, "wikilink").unwrap();
        store.insert_edge(f2, f3, "wikilink").unwrap();
        store
            .insert_chunk(f3, "## Shared", "Shared topic content", 10, 20)
            .unwrap();
        store
            .insert_fts_chunk(f3, 0, "Shared topic content")
            .unwrap();

        let seeds = vec![
            RankedResult {
                file_path: "a.md".into(),
                file_id: f1,
                score: 0.9,
                heading: None,
                snippet: "A".into(),
                docid: None,
            },
            RankedResult {
                file_path: "b.md".into(),
                file_id: f2,
                score: 0.5,
                heading: None,
                snippet: "B".into(),
                docid: None,
            },
        ];

        let expanded = graph_expand(&store, &seeds, "topic", 1, 20).unwrap();
        assert_eq!(expanded.len(), 1);
        assert_eq!(expanded[0].file_path, "shared.md");
        // Should use highest parent: 0.9 * 0.8 = 0.72
        assert!((expanded[0].score - 0.72).abs() < 0.01);
    }

    #[test]
    fn test_graph_expand_empty_graph() {
        let store = Store::open_memory().unwrap();
        let f1 = store
            .insert_file("a.md", "h1", 100, &[], "aaa111", None, None)
            .unwrap();

        let seeds = vec![RankedResult {
            file_path: "a.md".into(),
            file_id: f1,
            score: 0.9,
            heading: None,
            snippet: "A".into(),
            docid: None,
        }];

        let expanded = graph_expand(&store, &seeds, "query", 2, 20).unwrap();
        assert!(expanded.is_empty());
    }

    #[test]
    fn test_graph_expand_tag_fallback() {
        let store = Store::open_memory().unwrap();
        let f1 = store
            .insert_file(
                "seed.md",
                "h1",
                100,
                &["rust".into(), "cli".into()],
                &generate_docid("seed.md"),
                None,
                None,
            )
            .unwrap();
        let f2 = store
            .insert_file(
                "linked.md",
                "h2",
                100,
                &["rust".into()],
                &generate_docid("linked.md"),
                None,
                None,
            )
            .unwrap();

        store.insert_edge(f1, f2, "wikilink").unwrap();
        store
            .insert_chunk(f2, "## Linked", "Unrelated content", 10, 20)
            .unwrap();
        store
            .insert_fts_chunk(f2, 0, "Unrelated content here")
            .unwrap();

        let seeds = vec![RankedResult {
            file_path: "seed.md".into(),
            file_id: f1,
            score: 0.85,
            heading: None,
            snippet: "Seed".into(),
            docid: None,
        }];

        // Query doesn't match FTS, but shared tag "rust" should keep it (with 0.7x penalty)
        let expanded = graph_expand(&store, &seeds, "nonexistent query term", 2, 20).unwrap();
        assert_eq!(expanded.len(), 1);
        // Score: 0.85 * 0.8 * 0.7 = 0.476
        assert!((expanded[0].score - 0.476).abs() < 0.01);
    }

    #[test]
    fn test_graph_expand_follows_backlinks() {
        let store = Store::open_memory().unwrap();
        let seed = store
            .insert_file(
                "seed.md",
                "h1",
                100,
                &[],
                &generate_docid("seed.md"),
                None,
                None,
            )
            .unwrap();
        let backlinker = store
            .insert_file(
                "backlink.md",
                "h2",
                100,
                &[],
                &generate_docid("backlink.md"),
                None,
                None,
            )
            .unwrap();

        // backlink.md links TO seed.md; seed.md has no outgoing links.
        store.insert_edge(backlinker, seed, "wikilink").unwrap();
        store
            .insert_chunk(
                backlinker,
                "## Backlink",
                "Backlink content about delivery",
                10,
                20,
            )
            .unwrap();
        store
            .insert_fts_chunk(backlinker, 0, "Backlink content about delivery")
            .unwrap();

        let seeds = vec![RankedResult {
            file_path: "seed.md".into(),
            file_id: seed,
            score: 0.85,
            heading: None,
            snippet: "Seed".into(),
            docid: None,
        }];

        // Graph expansion is undirected: a note that links INTO the seed and
        // matches the query is surfaced as an expansion.
        let expanded = graph_expand(&store, &seeds, "delivery", 2, 20).unwrap();
        assert_eq!(expanded.len(), 1);
        assert_eq!(expanded[0].file_path, "backlink.md");
    }
}
