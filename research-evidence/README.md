# Engraph research evidence controls

These controls separate model-byte identity, licensing metadata, correctness,
performance, and replay classification. They do not convert a model card or
declared license into a legal conclusion.

## Model acquisition

`models/model-manifest-v1.json` is compiled into engraph. A model URI is usable
only when it has exactly one manifest entry and the cached or downloaded bytes
match its SHA-256 object identity. Future download URLs use a repository commit
whose official LFS metadata matches that exact object hash, not mutable
`resolve/main`. New downloads publish without replacement and receive a
`*.provenance.json` acquisition receipt containing the acquisition time,
model/base-model identity, tokenizer identity, declared license evidence, and
explicit non-conclusions.

Callers that must prove zero-network behavior use
`ensure_model_with_policy(..., ModelAcquisitionPolicy::CacheOnly)`. A cache miss
fails before directory creation or HTTP. The normal application path explicitly
uses `AllowDownload`; neither policy weakens manifest or byte verification.

`models/model-source-receipts-v1.json` preserves compact projections of official
Hugging Face revision and LFS-object metadata plus digests or explicit UNKNOWNs
for license evidence observed on 2026-07-17. The offline verifier
`scripts/verify_model_provenance.py` checks manifest uniqueness, immutable
revision shape, projection digests, cross-links, and non-conclusion fields. The
source pages were not archived, quantizer notice completeness is UNKNOWN, and
these records do not establish consent or legal reuse.

Existing legacy cache entries are accepted only after byte verification. If they
predate acquisition receipts, their acquisition time remains `UNKNOWN`. The
default tokenizer comes from the hash-bound GGUF. External tokenizers require
their own manifest entries and exact hashes; cache presence is not authority.

Research attempt receipts bind a matching acquisition sidecar when one exists.
If none exists, the attempt records `UNKNOWN_LEGACY_CACHE`; availability and a
matching model hash do not become proof of when or how the artifact was
acquired. A matching sidecar is labeled
`LOCAL_SIDECAR_BOUND_UNAUTHENTICATED`; its locally asserted acquisition time is
not an independently verified timestamp.

## Correctness and performance receipts

`scripts/research_receipt.py run` writes a unique planned event before executing
the measured argv, preserves combined raw output for success, failure, or
timeout, then writes a no-clobber terminal receipt. The receipt binds:

- raw output, fixture/prompt, tool binary, scorer, and summary bytes;
- exact model manifest, model artifact, and tokenizer identity;
- commit plus tracked-diff and untracked-file byte digests;
- `Cargo.toml`, `Cargo.lock`, host/runtime, seed, settings, timestamps, and exit
  state.

Host capture is allowlisted: OS, architecture, processor, logical CPU count,
memory, detected Rust/Cargo/CMake versions, and the research-related environment
variables used by this repository. Arbitrary environment variables are excluded
to avoid capturing secrets.

External tokenizer receipts fail closed until the tokenizer has its own
manifested URI and digest. The current model paths use the tokenizer embedded in
the exact-hash GGUF.

New receipts deliberately do not offer an `exact_reproduction` classification:
the runner cannot prove that an executable semantically consumed every declared
input, and there is no independently authenticated baseline. A clean,
tool-executed run may use `same_manifest_attempt`, which means only that the
captured pre/post inputs match within one attempt. It is not a replication
claim. Post-hoc `create` receipts remain `incomparable` or variant evidence.
The terminal gate rechecks that the commit and worktree stayed unchanged,
excluding only the unique plan and raw-output files created by the runner.
`audit-plans` scans an evidence directory for planned attempts. It distinguishes
an absent terminal result (`INCOMPLETE_NO_TERMINAL_RESULT`) from a preserved
terminal receipt whose required external model, tokenizer, configuration, or
ignored tool binary is absent (`TERMINAL_RESULT_UNAVAILABLE`) and from bound
evidence that is malformed, changed, or hash-mismatched
(`TERMINAL_RESULT_INVALID`). Unavailable remains an explicit non-pass, not a
clean-clone success. Duplicate plan or terminal IDs are explicit
`AMBIGUOUS_DUPLICATE_*` failures rather than last-path-wins; none of these
conditions is silently converted to a pass. V2 plans are also checked for a
complete fail-closed envelope and globally unique event identity. Use
`--attempt-id <id>` to evaluate one new attempt without allowing an unrelated
historical failure to become either its pass or its failure.

Receipts contain a canonical self-digest for accidental corruption detection.
The digest is stored beside editable metadata and is not authentication. They
are not signed, externally timestamped, or stored on immutable media, so
authorship, deliberate-tamper resistance, and host-independent custody remain
`UNKNOWN`.

The runner requires the bound tool binary to be the executable in `argv[0]`,
rejects secret-looking arguments, localizes absolute paths, and accepts only
explicitly synthetic or public non-sensitive evidence. Plan, raw output, and
receipt files start owner-only. The measured command runs in a dedicated process
group; group-contained descendants are terminated and the group must disappear
before terminal hashing. A process can deliberately escape that group, so
`same_manifest_attempt` is restricted to a source-reviewed non-spawning tool;
generic tools remain variant or incomparable evidence. Command output can still
disclose what the measured tool prints, so private corpus runs are out of scope
for this evidence directory.

The tool does not grant authority to run costly commands. Model downloads,
expensive benchmarks, and external workloads still require the applicable
operator approval.

## Citation and novelty durability

`citation-ledger-v1.json` is preserved as the historical citation assessment.
`claim-durability-overlay-v1.json` binds that ledger and the original
`fable/t1-gate-verdict` research records by SHA-256 without rewriting either
source. Four exact historical files are retained in a deterministic local
archive, so verification does not depend on an unrelated Git ref remaining
reachable in a future or shallow checkout. The overlay also binds
`historical-claims.json` and the scoped comparison sections in the live
README. The overlay makes separate decisions:

- mutable llama.cpp `master` links require revalidation and an immutable
  upstream revision before durable citation;
- the sqlite-vec figures remain unsuitable for exact public citation until
  confirmed from a primary, versioned source;
- the whole-pipeline whitespace claim is not established because the
  historical search did not preserve exact queries, search surfaces, inclusion
  criteria, results, timestamps, or a stopping rule.
- the separate “novel relative to everything surveyed” sentence is likewise
  not established;
- T1 is a documented historical assertion that is not reproducible from the
  surviving receipt/model evidence; P3 remains historically unverified; P6
  remains scoped to its tested machine, model, and configuration.

The names of candidates and negative results recorded in the old note survive
only as source-note assertions; they are not reconstructed search receipts.
Before any novelty claim is reasserted, the overlay requires a dated protocol
that preserves exact queries, competitor and disconfirming results, source
versions and hashes, supersession status, and the convergence rule. A bounded
search may support only a dated “no example found” statement, never a universal
“nobody has built this” conclusion.

Verify the boundary locally without network access:

```sh
PYTHONPATH=scripts python3 -m unittest scripts/test_verify_claim_durability.py
python3 scripts/verify_claim_durability.py
```

The same commands run in CI. Changes to the bound README comparison sections,
historical claim files, archived source bytes, durability dispositions, or
schema must update the evidence contract and pass its adversarial tests; new
high-risk novelty phrases fail closed.

## Cross-repository schema boundary

`research-evidence/schema-lineage-v1.json` records that Engraph and OPERANT
published different contracts under the same v1 schema identifier. The
byte-identical package in `research-evidence/contracts/v2/` introduces a new
identifier, an exact schema lock, representative records for both systems, and
a non-rewriting compatibility map. Both producers now retain v1 readers and use
v2 for new controlled writes. The package status is
`DUAL_READ_NEW_WRITE_V2_SYNTHETICALLY_VALIDATED`; no authentic post-adoption
model or provider run has yet upgraded that status beyond synthetic validation.
