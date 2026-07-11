# Web research notes

Status: two background research agents (retrieval-perf practice, interactive
explainer craft) ran long and never returned reports; rather than block, the
load-bearing questions were verified inline. Findings below are only what was
actually checked against sources. The explainer-craft research remains open —
the concept doc (06) leans on established patterns (stepper beats scroll-jack,
reduced-motion parity, build-time layout) that should be validated against the
Ciechanowski/samwho/Red Blob corpus before build.

## llama.cpp embedding practice (verified)

- The canonical llama.cpp embedding example uses **one context, clearing the
  KV cache between batches** rather than re-creating contexts — exactly the
  shape of our Fix D. Sources:
  [embedding.cpp example](https://github.com/ggml-org/llama.cpp/blob/master/examples/embedding/embedding.cpp),
  [discussion #7712 (compute embeddings tutorial)](https://github.com/ggml-org/llama.cpp/discussions/7712).
- Throughput at scale comes from **packing multiple sequences into one decode
  call** (`--parallel N` + large `--batch-size`; `--ubatch-size` sized to the
  longest sequence, default 512). Server-side continuous batching is reported
  up to ~3.2× vs sequential. This validates P6 (multi-sequence batch encode)
  as the indexing lever. Sources:
  [llama-server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
  [llama-embedding manpage](https://manpages.debian.org/testing/llama.cpp-examples/llama-embedding.1.en.html),
  [batching tuning writeup](https://promptsicle.com/tips/boosting-llama-server-performance-with-batch-settings/),
  [maintainer discussion #21112](https://github.com/ggml-org/llama.cpp/discussions/21112).

## sqlite-vec scaling reality (verified)

- Official v0.1.0 benchmarks (Alex Garcia): 100K vectors at dims ≤1024 answer
  KNN in **<75ms**; 1M × 128-dim (SIFT1M) in ~17-33ms depending on mode —
  competitive with Faiss/DuckDB. Our measured 35ms median for **12K × 256-dim**
  is therefore high for the library's own baseline: consistent with the alpha
  version engraph pins (0.1.8-alpha) plus per-row virtual-table overhead and
  ambient load. Sources:
  [sqlite-vec stable release post](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html),
  [state of vector search in SQLite](https://marcobambini.substack.com/p/the-state-of-vector-search-in-sqlite).
- Practical ceiling guidance: brute force is comfortable to ~100K vectors
  (dim ≤1024); binary quantization extends reach dramatically; no ANN in
  sqlite-vec yet. Confirms both P2's premise (in-memory is trivially fine at
  our scale) and the "revisit at ~500K chunks" line in the proposals.
- Community comparisons (e.g. sqlite-vector vs sqlite-vec, 100K × 384-dim on
  M1 Pro) show quantization delivering ~3× query speedups — the upgrade path
  if the vault ever outgrows in-memory scanning.
  [Comparison writeup](https://marcobambini.substack.com/p/the-state-of-vector-search-in-sqlite).

## Implications folded back into this session

1. Fix D matches upstream's own canonical pattern (context reuse + KV clear) —
   defensible in an upstream PR.
2. P6 (multi-sequence batching) is the documented, measured path to indexing
   throughput; our 5.6%-CPU-utilization reindex is the local proof of need.
3. P2 (in-memory scan at 12K vectors) is conservative relative to what even
   sqlite-vec claims to handle; the motivation is our measured per-call
   overhead and tail, not fundamental scaling.
4. Nothing found contradicting the CPU-for-short-queries observation (P4);
   worth a quiet-machine re-measure after P3 before deciding.

## Still open (would have been the agents' deliverables)

- Survey of retrieval/RAG interactive explainers (prior art scan).
- Ciechanowski / samwho / Red Blob "how I build explainers" craft notes.
- Cross-encoder cost/benefit literature for personal-corpus scale.
