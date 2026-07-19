//! A gate around GPU-backed model work.
//!
//! The embedder and the reranker are separate exclusive models that share one
//! Metal device. Locking them independently lets one request's embed overlap
//! another's rerank, and measurement showed that overlap is not free: with
//! intelligence enabled, 4-way concurrency swung between 0.33x and 1.19x of
//! sequential, i.e. sometimes materially worse than doing nothing in parallel.
//!
//! This gate makes that policy explicit and adjustable rather than emergent
//! from where the mutexes happen to sit. One permit means GPU work never
//! overlaps; raising it restores the previous free-for-all.

use std::sync::Arc;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

/// Default number of concurrent GPU operations.
///
/// One: the models already serialize individually, and letting different model
/// types interleave on a single device measured worse than not.
pub const DEFAULT_GPU_PERMITS: usize = 1;

/// Environment override, so the policy can be swept against a real workload.
pub const GPU_PERMITS_ENV: &str = "ENGRAPH_GPU_PERMITS";

/// Configured permit count.
pub fn configured_permits() -> usize {
    parse_permits(std::env::var(GPU_PERMITS_ENV).ok().as_deref())
}

fn parse_permits(raw: Option<&str>) -> usize {
    raw.and_then(|v| v.trim().parse::<usize>().ok())
        .filter(|n| *n > 0)
        .unwrap_or(DEFAULT_GPU_PERMITS)
}

/// Admission control for GPU-backed work.
pub struct GpuGate {
    permits: Arc<Semaphore>,
}

impl GpuGate {
    pub fn new(permits: usize) -> Self {
        Self {
            permits: Arc::new(Semaphore::new(permits.max(1))),
        }
    }

    /// Number of operations allowed to run at once.
    pub fn capacity(&self) -> usize {
        self.permits.available_permits()
    }

    /// Wait for permission to run GPU work. The permit is released on drop.
    pub async fn enter(&self) -> OwnedSemaphorePermit {
        self.permits
            .clone()
            .acquire_owned()
            .await
            .expect("gpu gate semaphore is never closed")
    }
}

impl Default for GpuGate {
    fn default() -> Self {
        Self::new(configured_permits())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn permits_fall_back_on_missing_or_invalid_values() {
        assert_eq!(parse_permits(None), DEFAULT_GPU_PERMITS);
        assert_eq!(parse_permits(Some("nope")), DEFAULT_GPU_PERMITS);
        // Zero permits would deadlock every request.
        assert_eq!(parse_permits(Some("0")), DEFAULT_GPU_PERMITS);
    }

    #[test]
    fn permits_honor_a_valid_override() {
        assert_eq!(parse_permits(Some("4")), 4);
    }

    /// The whole point: a second entrant waits while the first holds a permit.
    #[tokio::test]
    async fn single_permit_excludes_a_second_entrant() {
        let gate = GpuGate::new(1);
        let held = gate.enter().await;

        assert!(
            tokio::time::timeout(Duration::from_millis(50), gate.enter())
                .await
                .is_err(),
            "second entrant got in while a permit was held"
        );

        drop(held);
        assert!(
            tokio::time::timeout(Duration::from_millis(500), gate.enter())
                .await
                .is_ok(),
            "permit was not released on drop"
        );
    }

    /// Raising the count restores overlap, which is what makes the A/B possible.
    #[tokio::test]
    async fn extra_permits_allow_overlap() {
        let gate = GpuGate::new(2);
        let _a = gate.enter().await;
        assert!(
            tokio::time::timeout(Duration::from_millis(50), gate.enter())
                .await
                .is_ok(),
            "two permits should admit two entrants"
        );
    }

    #[tokio::test]
    async fn zero_is_clamped_so_requests_cannot_deadlock() {
        let gate = GpuGate::new(0);
        assert!(
            tokio::time::timeout(Duration::from_millis(50), gate.enter())
                .await
                .is_ok(),
            "a zero-permit gate would block every request forever"
        );
    }
}
