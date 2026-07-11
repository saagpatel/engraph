//! Characterize the graph lane: how many SQLite probes does graph_expand fire,
//! and what does each cost? Replicates graph_expand's loop with counters.
//! Usage: fable_graph_bench <data_dir> "query"

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::time::Instant;

use engraph::fusion::RankedResult;
use engraph::graph;
use engraph::llm::{self, EmbedModel};
use engraph::store::Store;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let data_dir = PathBuf::from(&args[1]);
    let query = &args[2];

    let config = engraph::config::Config::default();
    let mut embedder = llm::LlamaEmbed::new(&data_dir.join("models"), &config)?;
    let store = Store::open(&data_dir.join("engraph.db"))?;

    // Build seeds the same way search_with_intelligence does (semantic + fts).
    let qvec = embedder.embed_one(query)?;
    let tombstones = HashSet::new();
    let raw = store.search_vec(&qvec, 15, &tombstones)?;
    let mut by_file: HashMap<String, RankedResult> = HashMap::new();
    for (vector_id, distance) in raw {
        if let Some(chunk) = store.get_chunk_by_vector_id(vector_id)? {
            if let Some(f) = store.get_file_by_id(chunk.file_id)? {
                let score = (1.0 - distance) as f64;
                let e = by_file.entry(f.path.clone()).or_insert(RankedResult {
                    file_path: f.path,
                    file_id: chunk.file_id,
                    score,
                    heading: None,
                    snippet: String::new(),
                    docid: f.docid,
                });
                if score > e.score {
                    e.score = score;
                }
            }
        }
    }
    for fr in store.fts_search(query, 15).unwrap_or_default() {
        if let Some(f) = store.get_file_by_id(fr.file_id)? {
            let e = by_file.entry(f.path.clone()).or_insert(RankedResult {
                file_path: f.path,
                file_id: fr.file_id,
                score: fr.score,
                heading: None,
                snippet: String::new(),
                docid: f.docid,
            });
            if fr.score > e.score {
                e.score = fr.score;
            }
        }
    }
    let seeds: Vec<RankedResult> = by_file.into_values().collect();
    let terms = graph::extract_query_terms(query);
    println!("seeds={} terms={}", seeds.len(), terms.len());

    // Count neighbor sets + probe costs, mirroring graph_expand's loop shape.
    let mut n_neighbors = 0usize;
    let mut n_term_probes = 0usize;
    let mut n_tag_probes = 0usize;
    let mut t_neighbors = 0u128;
    let mut t_term = 0u128;
    let mut t_tag = 0u128;
    let seed_ids: HashSet<i64> = seeds.iter().map(|s| s.file_id).collect();
    for seed in &seeds {
        let t = Instant::now();
        let neighbors = store.get_neighbors(seed.file_id, 2)?;
        t_neighbors += t.elapsed().as_micros();
        for (neighbor_id, _hop) in neighbors {
            if seed_ids.contains(&neighbor_id) {
                continue;
            }
            n_neighbors += 1;
            let mut matched = false;
            for term in &terms {
                n_term_probes += 1;
                let t = Instant::now();
                let hit = store.file_contains_term(neighbor_id, term).unwrap_or(false);
                t_term += t.elapsed().as_micros();
                if hit {
                    matched = true;
                    break;
                }
            }
            if !matched {
                n_tag_probes += 1;
                let t = Instant::now();
                let _ = store
                    .get_shared_tags_files(neighbor_id, 100)
                    .unwrap_or_default();
                t_tag += t.elapsed().as_micros();
            }
        }
    }
    println!(
        "neighbors_visited={n_neighbors} term_probes={n_term_probes} tag_probes={n_tag_probes}"
    );
    println!(
        "t_get_neighbors_ms={} t_term_probes_ms={} t_tag_probes_ms={}",
        t_neighbors / 1000,
        t_term / 1000,
        t_tag / 1000
    );

    // And the real call end-to-end for comparison:
    let t = Instant::now();
    let g = graph::graph_expand(&store, &seeds, query, 2, 20)?;
    println!(
        "graph_expand_real_ms={} results={}",
        t.elapsed().as_millis(),
        g.len()
    );
    Ok(())
}
