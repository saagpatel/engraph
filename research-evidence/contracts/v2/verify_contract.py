#!/usr/bin/env python3
"""Fail-closed verifier for the shared research claim/run v2 contract."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "research-claim-run-manifest-v2.schema.json"
LOCK = HERE / "research-claim-run-manifest-v2.lock.json"
COMPATIBILITY = HERE / "research-claim-run-manifest-v1-to-v2.json"
REPRESENTATIVES = HERE / "representative-records.json"
HEX = set("0123456789abcdef")
EXPECTED_PACKAGE_FILES = {
    "README.md",
    "representative-records.json",
    "research-claim-run-manifest-v1-to-v2.json",
    "verify_contract.py",
}


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    _require(isinstance(value, dict), f"{path.name} must contain an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _repo_root() -> Path:
    for candidate in (HERE, *HERE.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ValueError("repository root unavailable")


def _local_v1_schema(root: Path) -> Path:
    candidates = (
        root / "research-evidence" / "research-claim-run-manifest-v1.schema.json",
        root / "agent_eval" / "operant" / "research-claim-run-manifest-v1.schema.json",
    )
    matches = [path for path in candidates if path.is_file()]
    _require(len(matches) == 1, "exactly one local v1 schema must be discoverable")
    return matches[0]


def _verify_artifact(value: object, label: str) -> None:
    _require(isinstance(value, dict), f"{label} must be an object")
    if value.get("status") == "UNKNOWN":
        _require(
            isinstance(value.get("reason"), str) and bool(value["reason"]),
            f"{label} UNKNOWN requires a reason",
        )
        return
    _require(value.get("status") == "BOUND", f"{label} status unsupported")
    _require(_is_digest(value.get("sha256")), f"{label} digest invalid")
    _require(
        isinstance(value.get("bytes"), int) and value["bytes"] >= 0,
        f"{label} byte count invalid",
    )
    _require(bool(value.get("locator")), f"{label} locator missing")


def _validate_with_reference_cli(schema: Path, records: list[dict[str, Any]]) -> None:
    executable = shutil.which("jsonschema")
    _require(executable is not None, "reference JSON Schema validator unavailable")
    with tempfile.TemporaryDirectory(prefix="research-contract-v2-") as tmp:
        for index, record in enumerate(records):
            instance = Path(tmp) / f"record-{index}.json"
            instance.write_text(
                json.dumps(record, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [executable, "-i", str(instance), str(schema)],
                capture_output=True,
                text=True,
                check=False,
            )
            _require(
                result.returncode == 0,
                f"representative {index} fails JSON Schema validation: "
                f"{result.stderr.strip() or result.stdout.strip()}",
            )


def _require_reference_rejection(
    schema: Path, record: dict[str, Any], label: str
) -> None:
    executable = shutil.which("jsonschema")
    _require(executable is not None, "reference JSON Schema validator unavailable")
    with tempfile.TemporaryDirectory(prefix="research-contract-v2-negative-") as tmp:
        instance = Path(tmp) / "invalid.json"
        instance.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        result = subprocess.run(
            [executable, "-i", str(instance), str(schema)],
            capture_output=True,
            text=True,
            check=False,
        )
        _require(result.returncode != 0, f"schema accepted invalid {label}")


def verify() -> None:
    schema = _load(SCHEMA)
    lock = _load(LOCK)
    compatibility = _load(COMPATIBILITY)
    representatives = _load(REPRESENTATIVES)
    schema_sha256 = _sha256(SCHEMA)

    _require(
        schema["$id"]
        == "https://schemas.local.invalid/research-claim-run-manifest/v2/schema.json",
        "v2 contract identifier mismatch",
    )
    _require(lock["contract_id"] == schema["$id"], "lock contract identifier mismatch")
    _require(lock["contract_sha256"] == schema_sha256, "contract lock mismatch")
    _require(
        set(lock["package_files"]) == EXPECTED_PACKAGE_FILES,
        "package lock coverage set changed",
    )
    for name, expected in lock["package_files"].items():
        _require(_sha256(HERE / name) == expected, f"package file drift: {name}")
    _require(
        compatibility["target_contract_sha256"] == schema_sha256,
        "compatibility target digest mismatch",
    )
    _require(
        lock["historical_policy"] == "PRESERVE_V1_BYTES_NO_IN_PLACE_REWRITE",
        "historical preservation policy missing",
    )
    _require(
        compatibility["migration_policy"]["mode"] == "COPY_FORWARD_NEW_EVENT_ONLY",
        "migration is not copy-forward only",
    )
    _require(
        compatibility["migration_policy"]["exact_reproduction_action"]
        == "REJECT_AUTOMATIC_MAPPING_REQUIRES_MANUAL_CLASSIFICATION",
        "legacy exact reproduction mapping does not fail closed",
    )
    _require(
        compatibility["adoption_status"]
        == "DUAL_READ_NEW_WRITE_V2_SYNTHETICALLY_VALIDATED",
        "adoption status does not match the validated producer rollout",
    )
    _require(
        lock["adoption_status"] == compatibility["adoption_status"],
        "lock adoption status differs from compatibility status",
    )

    root = _repo_root()
    local_v1_sha256 = _sha256(_local_v1_schema(root))
    collision = compatibility["collision"]
    _require(
        local_v1_sha256
        in {collision["operant_v1_sha256"], collision["engraph_v1_sha256"]},
        "local v1 schema digest is not recorded by the collision map",
    )
    _require(
        collision["operant_v1_sha256"] != collision["engraph_v1_sha256"],
        "v1 collision map lost the incompatible digests",
    )

    records = representatives.get("records")
    _require(isinstance(records, list) and len(records) == 2, "two representatives required")
    _require(
        {record.get("producer", {}).get("system") for record in records}
        == {"OPERANT", "ENGRAPH"},
        "representatives must cover OPERANT and ENGRAPH",
    )
    by_event_id = {record.get("event_id"): record for record in records}
    _require(len(by_event_id) == len(records), "base representative event IDs are not unique")
    derivations = representatives.get("score_derivations")
    _require(
        isinstance(derivations, list) and len(derivations) == 1,
        "one score derivation is required",
    )
    derivation = derivations[0]
    parent = by_event_id.get(derivation.get("parent_event_id"))
    _require(parent is not None, "score derivation parent is absent")
    _require(parent.get("record_type") == "attempt_result", "score parent is not an attempt result")
    score = deepcopy(parent)
    score["record_type"] = derivation["record_type"]
    score["event_id"] = derivation["event_id"]
    score["parent_event_id"] = derivation["parent_event_id"]
    score["outcome"]["state"] = derivation["state"]
    validation_records = [*records, score]
    event_ids: set[str] = set()
    required_evidence = {
        "exact_input",
        "case",
        "prompt",
        "system_harness",
        "dependency_lock",
        "model",
        "tokenizer",
        "raw_output",
        "scorer",
        "summary",
    }
    for index, record in enumerate(validation_records):
        label = f"representative {index}"
        _require(record.get("schema") == "research-claim-run-manifest.v2", f"{label} schema")
        _require(record["contract"]["sha256"] == schema_sha256, f"{label} contract digest")
        event_id = record.get("event_id")
        _require(isinstance(event_id, str) and bool(event_id), f"{label} event ID")
        _require(event_id not in event_ids, f"{label} duplicate event ID")
        event_ids.add(event_id)
        evidence = record.get("evidence")
        _require(isinstance(evidence, dict), f"{label} evidence missing")
        _require(set(evidence) == required_evidence, f"{label} evidence fields differ")
        for name, artifact in evidence.items():
            _verify_artifact(artifact, f"{label} evidence.{name}")
        _verify_artifact(record["comparison"]["baseline"], f"{label} comparison.baseline")
        _verify_artifact(record["integrity"]["self_digest"], f"{label} integrity.self_digest")
        if record["integrity"]["self_digest"]["status"] == "UNKNOWN":
            _require(
                record["integrity"]["canonicalization"] == "UNKNOWN"
                and record["integrity"]["authentication"] == "UNKNOWN",
                f"{label} overclaims unavailable integrity",
            )
        _require(
            record["comparison"]["class"] != "exact_reproduction",
            f"{label} uses prohibited exact reproduction class",
        )
        declared_license = record["rights"]["declared_license"]
        _require(bool(declared_license.get("reason")), f"{label} declared license reason missing")
        _verify_artifact(
            declared_license.get("evidence"),
            f"{label} rights.declared_license.evidence",
        )
        for name in (
            "license_conclusion",
            "consent_status",
            "redistribution_status",
            "derivative_use_status",
        ):
            decision = record["rights"][name]
            _require(bool(decision.get("reason")), f"{label} rights.{name} reason missing")
            _verify_artifact(decision.get("evidence"), f"{label} rights.{name}.evidence")
        _require(
            record["rights"]["license_conclusion"]["status"]
            in {"NOT_ASSERTED", "UNKNOWN"},
            f"{label} asserts a legal conclusion",
        )
        allowed_states = {
            "attempt_planned": {"pending"},
            "attempt_result": {"completed", "failed", "timeout", "aborted", "null"},
            "score_result": {"scored", "null", "failed"},
            "historical_evidence": {"historical"},
        }
        _require(
            record["outcome"]["state"] in allowed_states[record["record_type"]],
            f"{label} record type and outcome state mismatch",
        )
        if record["record_type"] == "score_result":
            parent_record = by_event_id.get(record.get("parent_event_id"))
            _require(parent_record is not None, f"{label} score is orphaned")
            _require(
                parent_record.get("record_type") == "attempt_result",
                f"{label} score parent type mismatch",
            )
            for field in ("producer", "run_id", "attempt_id", "case_id"):
                _require(
                    record.get(field) == parent_record.get(field),
                    f"{label} score-parent {field} mismatch",
                )

    _validate_with_reference_cli(SCHEMA, validation_records)
    invalid_declared_license = deepcopy(records[0])
    invalid_declared_license["rights"]["declared_license"]["status"] = "DECLARED"
    _require_reference_rejection(
        SCHEMA,
        invalid_declared_license,
        "declared license without bound evidence",
    )
    invalid_integrity = deepcopy(records[0])
    invalid_integrity["integrity"]["self_digest"] = {
        "status": "BOUND",
        "sha256": "0" * 64,
        "bytes": 0,
        "locator_kind": "content_addressed",
        "locator": "synthetic",
    }
    _require_reference_rejection(
        SCHEMA,
        invalid_integrity,
        "bound self-digest without canonicalization",
    )


def main() -> int:
    try:
        verify()
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print("OK: shared v2 research contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
