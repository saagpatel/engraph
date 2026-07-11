# engraph end-to-end improvement proposals

My opinionated read after a full source pass, a day of profiling, and the
operational history in session memory. Perf-specific items live in
[03-perf-proposals.md](03-perf-proposals.md); these are quality, correctness,
and design. Ordered by how much I think they matter to you.

## Q1 — Split "intelligence" into its two very different halves

`intelligence = true` bundles query expansion (Qwen3-0.6B orchestrator) with
cross-encoder reranking. Your own 2026-06-05 harness showed the bundle is
net-negative (hit@3 67 vs 83 off), and the 4B-reranker retest proved *why*:
expansion corrupts the candidate pool **before** any reranker sees it, so no
reranker upgrade can win. Yet the config can't express "rerank only."

Proposal: `[intelligence] expansion = bool, rerank = bool` (back-compat:
`intelligence = true` sets both). Then re-run your retrieval-truth harness
with rerank-only — that's the experiment the current design physically
prevents, and the reranker deserves a fair trial.

## Q2 — Fix the health checker's two systematic lies

Known from ops history, root cause visible in source:

- **Broken-link false positives are permanent**: the checker looks up target
  slugs without `.md` while the index stores paths with `.md` — reindexing
  target files can never clear them (confirmed live 2026-06-19: 163 broken
  links unchanged after 50+ reindexes).
- **Orphans/broken-links are computed against the *scoped* index, not disk**:
  links into excluded layers read "broken", notes referenced only from
  excluded layers read "orphan" (~90% false-positive rate measured 2026-06-07).

Proposal: normalize slugs symmetrically (strip/add `.md` on both sides), and
give health a disk-reconciliation pass for link targets (a `walk_vault` hit
set is enough — targets need existence checks, not index membership). These
two fixes turn `health` from "known to cry wolf" into something you can act on
without a manual disk reconciliation every time.

## Q3 — Honest confidence numbers

`confidence = rrf_score / max * 100` → the top hit is **always 100%**, even
when it's garbage. That's a relative-rank number wearing a confidence
costume, and it actively misleads agent callers deciding whether to trust a
result. Options, cheapest first: (a) rename the field `relative_score`;
(b) calibrate on lane agreement (a hit found by semantic+FTS+graph deserves
more trust than a graph-only straggler — the data is already in
`lane_contributions`); (c) expose raw per-lane ranks and let callers decide.
I'd do (a)+(b): the lane-agreement signal is nearly free and matches how you
already reason about result quality.

## Q4 — Stemming for the FTS lane and graph relevance filter

`chunks_fts` uses FTS5's default unicode61 tokenizer — no stemming.
"ranking" ≠ "rankings" ≠ "ranked" in both the keyword lane and
`file_contains_term` (which gates graph expansion — a relevant neighbor gets
dropped because it says "rankings" while the query says "ranking").
Proposal: `tokenize = 'porter unicode61'` on the FTS table (needs a one-time
FTS rebuild, not a re-embed), and consider prefix matching for the graph
probe. Cheap, real recall win, zero effect on semantic lane.

## Q5 — Fix `status` lying about the model

`run_status` hardcodes `model: "all-MiniLM-L6-v2"` (search.rs) — you already
learned to distrust it and verify via the DB's vector dim. The embed model
URI is known at config load; store it in `meta` at index time (it IS the
index's model — config can change under an old index) and report from there,
plus the dim. Ten lines, removes a documented operational trap, and makes
dimension-mismatch failures self-diagnosing.

## Q6 — Delete or resurrect the fossil test suite

`tests/integration.rs` imports `engraph::hnsw` and `engraph::embedder` —
modules that no longer exist. It cannot compile; CI never notices because it
only runs `--lib`. Same for `tests/write_pipeline.rs`. Either port the
integration tests to the current API (they'd have caught none of this
session's findings, but a search-behavior snapshot test WOULD have caught the
nondeterminism) or delete them — a test file that can't compile is worse than
no test file. My pick: replace with a golden-file search battery over a tiny
fixture vault — that's the test shape this session proved valuable (it's how
the nondeterminism surfaced).

## Q7 — Exclude patterns that mean what they say

Config `exclude` only matches top-level entries and root files; nested paths
(`wiki/maps/`) are silently ignored (you carry a vault `.ignore` workaround,
which CLI indexing honors but servers don't — so a writable server's watcher
can re-pollute). Proposal: glob semantics (`globset`) for `exclude`, applied
identically in walker and watcher, and log a warning for patterns that match
nothing. This closes a whole class of "I excluded it but it's back" incidents.

## Q8 — Drop the duplicate vector store

Vectors live in `chunks.vector` BLOBs *and* the `chunks_vec` vec0 table —
verified consistent today (0 drift, both directions), but it's 2× the write
cost and file size, and the inline BLOB is what made the missing-index table
scans so expensive (Finding 3). Once the in-memory scan (P2) lands,
`chunks_vec` can go entirely; if not, move `vector` out of `chunks` into a
side table so hot row scans stop dragging kilobytes of float bytes around.

## Q9 — Tighter lock scopes in the MCP server

The search tool holds the embedder mutex across the entire pipeline (SQL
included), and the watcher's startup reconciliation + FullRescan hold both
store and embedder locks for their whole run — the code even comments that
blocking tool calls is "acceptable." At your fleet size it isn't: it's the
timeout mechanism. Embed → release → SQL is a small refactor; the P3 actor
thread makes it structural. Pairs with P1 (watcher gating).

## Q10 — Temporal heuristics live twice

`parse_date_range_heuristic` runs in `heuristic_orchestrate`, and the LLM
orchestrator has its own date handling — two implementations of "what does
'last week' mean" that can disagree (and one is only exercised when
intelligence is on, i.e. never on this machine). Extract one shared temporal
resolver; the orchestrators call it. Low urgency, but it's the kind of drift
that bites exactly when you re-enable intelligence to test Q1.

## What I deliberately left alone

- **Chunking:** the break-point scorer is genuinely good (headings, fences,
  overlap handling). Didn't find a failure mode worth changing.
- **RRF as the fusion core:** right choice at this scale; weights-by-intent
  is a sane design. Resist the urge to learn weights — no training signal.
- **The write pipeline:** atomic temp+rename, mtime conflict detection,
  crash recovery — solid. The `recent_writes` map is single-process-only,
  but P1 obsoletes the cross-process gap.
- **Graph edges model:** directional edges + backlink traversal at query
  time is correct and cheap. The 0.8/0.5 hop decay is unvalidated but
  harmless; tune only with a harness, never by feel.
