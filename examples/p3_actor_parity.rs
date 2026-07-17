//! P3 acceptance: is `EmbedActor` bit-identical to `LlamaEmbed`, and does it
//! deliver the speedup 11-perf-claims-audit.md only *inferred*?
//!
//! The audit put P3's ceiling at ~1.8× on the query path (~6.8ms reused vs
//! 12.3ms fresh) by reasoning from shared-context batch encodes. That was a
//! BOUND, not a measurement. This is the measurement.
//!
//! Parity is checked first and is the gate: a faster embedder that returns
//! different vectors is not an optimization, it is a silent index corruption.
//! Both the document path (`embed_batch`, format_document) and the query path
//! (`embed_one`, format_query) are compared, since the actor routes them
//! through separate request variants and a mix-up would only show on one.
//!
//! Run in RELEASE.
//!
//! MODES EXIST BECAUSE TIMING BOTH ARMS IN ONE PROCESS IS INVALID. Holding a
//! `LlamaEmbed` and an `EmbedActor` at once means two resident models competing
//! for one GPU; measured that way, both arms inflate from ~12ms to ~90ms — which
//! is precisely P4's unexplained 91ms (03-perf-proposals.md). Contention is what
//! P3 exists to remove, so measuring it under self-inflicted contention would
//! bury the effect. Parity is immune (same inputs, same math), so that mode
//! loads both; timing modes load exactly one.
//!
//! Usage: p3_actor_parity <models_dir> parity      both models, correctness gates
//!        p3_actor_parity <models_dir> time-base   LlamaEmbed alone
//!        p3_actor_parity <models_dir> time-actor  EmbedActor alone
//!   T1_PASSES=<n>  timing repeats (default 5)

use std::path::PathBuf;
use std::time::Instant;

use engraph::llm::{EmbedActor, EmbedModel, LlamaEmbed};

fn texts(n: usize, words: usize) -> Vec<String> {
    const POOL: &[&str] = &[
        "retrieval",
        "pipeline",
        "wikilink",
        "graph",
        "vault",
        "embedding",
        "chunk",
        "reciprocal",
        "rank",
        "fusion",
        "traversal",
        "index",
        "semantic",
        "lexical",
        "hybrid",
        "query",
        "the",
        "a",
        "of",
        "and",
        "that",
        "which",
        "into",
        "across",
    ];
    (0..n)
        .map(|i| {
            let mut s = format!("Note {i}. ");
            for w in 0..words {
                s.push_str(POOL[(w + i * 7) % POOL.len()]);
                s.push(if w % 12 == 11 { '.' } else { ' ' });
            }
            s
        })
        .collect()
}

const QUERIES: &[&str] = &[
    "how does the retrieval pipeline rank candidates",
    "wikilink graph traversal",
    "what is reciprocal rank fusion",
];

fn exact_max_abs_diff(a: &[Vec<f32>], b: &[Vec<f32>]) -> anyhow::Result<f32> {
    anyhow::ensure!(a.len() == b.len(), "vector count mismatch");
    let mut max_diff = 0.0f32;
    for (index, (left, right)) in a.iter().zip(b).enumerate() {
        anyhow::ensure!(
            left.len() == right.len(),
            "dimension mismatch at vector {index}"
        );
        for (dimension, (&p, &q)) in left.iter().zip(right).enumerate() {
            anyhow::ensure!(
                p.is_finite() && q.is_finite(),
                "non-finite value at vector {index}, dimension {dimension}"
            );
            max_diff = max_diff.max((p - q).abs());
            anyhow::ensure!(
                p.to_bits() == q.to_bits(),
                "bit mismatch at vector {index}, dimension {dimension}"
            );
        }
    }
    Ok(max_diff)
}

fn finite_max_abs_diff(a: &[Vec<f32>], b: &[Vec<f32>]) -> anyhow::Result<f32> {
    anyhow::ensure!(a.len() == b.len(), "vector count mismatch");
    let mut max_diff = 0.0f32;
    for (index, (left, right)) in a.iter().zip(b).enumerate() {
        anyhow::ensure!(
            left.len() == right.len(),
            "dimension mismatch at vector {index}"
        );
        for (dimension, (&p, &q)) in left.iter().zip(right).enumerate() {
            anyhow::ensure!(
                p.is_finite() && q.is_finite(),
                "non-finite value at vector {index}, dimension {dimension}"
            );
            max_diff = max_diff.max((p - q).abs());
        }
    }
    Ok(max_diff)
}

fn median(mut v: Vec<f64>) -> f64 {
    v.sort_by(f64::total_cmp);
    v[v.len() / 2]
}

#[cfg(test)]
mod tests {
    use super::exact_max_abs_diff;

    #[test]
    fn exact_parity_rejects_shape_and_bit_false_passes() {
        assert!(exact_max_abs_diff(&[vec![1.0]], &[vec![1.0], vec![2.0]]).is_err());
        assert!(exact_max_abs_diff(&[vec![1.0]], &[vec![1.0, 2.0]]).is_err());
        assert!(exact_max_abs_diff(&[vec![f32::NAN]], &[vec![f32::NAN]]).is_err());
        assert!(exact_max_abs_diff(&[vec![0.0]], &[vec![-0.0]]).is_err());
    }
}

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let models_dir = PathBuf::from(&args[1]);
    let passes: usize = std::env::var("T1_PASSES")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(5);
    let config = engraph::config::Config::default();
    let mode = args.get(2).map(|s| s.as_str()).unwrap_or("parity");

    // Timing modes: exactly ONE model resident, then out.
    if mode == "time-base" || mode == "time-actor" {
        let mut embedder: Box<dyn EmbedModel + Send> = if mode == "time-base" {
            Box::new(LlamaEmbed::new(&models_dir, &config)?)
        } else {
            Box::new(EmbedActor::new(&models_dir, &config)?)
        };
        let reps = 20;
        for _ in 0..5 {
            embedder.embed_one(QUERIES[0])?;
        }
        let mut runs: Vec<f64> = Vec::new();
        for pass in 0..passes {
            let t = Instant::now();
            for _ in 0..reps {
                embedder.embed_one(QUERIES[0])?;
            }
            let ms = t.elapsed().as_secs_f64() * 1000.0 / reps as f64;
            runs.push(ms);
            println!("{{\"probe\":\"{mode}\",\"pass\":{pass},\"ms_per_query\":{ms:.3}}}");
        }
        let med = median(runs.clone());
        let lo = runs.iter().cloned().fold(f64::INFINITY, f64::min);
        let hi = runs.iter().cloned().fold(0.0, f64::max);
        println!(
            "{{\"probe\":\"summary\",\"mode\":\"{mode}\",\"median_ms\":{med:.3},\
             \"min_ms\":{lo:.3},\"max_ms\":{hi:.3}}}"
        );
        return Ok(());
    }

    let mut base = LlamaEmbed::new(&models_dir, &config)?;
    let mut actor = EmbedActor::new(&models_dir, &config)?;
    anyhow::ensure!(base.dim() == actor.dim(), "dim mismatch");

    // ── Gate 1: document path parity, across the real size range ────────────
    for words in [8usize, 32, 128, 512] {
        let t = texts(4, words);
        let refs: Vec<&str> = t.iter().map(|s| s.as_str()).collect();
        let b = base.embed_batch(&refs)?;
        let a = actor.embed_batch(&refs)?;
        let d = exact_max_abs_diff(&a, &b)?;
        println!(
            "{{\"gate\":\"doc_parity\",\"words\":{words},\"max_abs_diff\":{d:e},\
             \"bit_identical\":{}}}",
            d == 0.0
        );
    }

    // ── Gate 2: query path parity (format_query, separate actor variant) ────
    for q in QUERIES {
        let b = vec![base.embed_one(q)?];
        let a = vec![actor.embed_one(q)?];
        let d = exact_max_abs_diff(&a, &b)?;
        println!(
            "{{\"gate\":\"query_parity\",\"query\":\"{q}\",\"max_abs_diff\":{d:e},\
             \"bit_identical\":{}}}",
            d == 0.0
        );
    }

    // ── Gate 3: a query must NOT equal the same text embedded as a document ─
    // Guards the actual hazard of routing One through Batch: embeddinggemma is
    // asymmetric, so this diff must be NON-zero. If it were 0 the actor would
    // be applying the wrong prompt format and every gate above would still pass.
    let q = QUERIES[0];
    let as_query = vec![actor.embed_one(q)?];
    let as_doc = actor.embed_batch(&[q])?;
    let fmt_delta = finite_max_abs_diff(&as_query, &as_doc)?;
    println!(
        "{{\"gate\":\"prompt_format_distinct\",\"query_vs_document_diff\":{fmt_delta:e},\
         \"formats_differ\":{}}}",
        fmt_delta > 0.0
    );
    anyhow::ensure!(
        fmt_delta > 0.0,
        "query and document embeddings are identical — the actor is applying one prompt format to both"
    );

    println!(
        "{{\"probe\":\"parity\",\"result\":\"all gates passed\",\
         \"note\":\"timing lives in time-base / time-actor — one model per process\"}}"
    );
    Ok(())
}
