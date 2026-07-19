/// Reciprocal Rank Fusion (RRF) engine.
///
/// Merges ranked results from multiple search lanes (e.g. semantic vector
/// and FTS5 keyword search) into a single ranked list using the RRF formula:
///
///   rrf_score = sum( weight_i / (k + rank_i) )
///
/// A ranked result from a single search lane.
#[derive(Clone)]
pub struct RankedResult {
    pub file_path: String,
    pub file_id: i64,
    pub score: f64,
    pub heading: Option<String>,
    pub snippet: String,
    pub docid: Option<String>,
}

/// A fused result after RRF merging across lanes.
pub struct FusedResult {
    pub file_path: String,
    pub file_id: i64,
    pub rrf_score: f64,
    pub heading: Option<String>,
    pub snippet: String,
    pub docid: Option<String>,
    pub lane_contributions: Vec<LaneContribution>,
    /// This result's RRF score as a percentage of the top result's.
    ///
    /// The top hit is **always 100 by construction**, however bad it is. This
    /// ranks; it does not measure. It was previously called `confidence`, which
    /// read as a quality judgement to every caller deciding whether to trust a
    /// result. Renamed rather than redefined: reusing the old name with new
    /// semantics would have broken consumers silently.
    pub relative_score: f64,
    /// Distinct lanes that ranked this result (not the length of
    /// `lane_contributions`, which can hold several entries from one lane when
    /// that lane returns multiple chunks of the same file).
    pub lanes_hit: usize,
    /// Lanes that returned at least one result for this query, i.e. the lanes
    /// that had an opinion to offer. Carried so the agreement ratio is
    /// checkable instead of asserted.
    pub lanes_ran: usize,
}

impl FusedResult {
    /// Corroboration: the fraction of participating lanes that found this
    /// result. A hit surfaced by semantic, FTS and graph is better evidenced
    /// than one only the graph straggler produced.
    ///
    /// The denominator is lanes that *returned something*, not the number of
    /// lanes configured. A lane that ran and matched nothing offered no
    /// evidence either way, and counting it would systematically depress every
    /// score whenever intelligence is off and the reranker lane is absent.
    pub fn lane_agreement(&self) -> f64 {
        if self.lanes_ran == 0 {
            return 0.0;
        }
        self.lanes_hit as f64 / self.lanes_ran as f64
    }
}

/// Per-lane contribution details for --explain output.
pub struct LaneContribution {
    pub lane_name: String,
    pub rank: usize,
    pub raw_score: f64,
    pub weighted_contribution: f64,
    pub detail: Option<String>, // e.g., "1-hop from BRE-2579"
}

use std::collections::HashMap;

/// Fuse ranked results from multiple search lanes using Reciprocal Rank Fusion.
///
/// Each lane is a tuple of `(lane_name, results, weight)`.
/// Results are grouped by `file_path` (file-level deduplication).
/// The best snippet/heading per file is kept from the highest-ranked lane.
///
/// `k` is the RRF constant (typically 60).
pub fn rrf_fuse(lanes: &[(&str, &[RankedResult], f64)], k: usize) -> Vec<FusedResult> {
    // Track per-file: rrf_score, best snippet info, lane contributions
    struct Accumulator {
        file_path: String,
        file_id: i64,
        rrf_score: f64,
        heading: Option<String>,
        snippet: String,
        docid: Option<String>,
        best_rank: usize, // lowest rank seen (for picking best snippet)
        lane_contributions: Vec<LaneContribution>,
    }

    let mut acc_map: HashMap<String, Accumulator> = HashMap::new();

    for &(lane_name, results, weight) in lanes {
        for (idx, r) in results.iter().enumerate() {
            let rank = idx + 1; // 1-based
            let contribution = weight / (k as f64 + rank as f64);

            let acc = acc_map
                .entry(r.file_path.clone())
                .or_insert_with(|| Accumulator {
                    file_path: r.file_path.clone(),
                    file_id: r.file_id,
                    rrf_score: 0.0,
                    heading: r.heading.clone(),
                    snippet: r.snippet.clone(),
                    docid: r.docid.clone(),
                    best_rank: rank,
                    lane_contributions: Vec::new(),
                });

            acc.rrf_score += contribution;

            // Keep snippet from the best-ranked appearance
            if rank < acc.best_rank {
                acc.best_rank = rank;
                acc.heading = r.heading.clone();
                acc.snippet = r.snippet.clone();
                if r.docid.is_some() {
                    acc.docid = r.docid.clone();
                }
            }

            acc.lane_contributions.push(LaneContribution {
                lane_name: lane_name.to_string(),
                rank,
                raw_score: r.score,
                weighted_contribution: contribution,
                detail: None,
            });
        }
    }

    // Lanes that had an opinion to offer. A configured lane that matched
    // nothing is not evidence against a result, so it stays out of the
    // agreement denominator.
    let lanes_ran = lanes.iter().filter(|(_, r, _)| !r.is_empty()).count();

    let mut results: Vec<FusedResult> = acc_map
        .into_values()
        .map(|a| FusedResult {
            file_path: a.file_path,
            file_id: a.file_id,
            rrf_score: a.rrf_score,
            heading: a.heading,
            snippet: a.snippet,
            docid: a.docid,
            lanes_hit: a
                .lane_contributions
                .iter()
                .map(|lc| lc.lane_name.as_str())
                .collect::<std::collections::BTreeSet<_>>()
                .len(),
            lane_contributions: a.lane_contributions,
            relative_score: 0.0,
            lanes_ran,
        })
        .collect();

    // Sort by rrf_score descending; tiebreak on file_path so equal scores
    // rank deterministically (acc_map iteration order is random per process).
    results.sort_by(|a, b| {
        b.rrf_score
            .partial_cmp(&a.rrf_score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.file_path.cmp(&b.file_path))
    });

    // Express each score relative to the top one. Note what this is and is
    // not: the leader is 100 by definition, so this orders results and says
    // nothing about whether any of them are any good.
    let max_score = results.first().map(|r| r.rrf_score).unwrap_or(1.0);
    for r in &mut results {
        r.relative_score = if max_score > 0.0 {
            (r.rrf_score / max_score) * 100.0
        } else {
            0.0
        };
    }

    results
}

/// Format explain output for a single fused result.
pub fn format_explain(result: &FusedResult) -> String {
    let mut out = format!("  RRF: {:.4}\n", result.rrf_score);
    for lc in &result.lane_contributions {
        let detail_str = lc
            .detail
            .as_deref()
            .map(|d| format!(" ({})", d))
            .unwrap_or_default();
        out += &format!(
            "    {}: rank #{}, raw {:.2}{}, +{:.4}\n",
            lc.lane_name, lc.rank, lc.raw_score, detail_str, lc.weighted_contribution
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_result(file_path: &str, score: f64) -> RankedResult {
        RankedResult {
            file_path: file_path.to_string(),
            file_id: 0,
            score,
            heading: Some(format!("heading for {}", file_path)),
            snippet: format!("snippet for {}", file_path),
            docid: None,
        }
    }

    #[test]
    fn test_rrf_basic() {
        // Item appearing in both lanes should rank highest
        let semantic = vec![
            make_result("both.md", 0.87),
            make_result("sem_only.md", 0.75),
        ];
        let fts = vec![make_result("fts_only.md", 5.0), make_result("both.md", 3.2)];

        let fused = rrf_fuse(&[("semantic", &semantic, 1.0), ("fts", &fts, 1.0)], 60);

        assert_eq!(fused.len(), 3);
        // "both.md" should be first because it appears in both lanes
        assert_eq!(fused[0].file_path, "both.md");

        // Verify the RRF score for "both.md":
        // semantic rank 1: 1.0 / (60 + 1) = 0.01639...
        // fts rank 2: 1.0 / (60 + 2) = 0.01613...
        // total = 0.03252...
        let expected = 1.0 / 61.0 + 1.0 / 62.0;
        assert!((fused[0].rrf_score - expected).abs() < 1e-10);

        // Both single-lane items should have lower scores
        assert!(fused[0].rrf_score > fused[1].rrf_score);
        assert!(fused[0].rrf_score > fused[2].rrf_score);

        // "both.md" should have 2 lane contributions
        assert_eq!(fused[0].lane_contributions.len(), 2);
    }

    #[test]
    fn test_lane_agreement_counts_distinct_lanes_not_contributions() {
        // A lane returning two chunks of the SAME file pushes two
        // contributions. That is one lane agreeing with itself, not two lanes
        // corroborating each other, and conflating them would let a single
        // lane manufacture full agreement on its own.
        let semantic = vec![make_result("dup.md", 0.9), make_result("dup.md", 0.8)];
        let fts = vec![make_result("dup.md", 5.0)];

        let fused = rrf_fuse(&[("semantic", &semantic, 1.0), ("fts", &fts, 1.0)], 60);

        assert_eq!(fused[0].file_path, "dup.md");
        assert_eq!(
            fused[0].lane_contributions.len(),
            3,
            "three raw contributions"
        );
        assert_eq!(fused[0].lanes_hit, 2, "but only two DISTINCT lanes");
        assert_eq!(fused[0].lanes_ran, 2);
        assert!((fused[0].lane_agreement() - 1.0).abs() < 1e-10);
    }

    #[test]
    fn test_lane_agreement_denominator_excludes_silent_lanes() {
        // A lane that ran and matched nothing is not evidence against a
        // result. Counting it would depress every score whenever a lane is
        // absent -- e.g. the reranker with intelligence off, which is the
        // normal configuration on this machine.
        let semantic = vec![make_result("a.md", 0.9)];
        let empty: Vec<RankedResult> = vec![];

        let fused = rrf_fuse(
            &[
                ("semantic", &semantic, 1.0),
                ("fts", &empty, 1.0),
                ("graph", &empty, 1.0),
            ],
            60,
        );

        assert_eq!(fused[0].lanes_ran, 1, "only one lane had an opinion");
        assert_eq!(fused[0].lanes_hit, 1);
        assert!((fused[0].lane_agreement() - 1.0).abs() < 1e-10);
    }

    #[test]
    fn test_relative_score_ranks_but_agreement_discriminates() {
        // The point of the whole change. The top hit's relative_score is 100
        // by construction whether it is corroborated or not, so agreement is
        // the field that carries information about trustworthiness.
        let semantic = vec![make_result("lonely.md", 0.9)];
        let fts = vec![make_result("corroborated.md", 9.9)];
        let graph = vec![make_result("corroborated.md", 9.9)];

        let fused = rrf_fuse(
            &[
                ("semantic", &semantic, 1.0),
                ("fts", &fts, 1.0),
                ("graph", &graph, 1.0),
            ],
            60,
        );

        let top = &fused[0];
        let other = &fused[1];
        assert_eq!(top.file_path, "corroborated.md");
        assert!(
            (top.relative_score - 100.0).abs() < 1e-10,
            "leader is always 100"
        );

        // Agreement separates them where relative_score cannot.
        assert_eq!(top.lanes_hit, 2);
        assert_eq!(other.lanes_hit, 1);
        assert!(top.lane_agreement() > other.lane_agreement());
    }

    #[test]
    fn test_lane_agreement_is_zero_when_nothing_ran() {
        let fused = rrf_fuse(&[], 60);
        assert!(fused.is_empty());
        // And the guard holds for a hand-built result with no lanes.
        let r = FusedResult {
            file_path: "x.md".into(),
            file_id: 1,
            rrf_score: 0.0,
            heading: None,
            snippet: String::new(),
            docid: None,
            lane_contributions: Vec::new(),
            relative_score: 0.0,
            lanes_hit: 0,
            lanes_ran: 0,
        };
        assert_eq!(r.lane_agreement(), 0.0, "no divide-by-zero");
    }

    #[test]
    fn test_rrf_weighted() {
        // FTS weighted 3x should make FTS-only item win over semantic-only item
        let semantic = vec![make_result("sem.md", 0.95)];
        let fts = vec![make_result("fts.md", 8.0)];

        let fused = rrf_fuse(&[("semantic", &semantic, 1.0), ("fts", &fts, 3.0)], 60);

        assert_eq!(fused.len(), 2);
        // FTS item at rank 1 with weight 3.0: 3.0 / 61 = 0.04918...
        // Semantic item at rank 1 with weight 1.0: 1.0 / 61 = 0.01639...
        assert_eq!(fused[0].file_path, "fts.md");
        assert_eq!(fused[1].file_path, "sem.md");

        let fts_expected = 3.0 / 61.0;
        let sem_expected = 1.0 / 61.0;
        assert!((fused[0].rrf_score - fts_expected).abs() < 1e-10);
        assert!((fused[1].rrf_score - sem_expected).abs() < 1e-10);
    }

    #[test]
    fn test_rrf_single_lane() {
        let semantic = vec![
            make_result("a.md", 0.9),
            make_result("b.md", 0.8),
            make_result("c.md", 0.7),
        ];

        let fused = rrf_fuse(&[("semantic", &semantic, 1.0)], 60);

        assert_eq!(fused.len(), 3);
        assert_eq!(fused[0].file_path, "a.md");
        assert_eq!(fused[1].file_path, "b.md");
        assert_eq!(fused[2].file_path, "c.md");

        // Each should have exactly 1 lane contribution
        for f in &fused {
            assert_eq!(f.lane_contributions.len(), 1);
            assert_eq!(f.lane_contributions[0].lane_name, "semantic");
        }
    }

    #[test]
    fn test_format_explain() {
        let result = FusedResult {
            file_path: "test.md".to_string(),
            file_id: 1,
            rrf_score: 0.0328,
            heading: None,
            snippet: "test".to_string(),
            docid: None,
            relative_score: 100.0,
            lanes_hit: 1,
            lanes_ran: 1,
            lane_contributions: vec![
                LaneContribution {
                    lane_name: "semantic".to_string(),
                    rank: 1,
                    raw_score: 0.87,
                    weighted_contribution: 0.0164,
                    detail: None,
                },
                LaneContribution {
                    lane_name: "fts".to_string(),
                    rank: 3,
                    raw_score: 5.23,
                    weighted_contribution: 0.0159,
                    detail: None,
                },
            ],
        };

        let output = format_explain(&result);
        assert!(output.contains("RRF: 0.0328"));
        assert!(output.contains("semantic: rank #1, raw 0.87, +0.0164"));
        assert!(output.contains("fts: rank #3, raw 5.23, +0.0159"));
    }

    #[test]
    fn test_rrf_empty_lanes() {
        let fused = rrf_fuse(&[], 60);
        assert!(fused.is_empty());
    }

    #[test]
    fn test_rrf_empty_results() {
        let empty: Vec<RankedResult> = vec![];
        let fused = rrf_fuse(&[("semantic", &empty, 1.0), ("fts", &empty, 1.0)], 60);
        assert!(fused.is_empty());
    }
}
