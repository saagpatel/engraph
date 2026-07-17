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
terminal receipt that no longer verifies against the current checkout
(`TERMINAL_RESULT_INVALID`). Duplicate plan or terminal IDs are explicit
`AMBIGUOUS_DUPLICATE_*` failures rather than last-path-wins; none of these
conditions is silently converted to a pass.

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

## Cross-repository schema boundary

`research-evidence/schema-lineage-v1.json` records that Engraph and OPERANT
published different contracts under the same v1 schema identifier. The
byte-identical package in `research-evidence/contracts/v2/` introduces a new
identifier, an exact schema lock, representative records for both systems, and
a non-rewriting compatibility map. Neither v1 producer has switched yet; the
package status remains `CONTRACT_VALIDATED_PRODUCERS_NOT_YET_SWITCHED`.
