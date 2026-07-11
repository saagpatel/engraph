# Where the CPU actually went

*Draft 1, for saagarpatel.dev. Public-safe: architecture and measurements only,
no vault contents. Companion piece to the (planned) interactive explainer of
hybrid retrieval.*

---

My notes have a brain. It's a small Rust binary called engraph that turns a
folder of markdown into something my AI agents can actually query: semantic
embeddings, keyword search, wikilink graph traversal, all fused into one ranked
answer. Every agent on my machine talks to it dozens of times a day.

It also burns CPU like it's getting paid to, and some days a query just... times
out. I'd been living with that the way you live with a squeaky door. This week I
gave it a full session, with one rule borrowed from every performance war story
worth reading: measure first, and don't touch anything you can't prove.

I expected to find expensive math. I found a phone call.

## The suspect lineup

engraph's query path looks like this: embed the query with a local 300M
parameter model (llama.cpp, Metal GPU), scan ~12,000 chunk vectors for
semantic neighbors, run a keyword search, expand through the wikilink graph
two hops out, then merge all the ranked lists with reciprocal rank fusion.

Before profiling I wrote down ranked hypotheses, because the whole point of
measuring is catching yourself being wrong and I wanted receipts. My list:

1. The brute-force vector scan (no ANN index, just cosine against everything)
2. The per-query embedding
3. The graph expansion's chatty SQL
4. Whatever the fleet of server processes was doing to each other

Reasonable list. Mostly wrong order, and missing the actual headline twice.

## Finding 1: eight doomed HTTP requests, every single time

First profile run, and the trace opens with something absurd. Every
invocation, before doing any work, the binary tries to download a tokenizer
file from HuggingFace. Four candidate repos, two attempts each. Every one
fails: 404, 404, then 401s from the gated repos. It then falls back to the
tokenizer embedded in the model file it already has on disk, which works
perfectly, and always has.

The failure is never cached. So every search, every server start, repeats the
whole dance. Cost on a healthy network: about 1.2 seconds. Cost on hotel
wifi: your timeout. A fully local, privacy-first tool was making eight
network requests per start to fetch a file it was going to shrug off anyway.
The best part: the query path never even uses that tokenizer. It exists for
chunk-size estimates during indexing. The model tokenizes queries with its
own built-in vocabulary.

The fix is embarrassing in the good way: try the local caches first, network
last. Zero requests where there were eight.

## Finding 2: the GPU wasn't computing, it was napping between jobs

The embedding step measured anywhere from 12 milliseconds to 1.9 seconds for
the same 12-token query. That spread is the tell. Actual math doesn't vary
150x; environments do.

A stack sample during a slow run put 88% of the embed time in one frame:
`[MTLCommandBuffer waitUntilCompleted]`. The CPU wasn't grinding. It was
parked, waiting for the Metal GPU to hand back 256 floats. In a tight loop,
embeds ran 12ms flat. Add a 300ms idle gap between them, the realistic
rhythm of a server answering queries as they arrive, and embeds spiked past
a second. The GPU on a busy Mac is a shared, power-managed resource
(Ollama lives on mine), and every fresh request pays queueing, scheduling,
and wake-up costs before any arithmetic happens.

It gets better: the code builds a brand-new llama.cpp context for every
single embed call, because the context type isn't thread-safe and the
surrounding struct needed to be. Graph planning, buffer allocation, Metal
pipeline setup, per call. During indexing that means twelve thousand
create-encode-destroy cycles. A full reindex clocked 33 minutes of wall time
carrying under 2 minutes of compute: 5.6% CPU utilization. Not because it
was efficient. Because it was waiting.

For a 12-token encode, the dumbest possible benchmark settled the question:
CPU-only inference had a floor of 8.6ms against the GPU's 34ms, and it
dodges the queue entirely. Sometimes the accelerator is the slow path.

## Finding 3: one missing line of SQL

The graph lane, the part that follows wikilinks out from strong hits, was
costing up to 385ms per query. The probes themselves (over a hundred little
"does this neighbor mention the query term" checks) accounted for maybe 46ms.
The rest was a query plan reading `SCAN chunks`.

There was no index on the chunk table's file-ID column. So every "give me
the best snippet for this file" lookup, which runs about twenty times per
query, walked all 12,000 rows. And because each row carries its embedding
inline as a kilobyte-plus BLOB, walking the table meant dragging megabytes of
float data through SQLite's page cache to answer a question about one file's
headings.

`CREATE INDEX idx_chunks_file_id ON chunks(file_id);`

One line. Migration cost on my 60MB index: 0.07 seconds. Roughly 300ms
returned on every graph-heavy query, forever. I've written this bug myself
more times than I'll admit; the lesson that stuck is that `EXPLAIN QUERY
PLAN` takes ten seconds to run and I still forget to run it.

## Finding 4: the read-only servers that weren't

My machine runs a fleet of these servers, one per agent session, most
launched with `--read-only`. Ten-plus processes, each holding its own copy
of the embedding model.

Here's what `--read-only` actually gated: the write *tools*. The file
watcher, the thing that re-indexes your vault when a note changes, started
unconditionally in every one of them. I proved it in an isolated copy of the
whole setup: four read-only servers, one file save. Four log lines, one per
server, each independently re-chunking, re-embedding, and writing the shared
database. Every note edit on my machine was being indexed ten times by ten
processes racing on one SQLite file, absorbing the contention with a
five-second retry timeout until the day it couldn't.

And the loop closes: all that duplicate embedding churns the same GPU queue
that Finding 2 showed the query path waiting on. The fleet makes the
queries slow. The system was eating itself.

## The twist: it couldn't agree with itself

To prove my fixes changed nothing about *what* gets retrieved, I built a
parity harness: same ten queries, before-binary and after-binary, diff the
JSON. Half the queries failed parity. Alarming, briefly.

Then I ran the *unpatched* binary against itself. Six of ten queries returned
different results across two runs. Same binary, same index, same query,
different answers.

The pipeline assembles candidates in hash maps, and hash map iteration order
in Rust is deliberately randomized per process. Results with tied scores
entered the final sort in random order, and rank fusion turns rank positions
into scores, so ties didn't just reorder cosmetically; they shifted
confidence numbers, and in one spot (a truncate-to-20 after a tie-heavy
sort) they changed which files survived at all. My retrieval layer had been
quietly rolling dice on the margins the whole time, and nobody noticed
because nobody ever runs the same query twice and diffs the output.

Tiebreakers on every ranking sort fixed it: ten out of ten runs identical,
and suddenly parity testing means something. You can't prove behavior is
unchanged until the behavior is deterministic. I went in to save
milliseconds and accidentally made the thing *reproducible*, which in
retrospect I value more than the speed.

## The ledger

All together, on the same query battery, isolated copy of the real index:

| | Before | After |
|---|---|---|
| CLI search, wall time | 6.4s mean | 1.1-1.6s |
| Network requests per start | 8, all doomed | 0 |
| Same query, two runs | agreed 4/10 times | 10/10 |
| Embedding vectors | baseline | bitwise identical |
| Graph-lane best-snippet lookup | full scan × 20 per query | index hit |

The embedding vectors deserve the last word there: max absolute difference
between the old code path and the new one, across every dimension of every
test vector, was 0.0. Not "close enough." Identical. That's the standard a
retrieval change should meet before you trust it, because "faster but
slightly different results" is just a regression with better marketing.

## What I'd tell you to steal

**Write the suspect list before you profile.** Mine had the vector scan first.
The actual answer was a network call, a missing index, and an architecture
assumption. The list's job is to be wrong on the record, so you stop trusting
your instincts at the exact moment that stops being useful.

**Wall time and CPU time disagree for a reason.** 4 seconds of wall, 0.9 of
CPU. That gap was the whole story: network stalls and GPU waits. `time` tells
you there's a mystery; a stack sampler names it.

**"Read-only" is a claim about intent, not behavior.** Verify what the flag
actually gates. Mine gated a quarter of what I assumed.

**Determinism first, then optimization.** If your system can't produce the
same answer twice, you have no baseline to protect and no way to prove any
change is safe. It took ~30 lines of tiebreakers to give this system a
testable identity.

**Local-first tools still phone home.** Run one cold start with request
logging on. You might find your offline software has opinions about the
internet.

The branch with all of it, measurements to fixes, is a set of proposals
against upstream now. Total diff on the hot path: maybe 150 lines. The
profile did all the hard work; the code changes were just writing down what
it said.
