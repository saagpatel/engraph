//! Compare sqlite-vec vec0 KNN against a plain in-memory brute-force scan
//! over the same vectors (loaded from chunks.vector BLOBs).
//! Also verifies both return the same top-k ids.
//! Usage: fable_vec_bench <data_dir> <repeats>

use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Instant;

use engraph::store::Store;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let data_dir = PathBuf::from(&args[1]);
    let repeats: usize = args[2].parse()?;

    let mut store = Store::open(&data_dir.join("engraph.db"))?;
    let cached_vectors = store.enable_vector_cache()?;
    let conn = rusqlite::Connection::open(data_dir.join("engraph.db"))?;

    // Load all vectors into memory.
    let t = Instant::now();
    let mut stmt = conn.prepare("SELECT vector_id, vector FROM chunks WHERE vector IS NOT NULL")?;
    let rows: Vec<(u64, Vec<f32>)> = stmt
        .query_map([], |row| {
            let id: i64 = row.get(0)?;
            let blob: Vec<u8> = row.get(1)?;
            let v: Vec<f32> = blob
                .chunks_exact(4)
                .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
                .collect();
            Ok((id as u64, v))
        })?
        .collect::<Result<_, _>>()?;
    println!(
        "{{\"stage\":\"load_vectors\",\"n\":{},\"dim\":{},\"cached_vectors\":{},\"us\":{}}}",
        rows.len(),
        rows.first().map(|r| r.1.len()).unwrap_or(0),
        cached_vectors,
        t.elapsed().as_micros()
    );

    // Drift check between chunks.vector and chunks_vec (dual stores).
    let n_chunks: i64 = conn.query_row("SELECT COUNT(*) FROM chunks", [], |r| r.get(0))?;
    let n_vec: i64 = conn.query_row("SELECT COUNT(*) FROM chunks_vec", [], |r| r.get(0))?;
    let missing_in_vec: i64 = conn.query_row(
        "SELECT COUNT(*) FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM chunks_vec v WHERE v.rowid = c.vector_id)",
        [], |r| r.get(0))?;
    let orphan_in_vec: i64 = conn.query_row(
        "SELECT COUNT(*) FROM chunks_vec v WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.vector_id = v.rowid)",
        [], |r| r.get(0))?;
    println!(
        "{{\"stage\":\"drift\",\"chunks\":{n_chunks},\"vec_rows\":{n_vec},\"chunks_missing_in_vec\":{missing_in_vec},\"vec_orphans\":{orphan_in_vec}}}"
    );

    let k = 15usize;

    for rep in 0..repeats {
        // Use a deterministic battery of distinct stored vectors. A stored
        // vector is a valid normalized cosine query and keeps this gate
        // independent of model loading and the operator's live data.
        let query_index = rep * rows.len() / repeats.max(1);
        let query = rows[query_index.min(rows.len() - 1)].1.clone();

        // sqlite-vec path (the CLI/reference implementation)
        let t = Instant::now();
        let tombstones = std::collections::HashSet::new();
        let sv = engraph::vecstore::search_vec(&conn, &query, k, &tombstones)?;
        let t_sqlite = t.elapsed().as_micros();

        // Serve path: Store::search_vec dispatches to its enabled cache.
        let t = Instant::now();
        let cached = store.search_vec(&query, k, &tombstones)?;
        let t_cached = t.elapsed().as_micros();

        // Independent in-memory brute force (cosine distance = 1 - dot,
        // vectors are L2-normalized).
        let t = Instant::now();
        let mut scored: Vec<(u64, f32)> = rows
            .iter()
            .map(|(id, v)| {
                let dot: f32 = v.iter().zip(query.iter()).map(|(a, b)| a * b).sum();
                (*id, 1.0 - dot)
            })
            .collect();
        scored.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
        scored.truncate(k);
        let t_mem = t.elapsed().as_micros();

        let sv_ids: Vec<u64> = sv.iter().map(|(id, _)| *id).collect();
        let cached_ids: Vec<u64> = cached.iter().map(|(id, _)| *id).collect();
        let mem_ids: Vec<u64> = scored.iter().map(|(id, _)| *id).collect();
        let same_order = sv_ids == mem_ids;
        let same_set = sv_ids.iter().copied().collect::<HashSet<_>>()
            == mem_ids.iter().copied().collect::<HashSet<_>>();
        let same_cached_set = sv_ids.iter().copied().collect::<HashSet<_>>()
            == cached_ids.iter().copied().collect::<HashSet<_>>();
        println!(
            "{{\"rep\":{rep},\"query_index\":{query_index},\"sqlite_vec_us\":{t_sqlite},\"cached_us\":{t_cached},\"in_memory_us\":{t_mem},\"same_topk_set\":{same_set},\"same_cached_set\":{same_cached_set},\"same_topk_order\":{same_order}}}"
        );
        if (!same_set || !same_cached_set) && rep == 0 {
            eprintln!("sqlite: {:?}", sv_ids);
            eprintln!("cached: {:?}", cached_ids);
            eprintln!("memory: {:?}", mem_ids);
        }
    }
    Ok(())
}
