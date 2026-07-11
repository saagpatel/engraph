//! Compare sqlite-vec vec0 KNN against a plain in-memory brute-force scan
//! over the same vectors (loaded from chunks.vector BLOBs).
//! Also verifies both return the same top-k ids.
//! Usage: fable_vec_bench <data_dir> <repeats>

use std::path::PathBuf;
use std::time::Instant;

use engraph::store::Store;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let data_dir = PathBuf::from(&args[1]);
    let repeats: usize = args[2].parse()?;

    let store = Store::open(&data_dir.join("engraph.db"))?;
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
        "{{\"stage\":\"load_vectors\",\"n\":{},\"dim\":{},\"us\":{}}}",
        rows.len(),
        rows.first().map(|r| r.1.len()).unwrap_or(0),
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

    // Use one of the stored vectors (normalized) as the query.
    let query = rows[rows.len() / 2].1.clone();
    let k = 15usize;

    for rep in 0..repeats {
        // sqlite-vec path (same call the search pipeline makes)
        let t = Instant::now();
        let tombstones = std::collections::HashSet::new();
        let sv = store.search_vec(&query, k, &tombstones)?;
        let t_sqlite = t.elapsed().as_micros();

        // in-memory brute force (cosine distance = 1 - dot, vectors are L2-normalized)
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
        let mem_ids: Vec<u64> = scored.iter().map(|(id, _)| *id).collect();
        let same = sv_ids == mem_ids;
        println!(
            "{{\"rep\":{rep},\"sqlite_vec_us\":{t_sqlite},\"in_memory_us\":{t_mem},\"same_topk\":{same}}}"
        );
        if !same && rep == 0 {
            eprintln!("sqlite: {:?}", sv_ids);
            eprintln!("memory: {:?}", mem_ids);
        }
    }
    Ok(())
}
