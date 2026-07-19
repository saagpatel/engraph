# Operator review — branch `fable/perf-exploration`

Reviewed 2026-07-18. Verified against source and a full test run, not against
the proposal documents, which turned out to be stale.

## Verification performed

- `cargo test --locked`: **471 passed, 0 failed** (plus the golden-search test).
  The handoff records 468; three more have landed since.
- Live `~/.engraph/engraph.db` and `config.toml` sha256-snapshotted before and
  after the run and confirmed **byte-identical**. The suite does not touch the
  live index.
- Branch is **not pushed**, working tree clean apart from an untracked
  `HANDOFF.md`.
- Authorship across the branch is clean (`saagpatel <saagarpatelai@gmail.com>`).

## The documents undersell the branch

`04-improvement-proposals.md` lists ten quality items as open. Four are already
implemented on this branch, and the doc was never updated:

| Item | Claimed status | Actual |
|---|---|---|
| Q4 FTS stemming | open proposal | **done** — `tokenize='porter unicode61'` in `store.rs:906`, with `FTS_TOKENIZER_VERSION = "porter-unicode61-v1"` and a version-keyed rebuild so existing indexes migrate |
| Q5 `status` lies about the model | open proposal | **done** — same commit, `f9e4f6d` |
| Q6 fossil test suite | open proposal | **done** — `tests/integration.rs` and `tests/write_pipeline.rs` deleted, replaced with `tests/golden_search.rs` + fixtures, which is exactly the golden-file battery Q6 recommended |
| Q10 duplicated temporal heuristics | open proposal | **appears done** — `llm.rs:844` now delegates to `crate::temporal::parse_date_range_heuristic` rather than carrying its own |

Likewise the perf side. The handoff records "5 proven fixes"; the branch carries
**ten substantive commits**, including three that the docs still list as
unstarted proposals:

- `98e83ce perf(serve): cache vectors for in-memory scans` — this is **P2**
- `c75929a perf(store): cache hot read statements` — this is **P5**
- `8fcaa4d fix(serve): warn on stale read-only indexes` — this is **T4**, the
  staleness warning that P1's acceptance was explicitly gated on
- `6734989 fix(serve): keep read-only startup non-mutating` — P1 follow-through

**P1's gate is therefore already satisfied.** The handoff says P1 acceptance
must "ship the staleness warning (task T4) alongside." T4 shipped in `8fcaa4d`.

## Genuinely still open

Verified in source, not inferred from the doc.

**Q3 — confidence is a rank-relative number wearing a confidence costume.**
`fusion.rs:127-134` computes `confidence = (rrf_score / max_score) * 100`, so
the top hit is **always 100%**, whatever its quality. This is the highest-value
open item and I would take it first. It is cheap, and it is the same defect
class this operator has been auditing all week: a number that looks like
evidence and carries none. Recommend the doc's (a)+(b): rename the field to
`relative_score`, and derive real confidence from lane agreement, which is
already sitting in `lane_contributions` unused.

**Q2 — the health checker's false positives.** `health.rs:56` builds broken
links straight from `store.get_unresolved_links()` with no symmetric slug
normalization, matching the described defect. The ops history records ~90%
false positives and 163 broken links that survived 50+ reindexes. Worth fixing
because it currently makes an entire feature untrustworthy rather than merely
noisy. I did not trace the indexer side, so treat the exact root cause as
unconfirmed.

**Q9 — lock scopes.** `serve.rs:380-389` takes the store, embedder, orchestrator
and reranker locks together and holds them across the pipeline. At fleet size
this is the timeout mechanism, as the doc says. Real, and it pairs structurally
with P3.

**Q1 — intelligence is still one boolean.** `config.rs:130` is unchanged
(`intelligence: Option<bool>`). The rerank-only experiment remains physically
impossible to run. Low effort, and it unblocks a real measurement rather than
just tidying.

**Q7 — exclude patterns.** Config documents `exclude` as "glob patterns" but no
`globset` dependency is present, so the documented behaviour and the actual
behaviour differ. Closing this kills a recurring "I excluded it and it came
back" incident class.

**Q8 — duplicate vector storage.** `chunks_vec` plus inline `chunks.vector`
BLOBs both still present. Lowest urgency of the open set; it is cost and file
size, not correctness.

## Recommended order

1. **Q3** — cheap, correctness-shaped, and it stops the tool lying to its own
   agent callers.
2. **Q2** — restores a feature that currently cannot be trusted.
3. **Q9** — the practical failure mode at your fleet size.
4. **Q1** — unblocks the rerank-only trial the current design forbids.
5. **Q7**, then **Q8**.

## What I did not do

- Did not push the branch. Operator-gated.
- Did not swap the live brew binary. Operator-gated.
- Did not touch `~/.engraph`, and proved it by checksum.
- Did not act on T1/P6. The prior verdict stands: the 1e-6 packed-parity gate is
  unachievable on the installed llama.cpp, and the proposed replacement
  criterion (cosine >= 0.999 plus identical top-5 sets, packing gated to Metal)
  still awaits an operator decision. That recommendation looks sound to me on
  its stated grounding, namely that an accepted Metal/CPU backend swap already
  drifts further than packing does.

## Housekeeping

`04-improvement-proposals.md` and `07-handoff-next-steps.md` should be marked up
for Q4/Q5/Q6/Q10, P2, P5 and T4, or a future session will re-propose work that
is already merged. `HANDOFF.md` at the repo root is untracked and says "5 fixes";
it is now the least accurate description of the branch.
