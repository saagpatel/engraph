# Interactive explainer concept: "One query, five opinions"

*Concept doc for a saagarpatel.dev interactive explainer. How a hybrid
retrieval system answers a question: watch one query fan out across lanes,
traverse the note graph, and get fused into a single ranked list. Public-safe:
runs on a synthetic mini-vault, zero real note content.*

## Why this earns its place

Hybrid search is usually explained with a block diagram and the word
"reciprocal" doing a lot of unpaid labor. But the mechanism is *spatially*
intuitive: different lanes literally find different notes, graph expansion
literally walks edges, and fusion literally merges ranked lists. Every step
has a natural visual. And I have a live system (engraph over my vault) whose
real measured behavior gives the piece honest numbers and one great story
beat: ties in rank fusion made results nondeterministic until this month.

## The synthetic vault

~25 fictional notes with real structure: a few "project" notes, "people"
notes, daily notes with dates, wikilinks between them, tags. Content is
one-line stubs, enough to make BM25 and embedding behavior legible
("kombucha brewing log", "flywheel design sketch"). Embeddings precomputed
at build time into a small JSON (25 × a 2D projection + a similarity
matrix); no model in the browser, no runtime cost. The whole dataset ships
as one file under 50KB.

## The scene: a force-directed note graph

One persistent visual across the whole piece: the vault as a small
force-directed graph (notes = nodes, wikilinks = edges). The reader never
loses this map; each section lights it up differently. Stepper-driven, not
scroll-jacked: each stage is a discrete, replayable beat with a caption.
(Scroll-driven versions of this die on mobile and under prefers-reduced-motion;
a stepper respects both.)

## The eight beats

*(Restructured after the explainer-craft research: cold-open on the
uncertainty hook — distill.pub's evidence says animation earns its keep on
causality/uncertainty, and Sam Rose's pieces open on a "wait, what?" moment,
not a diagram. Sandbox moved to the end per Nicky Case's guided-freedom
pattern.)*

1. **Cold open: run it twice.** A search box, a dormant graph, no
   explanation yet. The reader hits "search," gets a ranked list. A "run it
   again" button — same query, and two results at the tail swap places (a
   faithful simulation of the pre-fix system, run counter included:
   "run 7: B, A"). One line of copy: *"Same notes, same query. This system
   really did that, 6 runs out of 10. The rest of this page is why."*
2. **The question, properly.** The query chip settles above the graph;
   reader can swap among 4 presets (each plays differently through every
   later beat, including one that returns a genuinely mediocre answer —
   retrieval limits shown, not narrated).
3. **Semantic lane.** Notes glow by embedding similarity (heat = cosine).
   The point that lands: *fuzzy, vocabulary-independent, sometimes weird* —
   one semantically-hot note is an obvious false friend.
4. **Keyword lane.** A different subset lights up, crisp on/off (BM25).
   Exact term hits, including one the semantic lane missed entirely. The two
   lanes visibly disagree; that disagreement is the whole reason hybrid
   search exists.
5. **Graph expansion.** Seeds pulse, then energy flows down wikilink edges:
   1 hop at 0.8 strength, 2 hops at 0.5 (real decay constants from the
   code). A neighbor gets rejected (greyed) by the relevance filter, with
   its reason on hover. Prior-art scan says this beat exists nowhere else:
   the note graph as a retrieval signal.
6. **Rank fusion.** The three lanes' ranked lists slide in as columns; RRF
   scores accumulate per note (1/(60+rank), stacking bars). Reader drags
   lane weights and a k-slider, watches the final order re-sort live; an
   "intent" toggle (conceptual / exact / temporal) applies the real weight
   presets. (Study the one strong prior art before building:
   Serghei's live RRF simulation, blog.serghei.pl.)
7. **The tie, resolved.** Two notes arrive at an identical fused score and
   hold visually level for a beat — then the tie-break rule fires as its own
   small animated event, and the cold open pays off: this exact moment,
   unhandled, was the run-to-run randomness. Disclosure inside the
   animation, not a footnote.
8. **What it cost + what I simplified.** The final list annotated with real
   measured stage timings (embed wait, vec scan, graph probes, fusion), the
   essay's punchline (the expensive part wasn't the math), and a Sam
   Rose-style closing: what this toy leaves out (reranker, real model, real
   scale) and why. Then the full **sandbox as payoff**: free query entry,
   all knobs unlocked.

## Interaction model

- **Stepper** (next/back, keyboard arrows), each beat ~1 replayable
  animation of 1-3s. Progress dots double as a table of contents.
- **Poke-ables:** query picker (beat 1), hover any node for its lane scores
  at any time, weight sliders + intent presets (beat 5), the re-run button
  (beat 6).
- **prefers-reduced-motion:** animations become instant state changes; every
  beat is fully legible as a static frame with the caption.
- **No-JS fallback:** server-rendered SVG of the final beat + the essay
  handles the narrative.

## Implementation shape (fits the current site)

Hand-rolled SVG + vanilla JS (or a single Preact island if the site already
has one); d3-force at *build time* to compute the layout, shipped as static
coordinates — no d3 in the bundle. Physics at runtime is unnecessary; the
graph never changes shape, only highlights. Estimated bundle: <30KB JS +
50KB data. All animation via CSS transitions on SVG attributes, which keeps
the reduced-motion path free.

## Open questions for the operator

- Standalone explainer page, or embedded in the profiling essay? My lean:
  standalone piece ("how my notes answer questions"), essay links to it —
  the essay's arc is measurement, the explainer's arc is mechanism; fusing
  them would bloat both.
- The site's palette/typography constraints (dataviz skill + palette rules)
  to be applied at build time, not in this concept.

## Research grounding (see 05-research-notes.md for sources)

- Stepper/segmentation over continuous animation: distill.pub's synthesis —
  segmentation measurably improves learning; steps double as the
  reduced-motion path (WCAG 2.3.3: motion carries information here, so the
  fallback is stepped, not stripped).
- Sandbox at the end: Nicky Case's guided-freedom pattern; naked sandboxes
  up front are a named failure mode.
- Hand-rolled vanilla JS + SVG at this node count; d3 buys nothing here for
  ~70KB; hybrid Canvas layer only if profiling demands it.
- Whitespace confirmed: stage-level prior art exists (embedding projectors,
  live PageRank editors, one live RRF k-slider demo worth studying), but no
  essay-grade end-to-end hybrid-pipeline narrative was found anywhere.
- Genre voice: real p50/p95 numbers over adjectives; disclose simplifications
  by name; counterintuitive hook first (all Sam Rose moves, all consistent
  with the measured material we already have).
