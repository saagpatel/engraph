# Where engraph's CPU (and wall time) actually goes — measured

Date: 2026-07-10/11. Machine: M-series Mac, ambient load present (10+ live
`engraph serve` processes, Ollama resident, this session's own agents). All
benches: engraph v1.7.2 built from source with debug symbols
(`CARGO_PROFILE_RELEASE_DEBUG=true cargo build --release --locked`).
Harnesses live in `examples/fable_*.rs` on branch `fable/perf-exploration`;
all read-path benches ran read-only against the live `~/.engraph` data dir
(same operations as a normal CLI search).

**Caveat, stated up front:** this machine is a noisy lab. Every number below is
either (a) a median/min across repeats, (b) a stack-sample attribution, or (c) a
mechanism confirmed by a query plan — not a single lucky timing. Numbers marked
"quiet" were taken in a low-load window; spreads are reported where they matter.

## Headline: a warm-server search spends its time like this

Per-stage timing of the real pipeline (`fable_bench`, intelligence off,
top_n=5, live index of 314 files / ~12K chunks), typical multi-word query:

| Stage | Typical | Range seen | Verdict |
|---|---|---|---|
| heuristic orchestrate | <10µs | — | free |
| **query embedding** | **250ms-1.7s** | 12ms floor (quiet, tight loop) | **dominant, wildly variable** |
| **sqlite-vec KNN** (12K × 256-d) | **~35ms median** | 15-336ms | 7-10× slower than in-memory |
| semantic hydration (N+1 lookups) | 1-6ms | — | fine at this scale |
| FTS5 lane | 2-12ms | — | fine |
| **graph lane** | **90-385ms** | ~0 when no neighbors | **mechanism found: missing index** |
| RRF fusion | <0.4ms | — | free |
| **end-to-end** | **0.8-2.0s** | — | |

The CLI adds per-invocation startup on top: **~1.2s of failed HuggingFace
HTTP requests + 1.7-18s model load** (below). MCP servers pay that once per
process — but there are 10+ processes.

## Finding 1 — every startup makes 8 doomed network calls (~1.2s, every time)

`RUST_LOG=info engraph search ...` timeline shows `try_external_tokenizer`
(llm.rs) attempting `tokenizer.json` downloads from 4 candidate HF repos ×2
retries each: 404, 404, 401, 401, 401, 401, 401, 401 — then falling back to
the GGUF-embedded tokenizer (shimmytok). Measured 1.14-1.23s per invocation
on a healthy network. **The failure is never cached**, so every CLI call and
every serve spawn repeats it. On a degraded network these become TCP stalls —
a direct "sometimes it times out" mechanism. Kicker: the external tokenizer
is only used for `token_count` (chunk sizing); the embedding itself tokenizes
via llama.cpp's `str_to_token`. The query path burns this time for an
artifact it doesn't use.

## Finding 2 — embedding time is Metal wait, not compute; per-call context churn amplifies it

- Raw phases, fresh context per call (current behavior, `fable_embed_bench`,
  12-token query, quiet): ctx_create ~6-8ms, encode ~2-13ms, extract ~8-19ms →
  **~25ms total**.
- Real `LlamaEmbed::embed_one` tight loop (quiet): **11-12ms steady**.
- Same call inside the full pipeline or after idle gaps: **100ms-1.9s**.
- `/usr/bin/sample` attribution during a slow run: 1,275/1,454 embed samples in
  `llama_get_embeddings_seq → ggml_metal_synchronize →
  [_MTLCommandBuffer waitUntilCompleted] → __psynch_cvwait` — i.e. **waiting
  for the GPU**, with another 141 in per-call context creation
  (`ggml_backend_sched_reserve` graph planning).
- Controlled comparison, 30 reps, 300ms idle gap between embeds (ambient load):
  - GPU (n_gpu_layers default): median **91ms**, p90 177ms, min 34ms
  - CPU (n_gpu_layers=0): median **39ms**, p90 190ms, min 8.6ms
- Reused context, tight loop (quiet): **10-11ms stable** — the best observed
  configuration.

Interpretation: for 12-token query encodes, Metal offload buys nothing and
costs a fixed ~25-35ms submission+sync floor plus exposure to GPU queue
contention and power-state ramp (Ollama is resident on this machine; spikes
to 1.9s observed). The per-call context creation additionally pays llama.cpp
graph planning each time. This is the direct mechanism of "CPU-hungry and
slow": the process isn't computing, it's blocked in `waitUntilCompleted`
while wall-clock burns.

Indexing pays the same per-call context churn **once per chunk**
(`embed_batch` is a sequential loop over `embed_text`; no llama.cpp
multi-sequence batching). ~12K chunks indexed = ~12K context create/destroy
cycles.

## Finding 3 — the graph lane's cost is a missing index

`fable_graph_bench`, query "tauri nspanel tray": 6 seeds → 89 unique
neighbors visited (2 hops) → **125 FTS `MATCH` probes + 18 tag probes = 143
point queries** (the N+1 relevance filter), totaling ~46ms. But the real
`graph_expand` takes **380ms** — the difference is the result-conversion
tail: `get_best_chunk_for_file` per surviving result runs

```sql
SELECT heading, snippet FROM chunks WHERE file_id = ? ORDER BY token_count DESC LIMIT 1
-- QUERY PLAN: SCAN chunks + USE TEMP B-TREE FOR ORDER BY
```

**There is no index on `chunks(file_id)`.** Every call scans all ~12K chunk
rows — each dragging an inline ~1KB vector BLOB through the pager — and
sorts. ×20 results per query. A one-line index
(`CREATE INDEX idx_chunks_file_id ON chunks(file_id)`) should remove
~300ms/query on graph-heavy queries. (Indexes exist for files.docid and
edges.*; chunks got missed.)

## Finding 4 — sqlite-vec is 7-10× slower than a trivial in-memory scan, at identical results

`fable_vec_bench`, k=15 over 12,228 × 256-d vectors: sqlite-vec vec0
**15-336ms (median ~35ms)** vs in-memory scalar Rust brute force **3.5-9ms**
(no SIMD, includes full sort). Top-k sets identical (only near-tie order
differs, float-precision ties between adjacent chunks of the same file).
Overhead = vec0 virtual-table per-row dispatch + BLOB traversal. Bonus
finding: vectors are stored twice (chunks.vector BLOB + chunks_vec vec0) —
consistent (0 drift measured) but doubling write cost and file size, and the
inline BLOB in `chunks` is what makes Finding 3's table scans expensive.

## Finding 5 — model load: 1.7-18s per process, ×N processes

`LlamaEmbed::new` (300MB Q8 GGUF + Metal init): 1.7s warm page cache, 9-18s
cold. Every `engraph serve` (10+ observed live: one writable per Claude Code
session, ~8 read-only from Codex) pays this at spawn, plus Finding 1's 1.2s
network dance, plus a startup reconciliation walk. Every CLI `engraph search`
pays it too.

## Finding 7 — a full reindex runs at 5.6% CPU utilization

Full `index --rebuild` of the lab copy (314 files, 11,251 chunks), new binary
with the shared-context fix: **1998s wall / 114s user CPU = 5.6% utilization**.
The unpatched binary was killed at 1564s wall / 114.7s CPU, still embedding,
zero rows written (it buffers all embeddings and writes at the end — so a
45-minute job also gives no progress signal and no partial result). Under this
machine's ambient GPU load the old-vs-new *wall* race is inconclusive (both are
Metal-sync-bound); the controlled evidence for the shared-context fix is the
32-chunk parity bench (1442→945ms) and quiet-machine micro-bench (25ms→11ms
per embed). The 94%-waiting number is the case for true multi-sequence
batching (P6): one GPU sync per 64 chunks instead of one per chunk.

## Finding 6 (confirmed in source AND lab) —
read-only servers run writable watchers

`run_serve` spawns the file watcher unconditionally (serve.rs:1079);
`--read-only` only gates write *tools*. Watcher startup runs
`run_index_shared` reconciliation **while holding the store + embedder
mutexes** (watcher.rs:46-60) — MCP tool calls block behind it. Every vault
file save wakes N watchers; each re-chunks, re-embeds, and re-writes the
shared SQLite DB (busy_timeout=5s). N-way duplicated embedding work + write
lock storms; matches the historically observed err-517 SQLITE_BUSY_SNAPSHOT.

**Lab experiment (before fix):** 4 read-only servers, one file save → 4
independent "indexed changed file" passes (chunk + 5 embeds + write + edge
rebuild, per server), racing on one SQLite DB; the 5s busy_timeout silently
absorbed the contention at N=4. **After the fix (commit c6fc677):** 2
read-only + 1 writable server, one save → read-only logs show "watcher not
started" and zero index activity; exactly one index pass total, from the
writable server.

## What this rules OUT

- RRF fusion, FTS5, snippet hydration: microseconds to low ms. Not suspects.
- Heuristic orchestration: free. (Intelligence models are off; when on, they
  load 2 more GGUFs per process and add per-query LLM calls — separately
  already measured net-negative for quality in the 2026-06-05 harness.)
- Vector count (12K): nowhere near sqlite-vec's practical ceiling; the scan
  cost is overhead-per-row, not math.
- Dual-store drift: none found (12,228 = 12,228, 0 orphans both directions).

## Reproduction

```
cargo build --release --locked --examples   # with CARGO_PROFILE_RELEASE_DEBUG=true
./target/release/examples/fable_bench ~/.engraph 5 "query" ...
./target/release/examples/fable_embed_bench ~/.engraph/models 30 "query"   # FABLE_N_GPU_LAYERS, FABLE_SLEEP_MS
./target/release/examples/fable_embed_real ~/.engraph 12 "query"           # FABLE_SLEEP_MS, FABLE_SQLITE_WORK
./target/release/examples/fable_vec_bench ~/.engraph 8
./target/release/examples/fable_graph_bench ~/.engraph "query"
/usr/bin/sample <pid> 5 1 -f sample.txt     # stack attribution
```
