//! P3 de-risk: is a FIXED-size context numerically identical to one fitted to
//! the input?
//!
//! P3 (03-perf-proposals.md) proposes an actor thread owning model + context and
//! reusing that context across every call, to remove the ~5.5ms of per-call
//! Metal pipeline setup that 11-perf-claims-audit.md localized as the query
//! path's real cost. That design only works if one context, sized once, gives
//! the same vectors as today's `make_context(max_tokens)` which fits n_ctx /
//! n_ubatch / n_batch to each input.
//!
//! This is not a safe assumption. 09/FABLE-T1-NOTES established that this
//! encoder's numerics are sensitive to batch SHAPE (token offset within the
//! ubatch, and ne11-keyed Metal kernel selection). n_ctx is an allocation size
//! rather than a graph dimension, so it *should* be inert — but "should" is what
//! this session keeps refuting. Measure it.
//!
//! A non-zero diff here does not merely complicate P3, it changes the design:
//! a reused context would have to be re-created per input size, which is the
//! per-call cost P3 exists to remove.
//!
//! Compares, over the same texts:
//!   control = fitted context (ENGRAPH_P3_NCTX unset, today's behavior)
//!   fixed   = ENGRAPH_P3_NCTX=<n> forced on the SAME binary
//! Run twice, once per mode, comparing against a dump — the two configs cannot
//! coexist in one process because the env is read inside make_context.
//!
//! Usage: p3_nctx_parity <models_dir> dump <path>    write control vectors
//!        p3_nctx_parity <models_dir> cmp  <path>    compare current vs dump
//!   ENGRAPH_P3_NCTX=<n>  set on the `cmp` run to test a fixed size

use std::path::PathBuf;

use engraph::llm::{EmbedModel, LlamaEmbed};

/// Inputs spanning the real range: a query, a small chunk, a production-cap
/// chunk. If a fixed context perturbs anything, size variety is where it shows.
fn texts() -> Vec<String> {
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
    let build = |words: usize, seed: usize| -> String {
        let mut s = format!("Note {seed}. ");
        for w in 0..words {
            s.push_str(POOL[(w + seed * 5) % POOL.len()]);
            s.push(if w % 12 == 11 { '.' } else { ' ' });
        }
        s
    };
    let mut v = vec!["how does the retrieval pipeline rank candidates".to_string()];
    for (i, words) in [8usize, 32, 128, 512].iter().enumerate() {
        v.push(build(*words, i));
    }
    v
}

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let models_dir = PathBuf::from(&args[1]);
    let mode = args[2].as_str();
    let path = PathBuf::from(&args[3]);

    let config = engraph::config::Config::default();
    let mut embedder = LlamaEmbed::new(&models_dir, &config)?;
    let t = texts();
    let refs: Vec<&str> = t.iter().map(|s| s.as_str()).collect();

    // embed_batch shares one fitted context across the batch, so per-text
    // sizing differences would be masked. Encode each text on its own context —
    // that is the path P3 replaces.
    let mut vecs: Vec<Vec<f32>> = Vec::new();
    for r in &refs {
        vecs.extend(embedder.embed_batch(&[r])?);
    }

    let nctx = std::env::var("ENGRAPH_P3_NCTX").unwrap_or_else(|_| "fitted".into());

    match mode {
        "dump" => {
            std::fs::write(&path, serde_json::to_vec(&vecs)?)?;
            println!(
                "{{\"probe\":\"dump\",\"nctx\":\"{nctx}\",\"texts\":{},\"dim\":{}}}",
                vecs.len(),
                vecs[0].len()
            );
        }
        "cmp" => {
            let base: Vec<Vec<f32>> = serde_json::from_slice(&std::fs::read(&path)?)?;
            anyhow::ensure!(base.len() == vecs.len(), "dump/current length mismatch");
            let mut worst = 0f32;
            let mut worst_i = 0usize;
            for (i, (a, b)) in base.iter().zip(vecs.iter()).enumerate() {
                let d = a
                    .iter()
                    .zip(b.iter())
                    .map(|(p, q)| (p - q).abs())
                    .fold(0f32, f32::max);
                if d > worst {
                    worst = d;
                    worst_i = i;
                }
            }
            let tokens: Vec<usize> = refs.iter().map(|r| embedder.token_count(r)).collect();
            println!(
                "{{\"probe\":\"cmp\",\"nctx\":\"{nctx}\",\"tokens\":{tokens:?},\
                 \"max_abs_diff\":{worst:e},\"worst_text\":{worst_i},\
                 \"bit_identical\":{}}}",
                worst == 0.0
            );
        }
        other => anyhow::bail!("unknown mode {other}"),
    }
    Ok(())
}
