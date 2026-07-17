#!/usr/bin/env python3
"""Offline verifier for Engraph model and source provenance contracts."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "models" / "model-manifest-v1.json"
SOURCES = ROOT / "models" / "model-source-receipts-v1.json"
LINEAGE = ROOT / "research-evidence" / "schema-lineage-v1.json"
HEX = set("0123456789abcdef")


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256_canonical(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= HEX


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify() -> None:
    manifest = _load(MANIFEST)
    sources = _load(SOURCES)
    lineage = _load(LINEAGE)

    _require(
        manifest["schema"] == "engraph-model-manifest.v1",
        "unsupported model manifest schema",
    )
    _require(
        sources["schema"] == "engraph-model-source-receipts.v1",
        "unsupported source receipt schema",
    )
    _require(
        "not a legal conclusion" in manifest["claim_boundary"],
        "model manifest claim boundary missing",
    )
    _require(
        "not archived source pages" in sources["claim_boundary"],
        "source receipt claim boundary missing",
    )

    models = manifest["models"]
    receipts = sources["receipts"]
    _require(
        len({row["uri"] for row in models}) == len(models),
        "duplicate model URI",
    )
    _require(
        len({row["source_receipt_id"] for row in models}) == len(models),
        "duplicate model source receipt reference",
    )
    _require(
        len({row["receipt_id"] for row in receipts}) == len(receipts),
        "duplicate model source receipt",
    )
    by_id = {row["receipt_id"]: row for row in receipts}

    for model in models:
        uri = model["uri"]
        _require(_is_hex(model["artifact_sha256"], 64), f"invalid hash for {uri}")
        _require(
            _is_hex(model["upstream_revision"], 40),
            f"mutable or invalid revision for {uri}",
        )
        _require(
            model["object_identity"] == f"sha256:{model['artifact_sha256']}",
            f"object identity mismatch for {uri}",
        )
        _require(
            model["license_conclusion"] == "NOT_ASSERTED",
            f"license conclusion asserted for {uri}",
        )
        _require(
            model["redistribution_status"] == "UNKNOWN",
            f"redistribution status overclaimed for {uri}",
        )

        receipt = by_id[model["source_receipt_id"]]
        projection = receipt["api_projection"]
        artifact = projection["artifact"]
        _require(receipt["uri"] == uri, f"source receipt URI mismatch for {uri}")
        _require(
            projection["sha"] == model["upstream_revision"],
            f"source revision mismatch for {uri}",
        )
        _require(
            artifact["lfs"]["sha256"] == model["artifact_sha256"],
            f"source LFS hash mismatch for {uri}",
        )
        _require(
            artifact["size"] == artifact["lfs"]["size"],
            f"source artifact size mismatch for {uri}",
        )
        _require(
            _sha256_canonical(projection) == receipt["api_projection_sha256"],
            f"source projection digest mismatch for {uri}",
        )
        _require(
            receipt["license_conclusion"] == "NOT_ASSERTED",
            f"source receipt asserts license conclusion for {uri}",
        )
        _require(
            receipt["redistribution_status"] == "UNKNOWN",
            f"source receipt overclaims redistribution for {uri}",
        )
        _require(
            receipt["quantizer_notice_completeness"] == "UNKNOWN",
            f"source receipt overclaims quantizer notice completeness for {uri}",
        )
        _require(bool(receipt["license_evidence"]), f"license evidence missing for {uri}")
        for evidence in receipt["license_evidence"]:
            digest = evidence["raw_response_sha256"]
            _require(
                digest == "UNKNOWN" or _is_hex(digest, 64),
                f"invalid license evidence digest for {uri}",
            )
            _require(
                evidence["archive_status"] == "NOT_CAPTURED",
                f"license archive status overclaimed for {uri}",
            )

    _require(
        lineage["compatibility"] == "INCOMPATIBLE_SAME_IDENTIFIER",
        "schema collision classification missing",
    )
    _require(
        lineage["migration_status"] == "REVIEW_REQUIRED_CROSS_REPO",
        "schema migration status overclaimed",
    )
    _require(
        lineage["historical_receipt_action"] == "PRESERVED_UNCHANGED",
        "historical receipt preservation boundary missing",
    )
    contracts = lineage["contracts"]
    _require(
        len({row["sha256"] for row in contracts}) == len(contracts),
        "schema collision contract hashes are not distinct",
    )
    _require(
        all(_is_hex(row["sha256"], 64) for row in contracts),
        "schema collision contract hash is invalid",
    )


def main() -> int:
    try:
        verify()
    except (KeyError, TypeError, ValueError) as error:
        print(f"FAIL: {error or 'provenance contract violation'}", file=sys.stderr)
        return 1
    print("OK: model provenance and schema lineage contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
