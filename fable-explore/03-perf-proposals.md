# Perf fixes — implemented on `fable/perf-exploration`, with proof

Every change here is a **proposal for review** — committed on the branch, nothing
pushed, live `~/.engraph` untouched (all mutation experiments ran in an isolated
lab copy under the session scratchpad). Full methodology + raw numbers:
[02-profiling.md](02-profiling.md).

## Shipped on the branch (4 commits)

### 1. `CREATE INDEX idx_chunks_file_id ON chunks(file_id)` — store.rs (8b92ef8)

- **Problem:** `get_best_chunk_for_file` (called ~20×/query by the graph lane)
  ran `SCAN chunks` + temp B-tree over ~12K rows whose inline vector BLOBs make
  every row ~1KB+. Measured 380ms real `graph_expand` vs ~46ms of actual probe
  work.
- **Fix:** one-line index in the schema batch (`IF NOT EXISTS` → free migration
  on next open; measured migration cost on the 60MB lab DB: **0.07s**).
  Also added `id ASC` tiebreak to the `ORDER BY` so the chosen chunk is
  deterministic regardless of access path.
- **Proof:** query plan flips to index search; CLI battery mean fell 6.4s → ~5.1s
  pre-network-fix, and the post-all-fixes CLI search runs **1.1-1.6s vs 6.4s
  baseline** (fixes 1+2 combined). 468/468 unit tests pass.

### 2. Tokenizer: cache → GGUF → network (was: network first, every time) — llm.rs (7886855)

- **Problem:** every process start (CLI search, serve spawn) made **8 doomed
  HTTPS requests** to HuggingFace (404/401 on all candidate repos, failure
  never cached), ~1.2s on a good network, unbounded on a bad one — then used
  the GGUF-embedded tokenizer anyway.
- **Fix:** try cached tokenizer.json first, then the GGUF-embedded tokenizer
  (the model file is guaranteed present at this point), network as true last
  resort. Behavior on this machine is identical (GGUF tokenizer was already
  what ran); first-run onboarding still downloads.
- **Proof:** `RUST_LOG=info` startup shows **0 network attempts** (was 8).
  Offline operation now actually offline.

### 3. Deterministic ranking under ties — fusion.rs, graph.rs, search.rs (b1fdec2)

- **Problem (discovered during parity testing):** the unpatched binary
  **disagrees with itself 6/10 runs** on identical query+index — HashMap
  iteration order fed tie-heavy unstable-in-effect sorts; RRF ranks,
  confidences, and even tail membership (graph lane truncates to 20 *after*
  a tie-random sort) were per-run random. Three different rank-5 files
  observed across 8 runs of one query.
- **Fix:** every ranking sort tiebreaks on `file_path` (file_id in graph);
  `merge_seeds` returns sorted seeds.
- **Proof:** patched binary reproduces **10/10**; set-level parity with the
  old binary is 9/10 with the single difference confined to a tie-tail slot
  the old binary itself randomized. This changes "what it returns" only in
  the sense that it stops being random.

### 4. One context per `embed_batch` (was: one per chunk) — llm.rs (7886855)

- **Problem:** `embed_batch` looped `embed_text`, creating + destroying a
  llama.cpp context (graph planning + Metal pipeline setup) per text. A full
  index = ~12K context cycles. Stack samples show context creation as a real
  component of embed cost (141/1454 samples) on top of Metal sync.
- **Fix:** tokenize all texts, create one context sized for the longest, KV
  clear between encodes.
- **Proof:** vectors **bitwise identical** (max_abs_diff = 0.0 across 32
  texts, `fable_batch_parity`); 32-chunk batch 1442ms → 945ms under ambient
  load (tight-loop measurements suggest ~2× when quiet). Full-reindex race:
  the OLD binary was killed after **26 min wall / 114s CPU (4% utilization),
  still embedding, zero rows written** — the per-chunk Metal-sync stall in
  its natural habitat. New-binary number pending below.
- **Honest limit:** context sharing removes creation cost (~10-15ms/chunk)
  but each encode still pays one Metal `waitUntilCompleted`. The follow-on
  win is true multi-sequence batching (P6).

## Proposed, not yet implemented (bigger surface, want your call)

### P1 — IMPLEMENTED (c6fc677): read-only servers no longer run writable watchers

`run_serve` spawns the file watcher unconditionally; `--read-only` gates only
write tools. Measured in the lab: 4 read-only servers, one file save → **4
independent chunk+embed+write+edge-rebuild passes**, racing on one SQLite DB
(the 5s busy_timeout absorbed the contention at N=4; at your live N=10+ it
historically surfaced as err-517 lock storms). Live machine today: 10+ serve
processes = every vault save costs ~10× the work and churns the same Metal
queue your queries wait on. The two problems compound.

Implemented: `--read-only` now skips the watcher + startup reconciliation;
read-only servers see updates via WAL on each query. **Verified in lab:**
2 read-only + 1 writable, one save → exactly one index pass (was N).
Caveat you should sign off on: a machine running ONLY read-only servers no
longer self-heals a stale index — worth adding a staleness warning at
startup as a follow-up. 468/468 tests pass.

### P2 — In-memory vector scan instead of (or in front of) sqlite-vec

12K × 256-d = 12MB. sqlite-vec vec0 KNN measured **15-336ms (median ~35ms)**;
naive in-process scan over the same vectors: **3.5-9ms, identical top-k sets**.
Load once at serve start (+refresh hook in the watcher/write path), keep
sqlite-vec for CLI one-shots. Kills the second-biggest query-path cost and its
ugly tail. Roughly ~60 lines + cache invalidation discipline. (Alternative:
upgrade sqlite-vec when its ANN lands upstream; brute-force-in-SQL overhead is
structural, not tunable.)

### P3 — Reuse the embed context for queries (actor thread)

`embed_one` still builds a context per query (~6-8ms quiet, and it's the
per-call Metal graph planning that inflates under load). `LlamaContext` is
`!Send`, which is exactly why the author went per-call — the clean fix is a
dedicated inference thread owning model+context, `mpsc` request/reply
(`EmbedModel` impl talks to the actor). Measured ceiling: reused-context
tight-loop embeds are **10-11ms stable** vs 25ms+ fresh, and the actor also
serializes GPU submissions (no more N contexts churning Metal). Medium diff
(~100 lines), high value for the MCP server path.

### P4 — Consider CPU inference for query-sized encodes

With idle gaps under ambient load: GPU median 91ms/p90 177ms vs CPU median
39ms (floor 8.6ms) for a 12-token encode. Metal buys nothing at query size and
exposes every query to GPU queue contention (Ollama lives here too) and
power-state ramp (spikes to 1.9s observed, all in `waitUntilCompleted`).
Config knob: `embed_gpu = "auto" | "always" | "indexing-only"`. Zero risk to
results (same model, same math). Worth re-measuring after P3 — context reuse
may make GPU competitive again at the floor.

### P6 — True multi-sequence batch encoding for indexing

Fix 4 shares one context but still encodes chunk-by-chunk: one GPU
round-trip (submit + `waitUntilCompleted`) per chunk. llama.cpp supports
packing multiple sequences into one `encode` call (`n_seq_max`); with
batch_size=64 that's one sync per 64 chunks instead of 64. Given the sync
wait dominates (full-reindex runs at 4-6% CPU — the process is parked on
the GPU), this is the order-of-magnitude lever for indexing throughput.
Requires per-sequence embedding extraction (`embeddings_seq_ith(i)`) and
a vector-parity check like `fable_batch_parity`. Medium diff, indexing
path only, zero effect on query behavior.

### P5 — Statement caching (`prepare_cached`)

store.rs has 46 `.prepare()` sites and zero `prepare_cached`. Individually
cheap; multiplied inside graph-lane loops (143 point probes/query measured).
Mechanical change, low risk, modest win — bundle with P2 rather than alone.

### Deliberately NOT proposed

- ANN index (HNSW etc.): pointless at 12K vectors; in-memory brute force is
  microseconds-capable. Revisit at ~500K chunks.
- Parallel (rayon) query lanes: lanes are already cheap once the above land;
  complexity not earned.
- Touching the reranker/orchestrator: intelligence is off and measured
  net-negative for this vault (2026-06-05 harness); nothing to optimize.
