# Web research notes

Two background research agents (retrieval-perf practice; interactive-explainer
craft) delivered full cited reports; my own inline verification of the
load-bearing perf questions is folded in. Everything below survived the "did a
source actually say this" filter; flagged items are marked.

## Retrieval performance practice

### llama.cpp embedding (validates Fix D, P3, P6)

- **Slot architecture is the production norm:** llama-server allocates one
  context/KV pool at startup and reuses it across all requests (`--parallel`
  sizing) — never context-per-call. The canonical embedding example shares one
  context and clears KV between batches: exactly Fix D's shape, and P3's
  (actor thread) justification for the query path.
  Sources: [llama-server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
  [embedding.cpp](https://github.com/ggml-org/llama.cpp/blob/master/examples/embedding/embedding.cpp),
  [discussion #7712](https://github.com/ggml-org/llama.cpp/discussions/7712).
- **Throughput = multi-sequence packing:** many sequences per decode call
  (`--parallel N`, large `--batch-size`, `--ubatch-size` ≥ longest sequence);
  continuous batching reported up to ~3.2× vs sequential. This is P6.
  Sources: [llama-embedding manpage](https://manpages.debian.org/testing/llama.cpp-examples/llama-embedding.1.en.html),
  [batch tuning writeup](https://promptsicle.com/tips/boosting-llama-server-performance-with-batch-settings/),
  [discussion #21112](https://github.com/ggml-org/llama.cpp/discussions/21112).
- **Metal sync waits for small work are a named, first-party phenomenon:**
  Apple's Metal guidance — "small amounts of work can lead to more time spent
  waiting than working"; recommends batching encoders into fewer command
  buffers. Kernel-launch + CPU-GPU sync overhead exceeding compute time for
  tiny workloads is corroborated in the general literature. This is Finding 2
  with a citation trail.
  Sources: [Apple Metal tech talk](https://developer.apple.com/videos/play/tech-talks/10580/),
  [The Framework Tax](https://arxiv.org/pdf/2302.06117),
  [CPU-induced slowdowns in multi-GPU LLM inference](https://arxiv.org/html/2603.22774v1).
- Apple-Silicon tuning notes: over-threading past P-core count hurts; for
  GPU-offloaded models minimal CPU threads is the vendor recommendation.
  (Directional; re-measure locally before applying — P4's quiet-machine
  re-measure covers this.)
- **CPU-beats-Metal for small models:** single anecdotal GitHub thread only —
  treat as hypothesis. Our local measurement (CPU floor 8.6ms vs GPU floor
  34ms for a 12-token embed) is the actual evidence on this hardware.

### sqlite-vec (validates P2's premise, bounds it honestly)

- Author benchmarks: 100K vectors, dims ≤1024 → **<75ms** KNN; 1M × 128-dim
  in 17-33ms. Our 12K × 256-d workload is deep inside the comfortable
  envelope, so vec0 is a *secondary* cost, not the headline. But the perf
  agent's own falsifiable threshold ("if the MATCH query is <10ms, look
  elsewhere") cuts the other way on our box: measured 35ms median / 336ms
  tail — above threshold, so P2 (in-memory scan: 3.5-9ms measured, identical
  results) keeps its seat.
  Sources: [sqlite-vec v0.1.0 release](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html)
  (numbers reached secondhand via search synthesis — confirm exact figures
  before citing publicly),
  [state of vector search in SQLite](https://marcobambini.substack.com/p/the-state-of-vector-search-in-sqlite).
- vec0 **metadata/auxiliary columns** add per-row comparison / JOIN overhead
  in the KNN path — a checkable overhead candidate. **Checked: ruled out for
  engraph** (chunks_vec is bare: `embedding float[256]` only). Our overhead is
  per-row virtual-table dispatch + the pinned alpha version.
  Sources: [metadata release post](https://alexgarcia.xyz/blog/2024/sqlite-vec-metadata-release/index.html),
  [aux columns issue #121](https://github.com/asg017/sqlite-vec/issues/121).
- No ANN in sqlite-vec (by design; Vec1 successor moving toward IVFADC+OPQ
  and partition keys). Binary quantization is the documented reach-extender.
  Matches the proposals' "revisit at ~500K chunks" line.

### Hybrid search / RRF engineering (validates the graph-lane fixes, P5)

- RRF fusion is trivially cheap; k=60 default traces to Cormack/Clarke/
  Büttcher 2009, tunable 40-80 *only with labeled data*. Engineering effort
  belongs in chunking, filtering, and eval — consistent with leaving the
  fusion core alone (04 §left-alone).
  Source: [Elastic on weighted RRF](https://www.elastic.co/search-labs/blog/weighted-reciprocal-rank-fusion-rrf).
- **Retrieve-then-hydrate** (IDs first, one batched fetch) is the standard
  N+1 cure; `rusqlite::prepare_cached` (LRU) is the idiomatic statement-cache.
  Both independently recommended; both were already measured locally (143
  probes/query) and proposed (P5 + batched hydration in P2's orbit).
- **Reranking at personal scale:** cross-encoders add nothing or hurt when
  first-stage precision is already high, vocabulary is single-author-narrow,
  or query expansion upstream injects noise the reranker then reinforces —
  the last being literally the 2026-06-05 harness finding, now with SIGIR
  2024 backing. Caveat worth keeping: a null result needs an adequately
  powered labeled set (the existing harness qualifies).
  Sources: [SIGIR 2024 — query expansion vs cross-encoders](https://arxiv.org/pdf/2311.09175),
  [When more documents hurt RAG](https://arxiv.org/pdf/2606.11350).

### Profiling Rust+FFI on macOS (retroactively confirms method)

samply > dtrace-based cargo-flamegraph on modern macOS; `debug = true` +
frame pointers in the release profile are what make llama.cpp FFI frames
resolve (without them the Metal-wait mechanism would have been an opaque
blob). Instruments/xctrace as fallback for stubborn mixed stacks — moot here,
Xcode isn't installed; `/usr/bin/sample` + debuginfo did the job.
Sources: [Rust profiling on macOS guide](https://blog.infinilabs.com/posts/2024/benchmarking-and-profiling-rust-applications-on-macos-a-practical-guide/),
[Rust Performance Book](https://nnethercote.github.io/perf-book/profiling.html).

## Interactive explainer craft

### What the masters actually do

- **Sam Rose (samwho.dev)** — the closest genre neighbor. 1-3 months per
  piece, bottom-up (simplest example first), trades realism for legibility
  and *says so*, reports p50/p95/p99 from real simulations, opens with a
  counterintuitive hook (round-robin: best median, worst tail), closes by
  naming what he left out and why, ends with a free-play sandbox.
  Sources: [load-balancing piece](https://samwho.dev/load-balancing/),
  [interview on technical blogging](https://writethatblog.substack.com/p/sam-rose-on-technical-blogging).
- **distill.pub research** ("Communicating with Interactive Articles"):
  animation empirically wins for **state transitions, causality, uncertainty,
  narrative** — not raw data density; **segmentation beats one continuous
  animation** for learning (direct support for a stepper); personalization
  (reader's own query) is a proven engagement lever; accessibility for
  interactive articles is an *open research problem* — an honest static
  fallback is craft, not failure.
  Source: [distill.pub](https://distill.pub/2020/communicating-with-interactive-articles/).
- **Nicky Case / explorable-explanations movement:** guided freedom (naked
  sandboxes are a named failure mode), teach sub-mechanics in isolation
  before combining, gate/pace content, **sandbox as payoff at the END**,
  question-first framing.
  Sources: [explorable explanations](https://blog.ncase.me/explorable-explanations/),
  [how I make one](https://blog.ncase.me/how-i-make-an-explorable-explanation/),
  [Bret Victor's original](https://worrydream.com/ExplorableExplanations/).
- **Julia Evans:** no vague metaphors; short, precise, *true* statements;
  start from a concrete scene. **Josh Comeau:** give readers literal controls
  over the physics; randomize durations to avoid robotic repetition.
- **Ciechanowski:** hand-rolled, framework-free, months per piece — inferred
  from third-party commentary only (his own "how" statement wasn't reachable
  this pass; flagged).

### Prior art for retrieval visualization — and the whitespace

Embedding projectors (Google's, WizMap), live-editable PageRank graphs, RAG
step-through debuggers (RAG Playground, RAGGY), and — closest hit — **a live
RRF simulation with a k-slider** ([Serghei's blog](https://blog.serghei.pl/posts/reciprocal-rank-fusion-explained/)):
two ranked lists merge, agreement climbs as k grows. Study it before building
the fusion scene. BM25 has *no* good interactive explainer found (gap).
**Nobody has built an essay-grade narrative walking a whole hybrid pipeline
(keyword + semantic + graph + fusion) end to end.** That's the whitespace this
piece occupies.

### Implementation consensus (adopted into the concept doc)

- Discrete step-triggers as the spine (Scrollama-style detection or plain
  click-advance); steps double as the `prefers-reduced-motion` path. Motion
  carries information here, so reduced-motion gets a stepped alternative, not
  nothing (WCAG 2.3.3).
- Hand-rolled vanilla JS over d3 (~70KB for data-binding this piece doesn't
  need); SVG is sufficient at dozens-to-hundreds of nodes — profile before
  adding a Canvas layer (hybrid Canvas-field + SVG-overlay only if particle
  counts demand it).
- No-JS fallback = static annotated diagram + full prose (unsolved
  industry-wide; a good static version is itself a point of craft).

### Design moves adopted from the reports

1. **Cold open on the nondeterminism hook**: reader hits "search" twice on
   the same query before any explanation; order shifts; the piece exists to
   answer why. (Uncertainty/causality is where animation earns its keep, and
   it's the Sam-Rose-style "wait, what?" hook.)
2. **Ties as a first-class animated event**: two candidates land on the same
   fused score, hold visually level for a beat, then the tie-break rule fires
   visibly — disclosure *inside* the animation, mid-piece. Novel relative to
   everything surveyed.
3. **Closing "what I simplified" section** with real measured numbers from
   the perf work, naming cut corners (reranker, model, scale), inviting
   scrutiny.
