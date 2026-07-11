//! Per-stage timing harness for the engraph query path.
//!
//! Mirrors `search_with_intelligence` (intelligence off) stage by stage using
//! the same public functions, so stage times sum to roughly the real pipeline.
//! Usage:
//!   fable_bench <data_dir> <repeats> "query 1" ["query 2" ...]
//! Emits JSON lines: one per (query, repeat) with per-stage micros, plus a
//! model-load line and a full `search_internal` end-to-end line per repeat.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Instant;

use engraph::fusion::{self, RankedResult};
use engraph::graph;
use engraph::llm::{self, EmbedModel};
use engraph::search;
use engraph::store::Store;

fn us(t: Instant) -> u128 {
    t.elapsed().as_micros()
}

fn dedup_by_file(results: Vec<RankedResult>) -> Vec<RankedResult> {
    let mut by_file: HashMap<String, RankedResult> = HashMap::new();
    for r in results {
        let dominated = by_file
            .get(&r.file_path)
            .is_some_and(|e| e.score >= r.score);
        if !dominated {
            by_file.insert(r.file_path.clone(), r);
        }
    }
    let mut v: Vec<RankedResult> = by_file.into_values().collect();
    v.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    v
}

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: fable_bench <data_dir> <repeats> <query>...");
        std::process::exit(2);
    }
    let data_dir = PathBuf::from(&args[1]);
    let repeats: usize = args[2].parse()?;
    let queries: Vec<&String> = args[3..].iter().collect();

    let config = engraph::config::Config::default();
    let models_dir = data_dir.join("models");

    let t = Instant::now();
    let mut embedder = llm::LlamaEmbed::new(&models_dir, &config)?;
    println!("{{\"stage\":\"model_load\",\"us\":{}}}", us(t));

    let store = Store::open(&data_dir.join("engraph.db"))?;

    // Metal / context warmup (first encode compiles pipelines)
    let t = Instant::now();
    let _ = embedder.embed_one("warmup")?;
    println!("{{\"stage\":\"first_embed_warmup\",\"us\":{}}}", us(t));

    let top_n = 5usize;
    for query in &queries {
        for rep in 0..repeats {
            // Stage: orchestrate (heuristic)
            let t = Instant::now();
            let orch = llm::heuristic_orchestrate(query);
            let t_orch = us(t);

            let mut all_semantic: Vec<RankedResult> = Vec::new();
            let mut all_fts: Vec<RankedResult> = Vec::new();
            let mut t_embed = 0u128;
            let mut t_vec = 0u128;
            let mut t_hydrate = 0u128;
            let mut t_fts = 0u128;

            for exp in &orch.expansions {
                // Stage: embed query
                let t = Instant::now();
                let qvec = embedder.embed_one(exp)?;
                t_embed += us(t);

                // Stage: KNN scan
                let t = Instant::now();
                let tombstones = std::collections::HashSet::new();
                let raw = store.search_vec(&qvec, top_n * 3, &tombstones)?;
                t_vec += us(t);

                // Stage: hydrate semantic hits (chunk + file point lookups)
                let t = Instant::now();
                let mut sem_by_file: HashMap<String, RankedResult> = HashMap::new();
                for (vector_id, distance) in raw {
                    if let Some(chunk) = store.get_chunk_by_vector_id(vector_id)? {
                        let (file_path, docid) = match store.get_file_by_id(chunk.file_id)? {
                            Some(f) => (f.path, f.docid),
                            None => ("<unknown>".to_string(), None),
                        };
                        let score = (1.0 - distance) as f64;
                        let heading = if chunk.heading.is_empty() {
                            None
                        } else {
                            Some(chunk.heading)
                        };
                        let better = sem_by_file
                            .get(&file_path)
                            .map(|e| score > e.score)
                            .unwrap_or(true);
                        if better {
                            sem_by_file.insert(
                                file_path.clone(),
                                RankedResult {
                                    file_path,
                                    file_id: chunk.file_id,
                                    score,
                                    heading,
                                    snippet: chunk.snippet,
                                    docid,
                                },
                            );
                        }
                    }
                }
                all_semantic.extend(sem_by_file.into_values());
                t_hydrate += us(t);

                // Stage: FTS + hydrate
                let t = Instant::now();
                let fts_raw = store.fts_search(exp, top_n * 3).unwrap_or_default();
                let mut fts_by_file: HashMap<String, RankedResult> = HashMap::new();
                for fr in fts_raw {
                    let (file_path, docid) = match store.get_file_by_id(fr.file_id)? {
                        Some(f) => (f.path, f.docid),
                        None => continue,
                    };
                    let better = fts_by_file
                        .get(&file_path)
                        .map(|e| fr.score > e.score)
                        .unwrap_or(true);
                    if better {
                        fts_by_file.insert(
                            file_path.clone(),
                            RankedResult {
                                file_path,
                                file_id: fr.file_id,
                                score: fr.score,
                                heading: None,
                                snippet: fr.snippet,
                                docid,
                            },
                        );
                    }
                }
                all_fts.extend(fts_by_file.into_values());
                t_fts += us(t);
            }

            let semantic_results = dedup_by_file(all_semantic);
            let fts_results = dedup_by_file(all_fts);

            // Stage: graph expansion (seeds = union, same as merge_seeds)
            let t = Instant::now();
            let mut by_file: HashMap<String, RankedResult> = HashMap::new();
            for r in semantic_results.iter().chain(fts_results.iter()) {
                let dominated = by_file
                    .get(&r.file_path)
                    .is_some_and(|e| e.score >= r.score);
                if !dominated {
                    by_file.insert(r.file_path.clone(), r.clone());
                }
            }
            let seeds: Vec<RankedResult> = by_file.into_values().collect();
            let graph_results =
                graph::graph_expand(&store, &seeds, query, 2, 20).unwrap_or_default();
            let t_graph = us(t);

            // Stage: RRF fusion
            let t = Instant::now();
            let weights = llm::LaneWeights::from_intent(&orch.intent);
            let fused = fusion::rrf_fuse(
                &[
                    ("semantic", &semantic_results, weights.semantic),
                    ("fts", &fts_results, weights.fts),
                    ("graph", &graph_results, weights.graph),
                ],
                60,
            );
            let t_fuse = us(t);

            // End-to-end via the real entry point
            let t = Instant::now();
            let output = search::search_internal(query, top_n, &store, &mut embedder)?;
            let t_e2e = us(t);

            println!(
                "{{\"query\":{:?},\"rep\":{},\"orch_us\":{},\"embed_us\":{},\"vec_us\":{},\"hydrate_us\":{},\"fts_us\":{},\"graph_us\":{},\"fuse_us\":{},\"e2e_us\":{},\"n_sem\":{},\"n_fts\":{},\"n_graph\":{},\"n_fused\":{},\"n_results\":{}}}",
                query, rep, t_orch, t_embed, t_vec, t_hydrate, t_fts, t_graph, t_fuse, t_e2e,
                semantic_results.len(), fts_results.len(), graph_results.len(), fused.len(),
                output.results.len(),
            );
        }
    }
    Ok(())
}
