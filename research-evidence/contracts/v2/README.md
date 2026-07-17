# Shared research claim/run contract v2

This directory is a byte-identical contract package shared by OPERANT and
Engraph. It resolves the v1 identifier collision by using a new `$id` and an
exact schema digest. Both systems retain v1 readers and use the v2 envelope for
new controlled writes; synthetic plan/result/score paths validate against this
contract.

The v2 envelope keeps correctness, reproducibility, provenance, model identity,
rights, consent, redistribution, and comparability as separate fields. Missing
evidence is represented as `UNKNOWN` with a reason. `exact_reproduction` is not
an allowed class.

Migration is copy-forward only. Existing v1 schemas, logs, plans, raw outputs,
and receipts remain unchanged. An adapter may create a new v2 event that binds a
v1 source by digest; it may not edit or relabel the source record. OPERANT v1
`exact_reproduction` values require manual reclassification and cannot be
automatically promoted.

Run:

```sh
python3 verify_contract.py
```

The verifier checks the schema lock, local v1 collision digest, migration
policy, both representative system records, fail-closed UNKNOWN fields, and the
records against an installed Draft 2020-12 JSON Schema reference validator.
