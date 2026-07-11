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

## The seven beats

1. **The question arrives.** A query chip ("what did I decide about the
   flywheel?") sits above the dormant graph. Reader can pick from 4 preset
   queries; each plays differently through every later beat.
2. **Semantic lane.** Notes glow by embedding similarity (heat = cosine).
   The point that lands: *fuzzy, vocabulary-independent, sometimes weird* —
   one semantically-hot note is an obvious false friend.
3. **Keyword lane.** A different subset lights up, crisp on/off (BM25).
   Exact term hits, including one the semantic lane missed entirely. The two
   lanes visibly disagree; that disagreement is the whole reason hybrid
   search exists.
4. **Graph expansion.** Seeds pulse, then energy flows down wikilink edges:
   1 hop at 0.8 strength, 2 hops at 0.5 (real decay constants from the
   code). A neighbor gets rejected (greyed) by the relevance filter, with
   its reason on hover. This is the beat nobody else's explainer has: the
   note graph as a retrieval signal.
5. **Rank fusion.** The three lanes' ranked lists slide in as columns;
   RRF scores accumulate per note (1/(60+rank), shown as stacking bars).
   Reader drags lane weights and watches the final order re-sort live. An
   "intent" toggle (conceptual / exact / temporal) sets the weight presets
   the real system uses.
6. **The tie roulette (the confession).** Two notes with identical fused
   scores. A "run it again" button: the pre-fix system, faithfully
   simulated, returns them in random order (with a run counter, e.g.
   "run 7: B, A"). Caption tells the true story: the real system disagreed
   with itself 6 runs out of 10 until deterministic tiebreakers landed.
   Interactive slop-free honesty; this is the beat readers will remember.
7. **What it cost.** The final ranked list annotated with the real measured
   timings of each stage (embed wait, vec scan, graph probes, fusion), and
   the punchline from the profiling essay: the expensive part wasn't the
   math.

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
- Preset queries: worth including one that returns a genuinely bad answer,
  as an honesty beat about retrieval limits?
- The site's palette/typography constraints (dataviz skill + palette rules)
  to be applied at build time, not in this concept.
