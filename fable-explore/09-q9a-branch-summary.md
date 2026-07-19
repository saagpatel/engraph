# Q9a branch summary: read pool, GPU gate, and four refuted premises

Branch `fable/q9a-read-pool`, 7 commits on top of `1f1b205`. 513 tests, `fmt` /
`clippy -D warnings` / `test` all green. Nothing pushed. The live `~/.engraph`
index was checksum-verified unchanged before and after every experiment.

This document exists so the merge decision can be made on evidence rather than
on commit subjects, because two of the headline numbers are weaker than they
look and one change was built and then deliberately thrown away.

## The short version

Concurrent search was fully serialized. It is now roughly 1.4x on the
intelligence-off path and, more importantly, no longer *worse* than sequential
on the intelligence-on path. Along the way a silent regression introduced by
this same branch was found and fixed, and it was worth more than the
concurrency work: it cut intelligence-on latency from 8786ms to 3475ms.

## What the review asked for, and why it was wrong

The operator review said search held four locks across the whole pipeline and
that this was the fleet-size failure mode. Narrowing three of them would have
measured as exactly zero.

The store guard is taken before the pipeline and every phase needs it, so
requests queued on the store no matter what happened to the other three. The
actual serializer was `Store { conn: Connection }` (`store.rs:153`) behind an
`Arc<Mutex<Store>>`: a rusqlite `Connection` is not `Sync`, so a single one
cannot serve concurrent readers. The database was already in WAL mode. The code
simply never opened a second connection.

## What shipped

| Commit | What |
|---|---|
| `3e8705e` | `ENGRAPH_DATA_DIR` override. Prerequisite: `data_dir()` was hardcoded, so nothing could be benchmarked without mutating the live index. |
| `f34305f` | `ReadPool` of read-only connections, plus splitting `search_with_intelligence` into `prepare_query` / `search_prepared` so the exclusive embedder is released before store work. |
| `7467e42` | Pool size becomes a knob; default stays 4 after sweeping. |
| `6c73db7` | Orchestration cache regression fix. See below. |
| `7cea1a1` | `GpuGate`: admission control for GPU model work. |
| `4a9e3d9` | Poison-recovery fix in the pool, from code review. |
| `b02dd73` | `exclude` honors `*.ext` patterns instead of matching them literally. |

Both the read pool and the embedder split were required. Either alone measures
as nothing. The same two-phase pattern had to be applied to `serve.rs` *and*
`http.rs`, which carried duplicate copies of the lock code; measuring only one
would have shown no change and wrongly discredited the pool.

## Measurements

All numbers are `bench/concurrency.mjs` (committed) against a 300-note synthetic
index, first run after startup discarded.

Intelligence off, 8 concurrent, five runs:

```
baseline              0.98  0.98  1.05  1.08  1.09    median 1.05x
pool, no shared cache 1.12  1.72  1.25  1.26  1.59    median 1.26x
pool + shared cache   1.85  1.48  1.47  1.63  1.44    median 1.48x
```

Ranges do not overlap against baseline, so the gain is real.

Intelligence on, 4 concurrent, three runs:

```
ungated   1.13  0.83  0.78    median 0.83, two runs below 1.0
GPU gated 2.22  1.13  1.08    median 1.13, none below 1.0
```

Across all six ungated intelligence-on runs collected, four came in below 1.0,
meaning concurrency was losing to sequential. Gated: zero. At n=3 this is
directional rather than conclusive, but the gate wins on min, median, and worst
case, and it removes the regime where adding load reduces throughput.

## The regression this branch introduced and then fixed

`f34305f` routed orchestration through a read-only pooled connection.
Orchestration writes its result to the LLM cache, so every one of those writes
failed, and the call site discarded the error with `let _ =`. The cache never
populated and every intelligence-enabled search re-ran the orchestrator forever.

Nothing surfaced it. It was found by accident: sweeping the reranker candidate
count to test whether reranking dominated latency showed 3 candidates was no
faster than 30, which made no sense until `SELECT count(*) FROM llm_cache`
returned 0.

Fixed in `6c73db7`. Phase 1 uses the writable store, which costs nothing between
searches because the embedder mutex already serializes that phase. Sequential
per-request latency went 8786ms to 3475ms. The cache write now logs on failure.
A regression test asserts a pooled connection *fails* that write rather than
appearing to succeed.

## Premises tested and refuted. Do not rebuild these.

1. **Narrowing the four locks.** Refuted before building. The store is needed by
   every phase, so three of the four were irrelevant to concurrency.
2. **Pool starvation.** A phase probe showed 147ms of a 217ms retrieve phase
   spent waiting for a connection, which looked conclusive. Raising the pool made
   it worse: medians 2 -> 1.20x, 4 -> 1.45x, 8 -> 1.20x, 16 -> 1.12x. Extra
   readers stop queueing on the mutex and contend for CPU instead.
3. **Debug-build overhead.** Release shows the same shape (4 -> 1.31x,
   8 -> 1.12x). `spawn_blocking` was never the limiter either; the machine has
   14 cores.
4. **Narrowing the GPU gate to the rerank call only.** This was a valid code
   review finding, and it was built: `search_prepared` split a third time into
   retrieve / rerank / fuse, with the gate covering only the rerank and no store
   connection held across it. 508 tests passed unmodified, so it was
   behavior-preserving. It did not pay. Intelligence-on median 1.12x versus
   1.13x for the wide gate, indistinguishable, because when intelligence is on
   the rerank *is* the request and freeing retrieval to overlap buys nothing.
   Intelligence-off showed a similar median but a worse floor (0.54x), plausibly
   because the split doubles pool checkouts per search. **Reverted.**
5. **Q8, duplicate vector storage.** Vectors do live in both `chunks.vector`
   BLOBs and the vec0 table, about 11MB of the 60MB live database. They are not
   redundant. `get_all_vectors` (`store.rs:628`) reads the BLOBs, and that feeds
   `VectorCache`, the fast in-memory serve path; vec0 serves CLI one-shot stores
   that never enable the cache. Deleting the BLOBs would load zero vectors and
   silently drop serve mode onto the slow path.

## Caveats that should affect the merge decision

- **Everything is measured on a 300-note synthetic index.** The real vault is
  about 12K chunks, where the store-bound share of a search is larger and
  pooling would likely matter more. The pool's value at true scale is projected,
  not measured.
- **It could not be measured at true scale** because every file under the
  SecondBrain vault is currently an iCloud dataless file. Content reads hang;
  `ls` and `stat` return instantly, which is why a naive probe using `wc -c`
  reports success. A reindex would hang the same way.
- **Intelligence-on is safe, not fast.** The gate removes the harmful regime. It
  does not make concurrent intelligence-enabled search meaningfully faster, and
  the reranker remains a single exclusive model.
- `HANDOFF.md` at the repo root is untracked and stale from an earlier session.
  It predates this work and should be deleted rather than trusted.

## Open decisions for the operator

1. **Merge as-is?** The intelligence-off win is solid. The intelligence-on story
   is "no longer harmful", which is honest but less exciting than the commit
   subjects imply.
2. **`globset` dependency.** `exclude` now supports `dir/`, `*.ext`, and
   substring, and warns on syntax it cannot honor. Real glob support needs
   `globset`, which was not added because dependency installs require operator
   approval.
3. **Q8 consolidation.** Removing the duplicate storage means choosing one
   source of truth: either the vector cache reads from vec0, or vec0 is dropped
   for serve-mode stores. Worth about 11MB and a design decision.
4. **Q1 remains untouched.** `intelligence` is still a single boolean, so a
   rerank-only configuration cannot be tested. That limits any future work on
   the reranker, which is now known to dominate intelligence-on latency.
