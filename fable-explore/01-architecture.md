# engraph architecture — as read from source (v1.7.2)

Read 2026-07-10 from a fresh clone of devwhodevs/engraph at f9a95bc (v1.7.2 + one
test commit). 25.7K lines of Rust, single binary, flat module layout.

## Stack

- **Storage:** one SQLite file (`~/.engraph/engraph.db`), WAL mode, busy_timeout 5s.
  Tables: `files` (path, content_hash, docid, tags, note_date, created_by),
  `chunks` (text + heading + offsets), `chunks_fts` (FTS5/BM25), `chunks_vec`
  (sqlite-vec vec0 virtual table, cosine, float[256]), `edges` (directed wikilink +
  mention graph), `tag_registry`, `folder_centroids`, `llm_cache`, `cli_events`,
  `tombstones`, `placement_corrections`.
- **Inference:** llama.cpp via `llama-cpp-2`. EmbeddingGemma-300M GGUF (256-dim after
  truncation, asymmetric `search_query:`/`search_document:` prefixes), Metal on macOS.
  Optional intelligence models (Qwen3-0.6B orchestrator + Qwen3-Reranker-0.6B) —
  **disabled in the live config** (and measured net-negative in the 2026-06-05 harness).
- **Servers:** `engraph serve` = MCP stdio (rmcp) + optional axum HTTP. Also a plain CLI.

## Live deployment on this machine

- Vault: `/Users/d/Documents/SecondBrain`. Index: 314 files, 11,251 chunks, 373 edges,
  60MB db + 20MB WAL. intelligence=false, mining_on_index=false.
- **10+ concurrent `engraph serve` processes observed** (one writable per Claude Code
  session, ~8 read-only spawned by Codex sessions), each holding ~350-460MB RSS.

## The query path (what a search actually does)

`search_with_intelligence()` in search.rs:

1. **Orchestrate.** intelligence=false → `heuristic_orchestrate()`: regex/pattern
   intent classification (Exact / Conceptual / Relationship / Exploratory / Temporal),
   1 expansion (the query itself), optional date range. No LLM. Effectively free.
2. **Per expansion, two lanes:**
   - **Semantic:** `embed_one(query)` → llama.cpp encode → `chunks_vec` KNN
     (`k = top_n*3`), then per-hit `get_chunk_by_vector_id` + `get_file_by_id`
     (two point SELECTs per hit), best-chunk-per-file dedup.
   - **FTS:** FTS5 MATCH (`top_n*3`), again per-hit `get_file_by_id`.
3. **Graph lane:** seeds = union(semantic, fts) (+temporal candidates if date query).
   For each seed: `get_neighbors(seed, 2 hops)` (both directions); for each neighbor:
   **per-query-term `file_contains_term` FTS5 probe** (relevance filter), tag-overlap
   fallback query on miss. Decay 0.8/0.5 by hop. Cap 20.
4. **RRF fusion pass 1** (semantic/fts/graph, k=60, intent-adaptive lane weights).
5. **Reranker lane** (off here) would score top-30; **temporal lane** (date queries
   only) scores candidates by date proximity. Final 3/4/5-lane RRF → top_n.

Key structural fact: with intelligence off the pipeline is
**1 query embedding + 1 vec scan + 1 FTS query + O(seeds × neighbors × terms) SQLite
probes + pure-Rust fusion**. Small index (11K vectors ≈ 11MB) → the brute-force KNN
scan is _not_ obviously the bottleneck at this scale.

## The write/index path

- `run_index` / `run_index_shared`: walk vault (`ignore` crate), SHA-256 every
  candidate file, diff vs stored hashes, re-chunk changed files (break-point scoring),
  `embed_batch` chunks, insert into chunks + FTS + vec + rebuild edges for affected
  files, recompute folder centroids.
- **`embed_batch` is a lie:** it loops texts sequentially, and every single text goes
  through `embed_text`, which **creates and destroys a fresh LlamaContext per call**
  (`n_ubatch=max(tokens,512)`) — KV-cache alloc + Metal pipeline setup for every chunk.
  11,251 chunks indexed = 11,251 context builds. Same cost on every watcher-triggered
  re-index and once per query.

## Server + watcher topology (the multiplication)

`run_serve()` (serve.rs:980-1100):

- Loads its own LlamaEmbed (300MB GGUF + Metal init) — per process.
- Runs `verify_index_integrity` + temp-file cleanup.
- **Spawns the file watcher unconditionally — `--read-only` only disables write
  *tools*** (serve.rs:1079 runs before any read_only gating; watcher.rs never sees
  the flag). Each watcher:
  - does **startup reconciliation** (`run_index_shared`) holding both the store and
    embedder async Mutexes — MCP tool calls block behind it;
  - on every debounced vault event, re-chunks + re-embeds + **writes** the shared DB
    (index_file/remove_file/rename_file + edge rebuild pass);
  - FullRescan events re-index the world while holding the locks (watcher.rs:617
    comments this "blocks MCP tool calls but is acceptable").

With N live servers this means: N model loads, N startup reconciliations, and **every
vault file save is re-embedded and re-written N times by N racing writers** on one
SQLite file (busy_timeout 5s). This matches the observed err-517 SQLITE_BUSY_SNAPSHOT
lock storms and MCP timeouts in the ops memory.

## Other observations (pre-measurement)

- `store.rs` uses `.prepare()` 46 times, `prepare_cached` **zero** times — every
  store call re-parses its SQL. Cheap individually; multiplied inside the graph lane's
  per-neighbor-per-term probes.
- CLI `engraph search` loads the embed model fresh on every invocation
  (search.rs:413) — model load likely dominates single CLI searches.
- `run_status` hardcodes `model: "all-MiniLM-L6-v2"` (search.rs:500) — the known
  lying status output.
- Tombstone param of `search_vec` is always the empty set in the live path (search.rs:126)
  — dead complexity retained from a pre-sqlite-vec design.
- Graph relevance filter cost scales seeds×neighbors×terms, each an FTS5 MATCH —
  fine at 373 edges, quadratic-feeling at real graph density.

## Hypotheses to test (ranked, falsifiable)

- **H1 — fleet multiplication, not per-query math, is the systemic burn.** N serve
  processes × (model load + startup reconciliation + per-save re-embedding + lock
  contention) dominates machine-level CPU. Test: measure one server's spawn cost +
  per-save cost; multiply by observed fleet; compare against per-query cost.
- **H2 — per-call LlamaContext creation dominates single-embed latency.** Context
  setup ≥ actual encode for short texts. Test: time embed_one broken into context
  creation vs encode (instrumented build); fix = reuse context (or one context per
  batch), verify identical vectors.
- **H3 — CLI search cost is ~all model load.** Test: time model load vs the rest.
- **H4 — the query path minus embedding is milliseconds** (vec scan 11K×256 + FTS +
  graph probes + RRF). Test: per-stage timings. If true, per-query "CPU hungry" is
  really H2 + H1, and sqlite-vec brute force doesn't matter until the index is ~10×
  bigger.
- **H5 — startup reconciliation + watcher FullRescan holding the embedder/store locks
  is the "timeout" mechanism** (not slow queries). Test: measure lock-hold durations
  around reconciliation with a warm index; simulate tool call during it.
