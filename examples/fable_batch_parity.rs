//! Prove shared-context batch embedding returns the same vectors as
//! fresh-context per-text embedding (the pre-fix behavior, reproduced by
//! single-item batches). Also times both.
//! Usage: fable_batch_parity <models_dir>

use std::path::PathBuf;
use std::time::Instant;

use engraph::llm::{EmbedModel, LlamaEmbed};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let models_dir = PathBuf::from(&args[1]);
    let config = engraph::config::Config::default();
    let mut embedder = LlamaEmbed::new(&models_dir, &config)?;

    let texts: Vec<String> = (0..32)
        .map(|i| {
            format!(
                "Note {i}: this chunk discusses retrieval pipelines, wikilink graphs, \
                 and reciprocal rank fusion in a personal knowledge vault. Sample {i} \
                 varies the content slightly to avoid identical sequences."
            )
        })
        .collect();
    let refs: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();

    // Shared-context batch (new behavior)
    let t = Instant::now();
    let batched = embedder.embed_batch(&refs)?;
    let t_batch = t.elapsed().as_millis();

    // Fresh-context per text (old behavior: one context per call)
    let t = Instant::now();
    let mut singles: Vec<Vec<f32>> = Vec::new();
    for r in &refs {
        singles.extend(embedder.embed_batch(&[r])?);
    }
    let t_single = t.elapsed().as_millis();

    let mut max_abs_diff = 0f32;
    let mut mismatched = 0usize;
    for (b, s) in batched.iter().zip(singles.iter()) {
        assert_eq!(b.len(), s.len());
        let d = b
            .iter()
            .zip(s.iter())
            .map(|(x, y)| (x - y).abs())
            .fold(0f32, f32::max);
        max_abs_diff = max_abs_diff.max(d);
        if d > 1e-6 {
            mismatched += 1;
        }
    }
    println!(
        "{{\"n\":{},\"batch_ms\":{},\"per_text_fresh_ms\":{},\"max_abs_diff\":{:e},\"vectors_beyond_1e-6\":{}}}",
        refs.len(),
        t_batch,
        t_single,
        max_abs_diff,
        mismatched
    );
    Ok(())
}
