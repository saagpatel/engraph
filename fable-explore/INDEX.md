# fable-explore — engraph deep session (2026-07-10)

Running index of findings, measurements, proposals, and drafts.
Mission: (1) find + cut the CPU burn with proof, (2) end-to-end improvement
proposals, (3) public material for saagarpatel.dev.

## Documents

- [01-architecture.md](01-architecture.md) — how engraph actually works: data model,
  the 5-lane retrieval pipeline, graph traversal, server/watcher topology.
  Includes the pre-measurement CPU hypothesis list.
- [02-profiling.md](02-profiling.md) — measured numbers: 6 findings with stack
  attribution (HF network dance, Metal wait, missing index, sqlite-vec overhead,
  model-load ×fleet, writable watchers in read-only servers).
- [03-perf-proposals.md](03-perf-proposals.md) — 4 fixes implemented on branch
  `fable/perf-exploration` with before/after + parity proof; 5 larger proposals
  (P1 watcher gating … P5 statement caching) awaiting your call.
- [04-improvement-proposals.md](04-improvement-proposals.md) — 10 quality/design
  proposals (intelligence split, health-checker lies, honest confidence, stemming,
  fossil tests, …) + what I deliberately left alone.
- [05-research-notes.md](05-research-notes.md) — verified web findings:
  llama.cpp context-reuse + multi-sequence batching practice, sqlite-vec
  scaling benchmarks; explainer-craft survey still open (research agents
  never reported).
- [06-public-material/](06-public-material/) —
  [essay draft "Where the CPU actually went"](06-public-material/essay-draft-where-the-cpu-went.md)
  (measure-first teardown, all numbers real) and
  [interactive explainer concept "One query, five opinions"](06-public-material/explainer-concept-query-fanout.md)
  (7-beat stepper over a synthetic vault graph, includes the tie-roulette
  confession beat).

## Status log

- 2026-07-10: Cloned devwhodevs/engraph at v1.7.2 (matches installed brew binary).
  Read the full retrieval path. Three mechanism-level CPU hypotheses formed
  (see 01-architecture.md §Hypotheses). Next: build from source + profile.
- 2026-07-11: Profiling complete (02). Headline mechanisms: per-start HuggingFace
  404 dance (~1.2s, never cached), embed time = Metal `waitUntilCompleted` (not
  compute), missing chunks(file_id) index (~300ms/query), sqlite-vec 7-10× vs
  in-memory, N read-only servers each running writable watchers (4× duplicate
  work measured in lab). Bonus discovery: ranking was nondeterministic (6/10
  self-disagreement) — fixed. Four fixes committed on branch with proof:
  CLI search 6.4s → 1.1-1.6s, determinism 10/10, embed vectors bitwise identical.
  Old-vs-new full-reindex race running. Live index/servers untouched throughout.
- 2026-07-11 (later): P1 implemented + lab-verified (one save → ONE index pass,
  was N; commit c6fc677). Full-reindex measured: 1998s wall / 114s CPU = 5.6%
  utilization (the P6 case); old binary killed unfinished at 26 min. Essay
  draft + explainer concept written (06). Web findings verified inline (05);
  explainer-craft survey still open. Branch: 5 commits, 468/468 tests, ready
  for operator review.
