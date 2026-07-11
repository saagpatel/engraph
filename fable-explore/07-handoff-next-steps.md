# Handoff — next steps, dispatch-ready

Written for a successor session (Codex / Opus / Sonnet — none of this needs
Fable). All context needed is on disk: INDEX.md → 01-06. Branch
`fable/perf-exploration` at `~/Projects/engraph`, 7 commits, 468/468 tests,
NOT pushed. Live `~/.engraph` untouched. Lab environment (safe to trash or
rebuild): session scratchpad `engraph-lab/` — rebuild recipe in 02 §Reproduction.

## Operator-gated (do NOT do without explicit instruction)

- Swap live brew binary → patched build (suggest: `brew unlink engraph` +
  symlink target/release, or a tap bump).
- Push branch / open upstream PRs to devwhodevs/engraph.
- Publish essay/explainer to saagarpatel.dev.
- Accept P1 semantics (read-only servers no longer self-heal stale indexes)
  — ship the staleness warning (task T4) alongside.

## T1 — P6: multi-sequence batch encode (highest value, medium difficulty)

Spec: in `LlamaEmbed::embed_batch` (src/llm.rs), pack up to `config.batch_size`
token sequences into ONE `LlamaBatch` with distinct sequence ids and ONE
`ctx.encode()` call; extract per-sequence via `embeddings_seq_ith(i)`.
Context must be sized: n_ctx ≥ sum of packed tokens (or pack greedily up to
n_ctx), n_ubatch ≥ longest sequence. Reference: llama.cpp
examples/embedding/embedding.cpp (canonical multi-sequence pattern).
GATE (must pass): `fable_batch_parity` — max_abs_diff vs single-sequence
output ≤ 1e-6 (current fix achieved 0.0; small nonzero may appear from
batch-order float effects — if >1e-6, reject). Measure: full reindex wall
time in lab (current: 1998s / 5.6% CPU utilization; expect several-fold).

**2026-07-11 Codex attempt — BLOCKED after two gate failures.** The first
packed implementation built successfully but `fable_batch_parity` exited 1
with `Encode Error -1: n_tokens == 0`. The repair added the context's
`n_seq_max`, after which the same harness aborted with exit 134 on llama.cpp's
`GGML_ASSERT(cparams.n_ubatch >= n_tokens && "encoder requires n_ubatch >= n_tokens")`.
The installed llama.cpp encoder therefore requires `n_ubatch` to cover the
entire packed batch, not only the longest sequence as the task spec states.
The unaccepted T1 source changes were reverted; no T1 implementation commit
was made. A future retry should set `n_ubatch` to the packed token total (and
retain `n_seq_max`) before rerunning the parity gate.

## T2 — P2: in-memory vector scan for serve (medium)

Spec: on serve startup, load (vector_id, embedding) for all chunks into a
Vec (12MB at current scale; source column chunks.vector). search path uses
in-memory cosine scan (see examples/fable_vec_bench.rs for the exact loop —
measured 3.5-9ms vs sqlite-vec 15-336ms, identical top-k sets). Invalidate:
watcher/write pipeline refreshes affected ids after index_file/remove_file/
rename_file. CLI one-shots keep sqlite-vec (no cache warm cost).
GATE: top-k id sets identical to sqlite-vec on the 10-query battery
(scratchpad/parity/queries.txt pattern); serve-path query timing before/after.

## T3 — P5: prepare_cached sweep (mechanical, low risk)

Spec: replace `self.conn.prepare(` with `self.conn.prepare_cached(` in
src/store.rs for hot-path read queries only (search, graph, fts, chunk/file
point lookups — NOT schema/migration statements). 46 sites total; judgment
per site is trivial. GATE: cargo test --lib green; fable_graph_bench probe
timings not worse.

## T4 — staleness warning for read-only serve (small, pairs with P1)

Spec: in run_serve read_only branch, compare max(files.indexed_at) age vs
newest mtime under vault_path (walk_vault, cheap); eprintln a warning if
index is older. No writes. GATE: unit test + manual lab check.

## T5 — quality items Q4/Q5/Q6 (mechanical)

- Q4: FTS5 `tokenize='porter unicode61'` on chunks_fts + one-time FTS rebuild
  path (no re-embed). GATE: rebuilt FTS returns stemmed matches ("ranking"
  finds "rankings"); tests green.
- Q5: store embed model uri+dim in meta at index time; run_status reads it
  (kill the hardcoded "all-MiniLM-L6-v2" at src/search.rs:500).
- Q6: delete or port tests/integration.rs + tests/write_pipeline.rs (they
  import deleted modules `engraph::hnsw`/`engraph::embedder`; cannot compile).
  Preferred: golden-file search battery over a tiny fixture vault (catches
  nondeterminism-class regressions).

## T6 — explainer build (implementer + one taste pass)

Follow 06-public-material/explainer-concept-query-fanout.md exactly (8 beats,
cold open = run-it-twice). Synthetic vault data + build-time layout; vanilla
JS + SVG; stepper; reduced-motion + no-JS fallbacks per the concept doc.
Study blog.serghei.pl/posts/reciprocal-rank-fusion-explained/ before the
fusion beat. Site conventions: portfolio-index repo AGENTS.md + memory notes
(build order, palette rules). Final review pass = operator or a
taste-capable model.

## T7 — essay finalize

06-public-material/essay-draft-where-the-cpu-went.md is draft 1 with all
numbers verified against 02. Run /review-prose + stop-slop before publish;
keep zero em dashes; verify no vault-content leakage (currently clean).

## Deferred / do-not-do

- P3 (embed actor thread): superseded in priority by T1; revisit only if
  query-path embed latency still spikes after T1+T2 land and fleet load
  drops (P1 already shipped).
- P4 (CPU-for-queries knob): re-measure on a quiet machine first; single
  anecdotal web support only, local evidence suggestive not conclusive.
- ANN/quantization: not until ~500K chunks.
- Q1 (intelligence split) + rerank re-eval: needs the operator's
  retrieval-truth harness; coordinate with operator.

## Wrap-up chores for the successor session

- bridge-db: log_activity (caller per lane) summarizing branch state; this
  file is the canonical next-steps list.
- On any merge to a local main: keep commits as-is (they're already logical
  units); do not squash away the measurement citations in messages.
- Memory: `~/.claude/projects/-Users-d/memory/project_engraph_perf.md` is
  current as of 2026-07-11; update its status line when Tn items land.
