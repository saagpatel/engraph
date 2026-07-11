# fable-explore — engraph deep session (2026-07-10)

Running index of findings, measurements, proposals, and drafts.
Mission: (1) find + cut the CPU burn with proof, (2) end-to-end improvement
proposals, (3) public material for saagarpatel.dev.

## Documents

- [01-architecture.md](01-architecture.md) — how engraph actually works: data model,
  the 5-lane retrieval pipeline, graph traversal, server/watcher topology.
  Includes the pre-measurement CPU hypothesis list.
- (pending) 02-profiling.md — measured numbers: where the CPU actually goes.
- (pending) 03-perf-proposals.md — targeted fixes, each with before/after +
  results-unchanged proof.
- (pending) 04-improvement-proposals.md — end-to-end quality/design proposals.
- (pending) 05-research-notes.md — web research: retrieval perf practice,
  interactive explainer craft.
- (pending) 06-public-material/ — essay + explainer drafts for saagarpatel.dev.

## Status log

- 2026-07-10: Cloned devwhodevs/engraph at v1.7.2 (matches installed brew binary).
  Read the full retrieval path. Three mechanism-level CPU hypotheses formed
  (see 01-architecture.md §Hypotheses). Next: build from source + profile.
