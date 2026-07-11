//! Times the REAL LlamaEmbed::embed_one in a tight loop, to compare against
//! the raw llama.cpp micro-bench (fable_embed_bench).
//! Usage: fable_embed_real <data_dir> <repeats> "query"

use std::path::PathBuf;
use std::time::Instant;

use engraph::llm::{EmbedModel, LlamaEmbed};

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let data_dir = PathBuf::from(&args[1]);
    let repeats: usize = args[2].parse()?;
    let query = &args[3];

    let config = engraph::config::Config::default();
    let t = Instant::now();
    let mut embedder = LlamaEmbed::new(&data_dir.join("models"), &config)?;
    eprintln!("model_load_us={}", t.elapsed().as_micros());
    eprintln!("pid={}", std::process::id());

    // Optional perturbations between embeds, to isolate what slows the GPU path:
    //   FABLE_SLEEP_MS=300     — idle gap only
    //   FABLE_SQLITE_WORK=1    — run a vec scan + FTS between embeds (needs db)
    let sleep_ms: u64 = std::env::var("FABLE_SLEEP_MS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let sqlite_work = std::env::var("FABLE_SQLITE_WORK").is_ok();
    let store = if sqlite_work {
        Some(engraph::store::Store::open(&data_dir.join("engraph.db"))?)
    } else {
        None
    };
    let probe_vec = embedder.embed_one("warmup probe")?;

    for rep in 0..repeats {
        if sleep_ms > 0 {
            std::thread::sleep(std::time::Duration::from_millis(sleep_ms));
        }
        if let Some(ref s) = store {
            let tombstones = std::collections::HashSet::new();
            let _ = s.search_vec(&probe_vec, 15, &tombstones)?;
            let _ = s.fts_search(query, 15).unwrap_or_default();
        }
        let t = Instant::now();
        let v = embedder.embed_one(query)?;
        println!(
            "{{\"rep\":{},\"embed_one_us\":{},\"dim\":{}}}",
            rep,
            t.elapsed().as_micros(),
            v.len()
        );
    }
    Ok(())
}
