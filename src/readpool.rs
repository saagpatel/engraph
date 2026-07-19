//! A small pool of read-only [`Store`] connections.
//!
//! The server holds one `Arc<Mutex<Store>>` for writes. Routing reads through
//! that same mutex serializes every search behind every other search, which is
//! what this pool exists to avoid: SQLite is already in WAL mode and supports
//! concurrent readers, it just needs more than one connection to do it with.
//!
//! Checkout uses a semaphore sized to the pool plus a free list. The semaphore
//! is what callers wait on; the free-list mutex is held only long enough to pop
//! or push a connection, never across query work. Because a permit is acquired
//! before the pop, the free list is guaranteed non-empty at that point.

use anyhow::Result;
use std::ops::Deref;
use std::path::Path;
use std::sync::Arc;
use std::sync::Mutex as StdMutex;
use tokio::sync::{Semaphore, SemaphorePermit};

use crate::store::Store;
use crate::vecstore::VectorCache;

/// Default number of read connections.
///
/// Retrieval is ~79% of a search and is store-bound, so this is effectively the
/// concurrency limit of the whole search path: at size 4 with 8 concurrent
/// searches, requests spent 147ms of a 217ms retrieve phase just waiting for a
/// connection. Each connection costs a file handle and its own SQLite page
/// cache (the vector cache is shared), so it is bounded rather than unlimited.
pub const DEFAULT_READ_POOL_SIZE: usize = 4;

/// Environment override for the read pool size, so it can be swept against a
/// real workload without a rebuild.
pub const READ_POOL_SIZE_ENV: &str = "ENGRAPH_READ_POOL_SIZE";

/// Default number of candidates handed to the cross-encoder reranker.
pub const DEFAULT_RERANK_CANDIDATES: usize = 30;

/// Environment override for the reranker candidate count.
pub const RERANK_CANDIDATES_ENV: &str = "ENGRAPH_RERANK_CANDIDATES";

/// Configured reranker candidate count.
///
/// This is the dominant cost of an intelligence-enabled search: the reranker
/// is a single exclusive model scoring each candidate in turn, so the count
/// multiplies directly into latency and cannot overlap across requests.
pub fn configured_rerank_candidates() -> usize {
    parse_positive(
        std::env::var(RERANK_CANDIDATES_ENV).ok().as_deref(),
        DEFAULT_RERANK_CANDIDATES,
    )
}

/// Configured pool size: `$ENGRAPH_READ_POOL_SIZE` when it parses to a
/// positive integer, else [`DEFAULT_READ_POOL_SIZE`].
pub fn configured_size() -> usize {
    parse_positive(
        std::env::var(READ_POOL_SIZE_ENV).ok().as_deref(),
        DEFAULT_READ_POOL_SIZE,
    )
}

/// Pure parse step, testable without touching process-global env state.
fn parse_positive(raw: Option<&str>, fallback: usize) -> usize {
    raw.and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(fallback)
}

pub struct ReadPool {
    free: StdMutex<Vec<Store>>,
    permits: Semaphore,
    size: usize,
}

impl ReadPool {
    /// Open `size` read-only connections to the database at `path`.
    ///
    /// A size of zero is meaningless (no reader could ever check out) and is
    /// treated as one.
    pub fn open(path: &Path, size: usize, cache: Option<Arc<VectorCache>>) -> Result<Self> {
        let size = size.max(1);
        let mut free = Vec::with_capacity(size);
        for _ in 0..size {
            let mut store = Store::open_read_only(path)?;
            store.set_vector_cache(cache.clone());
            free.push(store);
        }
        Ok(Self {
            free: StdMutex::new(free),
            permits: Semaphore::new(size),
            size,
        })
    }

    /// Number of connections in the pool.
    pub fn size(&self) -> usize {
        self.size
    }

    /// Check out a connection, waiting if all of them are busy.
    pub async fn get(&self) -> PooledStore<'_> {
        let permit = self
            .permits
            .acquire()
            .await
            .expect("read pool semaphore is never closed");
        let store = self
            .free
            .lock()
            .expect("read pool free list poisoned")
            .pop()
            .expect("a permit guarantees an available connection");
        PooledStore {
            store: Some(store),
            pool: self,
            _permit: permit,
        }
    }
}

/// A connection checked out of the pool, returned on drop.
pub struct PooledStore<'a> {
    store: Option<Store>,
    pool: &'a ReadPool,
    _permit: SemaphorePermit<'a>,
}

impl Deref for PooledStore<'_> {
    type Target = Store;

    fn deref(&self) -> &Store {
        self.store
            .as_ref()
            .expect("store is only taken in Drop, after which deref is unreachable")
    }
}

impl Drop for PooledStore<'_> {
    fn drop(&mut self) {
        if let Some(store) = self.store.take() {
            // A poisoned free list means another thread panicked mid-checkout.
            // Dropping the connection is preferable to propagating a panic out
            // of Drop, so the pool shrinks rather than taking the server down.
            if let Ok(mut free) = self.pool.free.lock() {
                free.push(store);
            }
        }
    }
}

/// A store borrowed for reading, from the pool when one exists and from the
/// exclusive write connection when it does not.
///
/// The fallback is not a performance path: it reintroduces the serialization
/// the pool exists to remove. It is here for in-memory stores, which cannot be
/// reopened by a second connection, so tests do not have to be file-backed.
pub enum ReadHandle<'a> {
    Pooled(PooledStore<'a>),
    Exclusive(tokio::sync::MutexGuard<'a, Store>),
}

impl Deref for ReadHandle<'_> {
    type Target = Store;

    fn deref(&self) -> &Store {
        match self {
            ReadHandle::Pooled(store) => store,
            ReadHandle::Exclusive(guard) => guard,
        }
    }
}

/// Borrow a store for reading, preferring the pool.
pub async fn acquire_read<'a>(
    pool: Option<&'a ReadPool>,
    exclusive: &'a tokio::sync::Mutex<Store>,
) -> ReadHandle<'a> {
    match pool {
        Some(pool) => ReadHandle::Pooled(pool.get().await),
        None => ReadHandle::Exclusive(exclusive.lock().await),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;
    use tempfile::tempdir;

    /// Build a real on-disk database so the pool has something to open.
    fn seeded_db() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempdir().unwrap();
        let path = dir.path().join("engraph.db");
        let store = Store::open(&path).unwrap();
        drop(store);
        (dir, path)
    }

    #[test]
    fn parse_positive_falls_back_on_missing_or_invalid_values() {
        assert_eq!(parse_positive(None, 4), 4);
        assert_eq!(parse_positive(Some(""), 4), 4);
        assert_eq!(parse_positive(Some("banana"), 4), 4);
        // Zero would mean a pool nobody can check out of.
        assert_eq!(parse_positive(Some("0"), 4), 4);
        assert_eq!(parse_positive(Some("-2"), 4), 4);
    }

    #[test]
    fn parse_positive_honors_a_valid_override() {
        assert_eq!(parse_positive(Some("12"), 4), 12);
        assert_eq!(parse_positive(Some(" 6 "), 4), 6);
    }

    #[tokio::test]
    async fn opens_requested_number_of_connections() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 3, None).unwrap();
        assert_eq!(pool.size(), 3);
    }

    #[tokio::test]
    async fn zero_size_is_clamped_to_one() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 0, None).unwrap();
        assert_eq!(pool.size(), 1);
        // Must still be usable, not deadlocked on an empty semaphore.
        let _guard = pool.get().await;
    }

    /// The point of the pool: distinct concurrent checkouts, not one shared
    /// connection handed out repeatedly.
    #[tokio::test]
    async fn hands_out_distinct_connections_concurrently() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 2, None).unwrap();

        let a = pool.get().await;
        let b = pool.get().await;
        let a_ptr = std::ptr::addr_of!(*a) as usize;
        let b_ptr = std::ptr::addr_of!(*b) as usize;
        assert_ne!(a_ptr, b_ptr, "pool handed out the same connection twice");
    }

    /// A checked-out connection must go back, or the pool leaks itself empty.
    #[tokio::test]
    async fn connections_return_to_the_pool_on_drop() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 1, None).unwrap();

        for _ in 0..5 {
            let guard = pool.get().await;
            drop(guard);
        }
        // A fifth acquisition would hang forever if drop failed to return the
        // connection or release its permit.
        let _guard = pool.get().await;
        assert_eq!(pool.free.lock().unwrap().len(), 0);
    }

    /// Checkout must block while the pool is exhausted and resume once a
    /// connection comes back.
    #[tokio::test]
    async fn blocks_until_a_connection_is_returned() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 1, None).unwrap();

        let held = pool.get().await;
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(50), pool.get())
                .await
                .is_err(),
            "checkout succeeded while the only connection was held"
        );
        drop(held);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(500), pool.get())
                .await
                .is_ok(),
            "checkout did not resume after the connection was returned"
        );
    }

    /// Read connections must be able to read what the writer wrote.
    #[tokio::test]
    async fn pooled_connection_reads_writer_data() {
        let (_dir, path) = seeded_db();
        let writer = Store::open(&path).unwrap();
        writer.set_meta("probe_key", "probe_value").unwrap();

        let pool = ReadPool::open(&path, 2, None).unwrap();
        let reader = pool.get().await;
        assert_eq!(
            reader.get_meta("probe_key").unwrap().as_deref(),
            Some("probe_value")
        );
    }

    /// Regression guard for a shipped bug: routing orchestration through a
    /// pooled connection made its LLM-cache write fail, and the call site
    /// discarded the error, so the cache never populated and every
    /// intelligence-enabled search re-ran the orchestrator. The write must
    /// return Err rather than appear to succeed.
    #[tokio::test]
    async fn pooled_connection_cannot_write_the_llm_cache() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 1, None).unwrap();
        let reader = pool.get().await;

        assert!(
            reader.set_llm_cache("hash", "{}", "orchestrator").is_err(),
            "read-only connection reported a successful llm_cache write"
        );

        // And the writer must still be able to, so the fix is to route the
        // orchestration phase through the writable store, not to drop caching.
        let writer = Store::open(&path).unwrap();
        writer.set_llm_cache("hash", "{}", "orchestrator").unwrap();
        assert_eq!(writer.get_llm_cache("hash").unwrap().as_deref(), Some("{}"));
    }

    /// The connections are read-only, so a stray write fails loudly instead of
    /// silently corrupting state behind the writer's back.
    #[tokio::test]
    async fn pooled_connection_rejects_writes() {
        let (_dir, path) = seeded_db();
        let pool = ReadPool::open(&path, 1, None).unwrap();
        let reader = pool.get().await;
        assert!(
            reader.set_meta("nope", "nope").is_err(),
            "read-only connection accepted a write"
        );
    }
}
