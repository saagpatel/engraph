// Concurrency benchmark for the search path.
//
// Measures whether concurrent searches actually overlap, or whether they
// serialize on a shared lock. Compares N sequential searches against the same
// N fired concurrently; the ratio is the whole result.
//
//   speedup ~= N     -> requests overlap fully
//   speedup ~= 1     -> requests are fully serialized
//
// Usage (never point this at a live index — it drives a real server):
//
//   ENGRAPH_DATA_DIR=/path/to/lab-index \
//     cargo run -- serve --http --port 8899 --no-auth &
//   node bench/concurrency.mjs [port] [n]
//
// The first requests pay model load and GPU wakeup, so a warmup pass runs
// before either measurement. Without it the sequential leg absorbs the
// startup cost and the comparison reports a speedup that is not real.

const PORT = process.argv[2] ?? '8899';
const URL = `http://127.0.0.1:${PORT}/api/search`;

const QUERIES = [
  'embedding latency', 'vector cache', 'graph fusion', 'temporal ranking',
  'sqlite storage', 'concurrency mutex', 'reranker pipeline', 'semantic keyword',
];
const N = Number(process.argv[3] ?? QUERIES.length);
const workload = Array.from({ length: N }, (_, i) => QUERIES[i % QUERIES.length]);

async function search(query) {
  const t0 = performance.now();
  const res = await fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_n: 5 }),
  });
  const body = await res.json().catch(() => null);
  const hits = Array.isArray(body) ? body.length : body?.results?.length ?? -1;
  return { ms: performance.now() - t0, status: res.status, hits };
}

const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;
const ms = (n) => n.toFixed(0).padStart(6);

const main = async () => {
  for (const q of workload.slice(0, 3)) await search(q);

  const seqStart = performance.now();
  const seq = [];
  for (const q of workload) seq.push(await search(q));
  const seqTotal = performance.now() - seqStart;

  const conStart = performance.now();
  const con = await Promise.all(workload.map(search));
  const conTotal = performance.now() - conStart;

  const bad = [...seq, ...con].filter((r) => r.status !== 200);
  if (bad.length) {
    console.error(`FAILED: ${bad.length}/${seq.length + con.length} requests were not 200`);
    console.error('statuses:', [...new Set(bad.map((r) => r.status))].join(', '));
    process.exit(1);
  }

  console.log(`N = ${N}`);
  console.log(`hits per query: ${[...new Set([...seq, ...con].map((r) => r.hits))].join(', ')}`);
  console.log(`sequential total ${ms(seqTotal)} ms   per-req mean ${ms(mean(seq.map((r) => r.ms)))} ms`);
  console.log(`concurrent total ${ms(conTotal)} ms   per-req mean ${ms(mean(con.map((r) => r.ms)))} ms`);
  console.log('');
  console.log(`speedup ${(seqTotal / conTotal).toFixed(2)}x   (${N}.00x = full overlap, 1.00x = fully serialized)`);
};

main().catch((err) => {
  console.error('benchmark failed:', err.message);
  process.exit(1);
});
