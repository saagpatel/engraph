#!/usr/bin/env python3
"""Verify Engraph citation durability and historical novelty boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "research-evidence" / "claim-durability-overlay-v1.json"
EXPECTED_CLAIM_BOUNDARY = (
    "This overlay classifies whether existing research claims remain citable and "
    "durable. It does not recreate an undocumented search, establish novelty, or "
    "upgrade unavailable evidence."
)
EXPECTED_AS_OF = "2026-07-17"
EXPECTED_SOURCE_LEDGER_PATH = "research-evidence/citation-ledger-v1.json"
EXPECTED_SCHEMA_PATH = "research-evidence/claim-durability-overlay-v1.schema.json"
EXPECTED_SOURCE_RECORD_COMMIT = "f69fffc1697f8974a906ec325b3f0976cf555ae2"
EXPECTED_SOURCE_RECORD_PATH = "fable-explore/05-research-notes.md"
EXPECTED_ARCHIVE_PATH = (
    "research-evidence/historical-sources/fable-t1-gate-verdict.tar"
)
EXPECTED_ARCHIVE_MEMBERS = {
    "fable-explore/05-research-notes.md": (
        "f1d5d17476cdcd2db75624fb82792be9c8c921ee633d8cd6422c11ef7df24185"
    ),
    "fable-explore/09-t1-perf-refutation.md": (
        "37d3f80cd46f10cca1933e3689abd7cfad9058c3594a0f883f9ae9e95cf86a85"
    ),
    "fable-explore/10-gpu-bound-verdict.md": (
        "fb6e4571a6e2cb39e629dab1c9270a72de9bd424be9e3a0111aea82a8f51966c"
    ),
    "fable-explore/11-perf-claims-audit.md": (
        "52b330eebb0f77a3232d367a74cb888faa9b48012db63334e4f18d6ccadd70ed"
    ),
}
EXPECTED_HISTORICAL_CLAIMS_PATH = "research-evidence/historical-claims.json"
EXPECTED_HISTORICAL_DISPOSITIONS = {
    "T1 packed multi-sequence encoding loses at production-sized inputs": (
        "SUPPORTED_LOCAL_HISTORICAL_OBSERVATION",
        "DOCUMENTED_HISTORICAL_ASSERTION_UNREPRODUCIBLE",
        "DO_NOT_PUBLISH_AS_REPRODUCED_OR_GENERAL",
    ),
    "P3 reused-context actor produced a 1.67x query-path speedup": (
        "HISTORICAL_UNVERIFIED",
        "HISTORICAL_UNVERIFIED",
        "DO_NOT_PUBLISH_EXACT_RATIO",
    ),
    "P6 is retired": (
        "SCOPED_TO_TESTED_MACHINE_MODEL_CONFIGURATION",
        "MACHINE_CONFIGURATION_SCOPED_ONLY",
        "UNIVERSAL_RETIREMENT_PROHIBITED",
    ),
}
EXPECTED_SURFACE_DISPOSITIONS = {
    "readme-problem-positioning": "POSITIONING_NOT_COMPETITOR_COVERAGE",
    "readme-how-it-compares": "SCOPED_REFERENCE_CATEGORIES_NOT_MARKET_SURVEY",
    "historical-whole-pipeline-nobody-built": (
        "NOT_ESTABLISHED_UNDOCUMENTED_SEARCH"
    ),
    "historical-tie-animation-novel": "NOT_ESTABLISHED_UNDOCUMENTED_SEARCH",
}
EXPECTED_ALLOWED_WORDING = (
    "The 2026-07-17 audit found an undocumented search note that reported no "
    "example; because the queries and result set were not preserved, novelty is "
    "not established."
)
EXPECTED_DECISION_RULE = (
    "Absence from a bounded search supports only a dated scoped no-example-found "
    "statement. It never establishes nobody-has-built or universal novelty."
)
EXPECTED_PROTOCOL_FIELDS = {
    "research_question_and_claim_scope",
    "search_start_and_cutoff_timestamps",
    "researcher_and_agent_identities",
    "search_engines_indexes_and_directories",
    "exact_queries_in_execution_order",
    "inclusion_and_exclusion_criteria",
    "candidate_and_competitor_results",
    "negative_and_disconfirming_results",
    "canonical_identifiers_versions_access_dates_and_hashes",
    "supersession_or_withdrawal_status",
    "convergence_and_stop_rule",
    "claim_wording_allowed_by_the_record",
}
EXPECTED_DURABILITY = {
    "llama-context-reuse-production-practice": (
        "PRIMARY_SOURCE_IDENTIFIED_VERSION_UNPINNED",
        "REVALIDATE_AND_PIN_IMMUTABLE_UPSTREAM_VERSION",
    ),
    "sqlite-vec-performance-figures": (
        "SECONDARY_SOURCE_UNCONFIRMED",
        "NOT_DURABLE_FOR_EXACT_NUMERIC_CITATION",
    ),
    "whole-hybrid-pipeline-whitespace": (
        "NEGATIVE_SEARCH_UNPRESERVED",
        "UNIVERSAL_NOVELTY_CLAIM_PROHIBITED",
    ),
}
EXPECTED_UNKNOWN_SEARCH_FIELDS = {
    "searched_at",
    "exact_queries",
    "search_surfaces",
    "inclusion_criteria",
    "exclusion_criteria",
    "stop_rule",
}
EXPECTED_CANDIDATES = {
    ("Embedding projectors and WizMap", None),
    ("Live-editable PageRank graph explainers", None),
    ("RAG Playground and RAGGY", None),
    (
        "Serghei reciprocal-rank-fusion k-slider",
        "https://blog.serghei.pl/posts/reciprocal-rank-fusion-explained/",
    ),
}
EXPECTED_NEGATIVE_ASSERTIONS = {
    "No good interactive BM25 explainer was found.",
    "No essay-grade narrative covering the whole hybrid pipeline was found.",
}
HIGH_RISK_PATTERN = re.compile(
    r"(?i)\b(?:nobody has built|no one has built|no existing (?:tool|product|system)"
    r"|first[- ]of[- ]its[- ]kind|novel relative to|competitors?|alternatives?"
    r"|unique|the only (?:local )?(?:tool|product|system|workflow|engine|search)"
    r"|the first (?:local )?(?:tool|product|system|workflow|engine|search)"
    r"|unlike any (?:existing )?(?:tool|product|system|competitor|alternative)"
    r"|no comparable (?:tool|product|system|competitor|alternative))\b"
)
HISTORICAL_NOVELTY_PATTERN = re.compile(
    r"(?i)\b(?:nobody has built|no one has built|no existing (?:tool|product|system)"
    r"|first[- ]of[- ]its[- ]kind|novel relative to)\b"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_repo_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or value in {"", "."}:
        return None
    return ROOT / candidate


def git_blob(commit: object, path: object) -> bytes | None:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return None
    if safe_repo_path(path) is None:
        return None
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def _schema_ref(root_schema: dict, reference: str) -> dict | None:
    if not reference.startswith("#/"):
        return None
    value: object = root_schema
    for part in reference[2:].split("/"):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value if isinstance(value, dict) else None


def validate_schema(
    instance: object,
    schema: dict,
    *,
    root_schema: dict | None = None,
    path: str = "$",
) -> list[str]:
    """Evaluate the strict JSON-Schema subset used by this local contract."""
    root_schema = root_schema or schema
    errors: list[str] = []
    if "$ref" in schema:
        resolved = _schema_ref(root_schema, schema["$ref"])
        if resolved is None:
            return [f"{path}: unresolved schema reference"]
        return validate_schema(instance, resolved, root_schema=root_schema, path=path)
    expected_type = schema.get("type")
    type_ok = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "boolean": isinstance(instance, bool),
    }
    if expected_type in type_ok and not type_ok[expected_type]:
        return [f"{path}: expected {expected_type}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value differs from schema const")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value is not in schema enum")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if pattern and re.fullmatch(pattern, instance) is None:
            errors.append(f"{path}: string does not match schema pattern")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                errors.append(f"{path}: missing required property {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}: unknown property {key}")
        for key, child_schema in properties.items():
            if key in instance:
                errors.extend(
                    validate_schema(
                        instance[key],
                        child_schema,
                        root_schema=root_schema,
                        path=f"{path}.{key}",
                    )
                )
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: array has too few items")
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True) for item in instance]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path}: array items are not unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(
                    validate_schema(
                        item,
                        item_schema,
                        root_schema=root_schema,
                        path=f"{path}[{index}]",
                    )
                )
    return errors


def extract_section(text: str, start_marker: str, end_marker: str) -> str | None:
    start = text.find(start_marker)
    if start < 0:
        return None
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        return None
    return text[start:end]


def high_risk_claims(text: str) -> list[str]:
    return [match.group(0) for match in HIGH_RISK_PATTERN.finditer(text)]


def historical_novelty_claims(text: str) -> list[str]:
    return [match.group(0) for match in HISTORICAL_NOVELTY_PATTERN.finditer(text)]


def read_archive(binding: dict) -> tuple[dict[str, bytes], list[str]]:
    errors: list[str] = []
    archive_path = safe_repo_path(binding.get("path"))
    if archive_path is None or not archive_path.is_file():
        return {}, ["historical source archive is unavailable or unsafe"]
    archive_bytes = archive_path.read_bytes()
    if sha256_bytes(archive_bytes) != binding.get("sha256"):
        errors.append("historical source archive hash mismatch")
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(archive_path, "r:") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    members[member.name] = handle.read()
    except (OSError, tarfile.TarError):
        errors.append("historical source archive is unreadable")
    return members, errors


def verify(data: dict) -> list[str]:
    errors: list[str] = []
    if data.get("schema") != "engraph-claim-durability-overlay.v1":
        errors.append("unsupported overlay schema")
    if data.get("as_of") != EXPECTED_AS_OF:
        errors.append("overlay as_of changed")
    if data.get("claim_boundary") != EXPECTED_CLAIM_BOUNDARY:
        errors.append("claim boundary is missing or changed")

    schema_binding = data.get("schema_binding")
    if not isinstance(schema_binding, dict):
        errors.append("missing schema binding")
        schema_binding = {}
    if schema_binding.get("path") != EXPECTED_SCHEMA_PATH:
        errors.append("schema binding path changed")
    schema_path = safe_repo_path(schema_binding.get("path"))
    if schema_path is None or not schema_path.is_file():
        errors.append("bound schema path is unavailable or unsafe")
        schema_data = {}
    else:
        schema_bytes = schema_path.read_bytes()
        if sha256_bytes(schema_bytes) != schema_binding.get("sha256"):
            errors.append("bound schema hash mismatch")
        try:
            schema_data = json.loads(schema_bytes)
        except json.JSONDecodeError:
            errors.append("bound schema is not valid JSON")
            schema_data = {}
    if schema_data:
        errors.extend(
            f"schema validation: {error}"
            for error in validate_schema(data, schema_data)
        )

    source_ledger = data.get("source_ledger")
    if not isinstance(source_ledger, dict):
        errors.append("missing source ledger binding")
        source_ledger = {}
    if source_ledger.get("path") != EXPECTED_SOURCE_LEDGER_PATH:
        errors.append("source ledger path changed")
    ledger_path = safe_repo_path(source_ledger.get("path"))
    if ledger_path is None or not ledger_path.is_file():
        errors.append("source ledger path is unavailable or unsafe")
        ledger = {}
    else:
        ledger_bytes = ledger_path.read_bytes()
        if sha256_bytes(ledger_bytes) != source_ledger.get("sha256"):
            errors.append("source ledger hash mismatch")
        try:
            ledger = json.loads(ledger_bytes)
        except json.JSONDecodeError:
            errors.append("source ledger is not valid JSON")
            ledger = {}

    source_record = data.get("source_claim_record")
    if not isinstance(source_record, dict):
        errors.append("missing source claim record binding")
        source_record = {}
    if source_record.get("git_commit") != EXPECTED_SOURCE_RECORD_COMMIT:
        errors.append("source claim commit changed")
    if source_record.get("path") != EXPECTED_SOURCE_RECORD_PATH:
        errors.append("source claim path changed")
    if source_record.get("availability") != "ARCHIVED_EXACT_BYTES":
        errors.append("source claim availability is not archive-backed")

    source_archive = data.get("source_archive")
    if not isinstance(source_archive, dict):
        errors.append("missing historical source archive binding")
        source_archive = {}
    if source_archive.get("path") != EXPECTED_ARCHIVE_PATH:
        errors.append("historical source archive path changed")
    if source_archive.get("source_commit") != EXPECTED_SOURCE_RECORD_COMMIT:
        errors.append("historical source archive commit changed")
    archive_members, archive_errors = read_archive(source_archive)
    errors.extend(archive_errors)
    declared_members = source_archive.get("members")
    declared_by_path = (
        {
            row.get("path"): row.get("sha256")
            for row in declared_members
            if isinstance(row, dict)
        }
        if isinstance(declared_members, list)
        else {}
    )
    if declared_by_path != EXPECTED_ARCHIVE_MEMBERS or len(
        declared_members or []
    ) != len(EXPECTED_ARCHIVE_MEMBERS):
        errors.append("historical source archive member manifest changed")
    if set(archive_members) != set(EXPECTED_ARCHIVE_MEMBERS):
        errors.append("historical source archive member set changed")
    for member_path, expected_digest in EXPECTED_ARCHIVE_MEMBERS.items():
        member_bytes = archive_members.get(member_path)
        if member_bytes is None or sha256_bytes(member_bytes) != expected_digest:
            errors.append(f"historical source member hash mismatch: {member_path}")
    archived_source = archive_members.get(EXPECTED_SOURCE_RECORD_PATH)
    if (
        archived_source is None
        or sha256_bytes(archived_source) != source_record.get("sha256")
    ):
        errors.append("source claim archive member hash mismatch")
    blob = git_blob(source_record.get("git_commit"), source_record.get("path"))
    if blob is not None and sha256_bytes(blob) != source_record.get("sha256"):
        errors.append("source claim Git object hash mismatch")

    historical_source = data.get("historical_claims_source")
    if not isinstance(historical_source, dict):
        errors.append("missing historical claims binding")
        historical_source = {}
    if historical_source.get("path") != EXPECTED_HISTORICAL_CLAIMS_PATH:
        errors.append("historical claims path changed")
    historical_path = safe_repo_path(historical_source.get("path"))
    if historical_path is None or not historical_path.is_file():
        errors.append("historical claims source is unavailable or unsafe")
        historical_data = {}
    else:
        historical_bytes = historical_path.read_bytes()
        if sha256_bytes(historical_bytes) != historical_source.get("sha256"):
            errors.append("historical claims hash mismatch")
        try:
            historical_data = json.loads(historical_bytes)
        except json.JSONDecodeError:
            errors.append("historical claims are not valid JSON")
            historical_data = {}
    historical_claims = historical_data.get("claims")
    if not isinstance(historical_claims, list):
        errors.append("historical claim rows are unavailable")
        historical_claims = []
    historical_by_claim = {
        row.get("claim"): row for row in historical_claims if isinstance(row, dict)
    }
    dispositions = data.get("historical_claim_dispositions")
    if not isinstance(dispositions, list):
        errors.append("historical claim dispositions are unavailable")
        dispositions = []
    disposition_by_claim = {
        row.get("claim"): row for row in dispositions if isinstance(row, dict)
    }
    if set(historical_by_claim) != set(EXPECTED_HISTORICAL_DISPOSITIONS):
        errors.append("historical source claim set changed")
    if set(disposition_by_claim) != set(EXPECTED_HISTORICAL_DISPOSITIONS):
        errors.append("historical disposition claim set changed")
    for claim, expected in EXPECTED_HISTORICAL_DISPOSITIONS.items():
        source = historical_by_claim.get(claim)
        disposition = disposition_by_claim.get(claim)
        if not isinstance(source, dict) or not isinstance(disposition, dict):
            continue
        observed = (
            disposition.get("source_status"),
            disposition.get("durability_state"),
            disposition.get("public_use"),
        )
        if observed != expected or source.get("status") != expected[0]:
            errors.append(f"historical claim disposition changed: {claim}")
        if (
            source.get("status", "").startswith("SUPPORTED")
            and (
                source.get("raw_receipt") == "UNKNOWN"
                or source.get("model_digest_at_run") == "UNKNOWN"
            )
            and disposition.get("durability_state")
            != "DOCUMENTED_HISTORICAL_ASSERTION_UNREPRODUCIBLE"
        ):
            errors.append(
                f"supported empirical claim lacks reproducibility evidence: {claim}"
            )

    surfaces = data.get("claim_surfaces")
    if not isinstance(surfaces, list):
        errors.append("claim surface registry is unavailable")
        surfaces = []
    surfaces_by_id = {
        row.get("surface_id"): row for row in surfaces if isinstance(row, dict)
    }
    if set(surfaces_by_id) != set(EXPECTED_SURFACE_DISPOSITIONS):
        errors.append("claim surface registry coverage changed")
    registered_live_excerpts: list[str] = []
    for surface_id, disposition in EXPECTED_SURFACE_DISPOSITIONS.items():
        surface = surfaces_by_id.get(surface_id)
        if not isinstance(surface, dict):
            continue
        if surface.get("disposition") != disposition:
            errors.append(f"{surface_id}: claim surface disposition changed")
        if surface.get("source_kind") == "LIVE_FILE_SECTION":
            path = safe_repo_path(surface.get("path"))
            if path is None or not path.is_file():
                errors.append(f"{surface_id}: live claim surface is unavailable")
                continue
            text = path.read_text(encoding="utf-8")
            excerpt = extract_section(
                text,
                str(surface.get("start_marker", "")),
                str(surface.get("end_marker", "")),
            )
            if excerpt is None or sha256_bytes(excerpt.encode()) != surface.get(
                "sha256"
            ):
                errors.append(f"{surface_id}: live claim surface hash mismatch")
            elif excerpt is not None:
                registered_live_excerpts.append(excerpt)
        elif surface.get("source_kind") == "ARCHIVE_EXCERPT":
            member = archive_members.get(surface.get("archive_member"))
            exact_excerpt = surface.get("exact_excerpt")
            if (
                member is None
                or not isinstance(exact_excerpt, str)
                or member.decode("utf-8").count(exact_excerpt) != 1
                or sha256_bytes(exact_excerpt.encode()) != surface.get("sha256")
            ):
                errors.append(f"{surface_id}: archived claim excerpt mismatch")
        else:
            errors.append(f"{surface_id}: unsupported claim surface kind")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in high_risk_claims(readme_text):
        if not any(phrase in excerpt for excerpt in registered_live_excerpts):
            errors.append(f"README contains unregistered high-risk claim: {phrase}")
    historical_text = archive_members.get(EXPECTED_SOURCE_RECORD_PATH, b"").decode(
        "utf-8", errors="replace"
    )
    registered_historical_excerpts = [
        row.get("exact_excerpt", "")
        for row in surfaces
        if isinstance(row, dict) and row.get("source_kind") == "ARCHIVE_EXCERPT"
    ]
    for phrase in historical_novelty_claims(historical_text):
        if not any(phrase in excerpt for excerpt in registered_historical_excerpts):
            errors.append(
                f"historical source contains unregistered novelty phrase: {phrase}"
            )

    ledger_claims = ledger.get("claims")
    if not isinstance(ledger_claims, list):
        errors.append("source ledger claims are unavailable")
        ledger_claims = []
    ledger_by_id = {
        row.get("claim_id"): row for row in ledger_claims if isinstance(row, dict)
    }
    if len(ledger_by_id) != len(ledger_claims):
        errors.append("source ledger claim IDs are missing or duplicated")

    overlay_claims = data.get("claims")
    if not isinstance(overlay_claims, list):
        errors.append("overlay claims are unavailable")
        overlay_claims = []
    overlay_by_id = {
        row.get("claim_id"): row for row in overlay_claims if isinstance(row, dict)
    }
    if len(overlay_by_id) != len(overlay_claims):
        errors.append("overlay claim IDs are missing or duplicated")
    if set(overlay_by_id) != set(ledger_by_id):
        errors.append("overlay does not cover the exact source-ledger claim set")

    for claim_id, expected in EXPECTED_DURABILITY.items():
        source = ledger_by_id.get(claim_id)
        overlay = overlay_by_id.get(claim_id)
        if not isinstance(source, dict) or not isinstance(overlay, dict):
            continue
        if overlay.get("source_status") != source.get("status"):
            errors.append(f"{claim_id}: source status mismatch")
        if (overlay.get("durability_state"), overlay.get("public_use")) != expected:
            errors.append(f"{claim_id}: durability disposition changed")
        if not isinstance(overlay.get("reason"), str) or not overlay["reason"]:
            errors.append(f"{claim_id}: missing durability reason")
        if (
            source.get("source_hash") == "UNKNOWN"
            or source.get("archive_status") in {"NOT_CAPTURED", "NEGATIVE_SEARCH_NOT_PRESERVED"}
            or "MUTABLE" in str(source.get("consulted_version"))
        ) and overlay.get("durability_state") in {"DURABLE", "SUPPORTED_DURABLE"}:
            errors.append(f"{claim_id}: unavailable evidence was upgraded to durable")

    novelty = overlay_by_id.get("whole-hybrid-pipeline-whitespace")
    search = novelty.get("search_record") if isinstance(novelty, dict) else None
    if not isinstance(search, dict):
        errors.append("novelty claim lacks historical search boundary")
    else:
        if search.get("execution_state") != "HISTORICAL_SEARCH_UNPRESERVED":
            errors.append("historical search execution state changed")
        for field in EXPECTED_UNKNOWN_SEARCH_FIELDS:
            value = search.get(field)
            if (
                not isinstance(value, dict)
                or value.get("status") != "UNKNOWN"
                or not isinstance(value.get("reason"), str)
                or not value["reason"]
            ):
                errors.append(f"historical search {field} must remain explained UNKNOWN")
        if search.get("claim_disposition") != "NOT_ESTABLISHED":
            errors.append("historical novelty claim disposition changed")
        if search.get("universal_claim_allowed") is not False:
            errors.append("universal novelty claim must remain prohibited")
        if search.get("allowed_wording") != EXPECTED_ALLOWED_WORDING:
            errors.append("allowed novelty wording is not fail-closed")
        candidates = search.get("preserved_candidate_assertions")
        observed_candidates = (
            {
                (row.get("candidate"), row.get("canonical_identifier"))
                for row in candidates
                if isinstance(row, dict)
                and row.get("evidence_state") == "NAMED_IN_SOURCE_NOTE_ONLY"
            }
            if isinstance(candidates, list)
            else set()
        )
        if observed_candidates != EXPECTED_CANDIDATES or len(candidates or []) != len(
            EXPECTED_CANDIDATES
        ):
            errors.append("historical search candidate assertions changed")
        negatives = search.get("preserved_negative_assertions")
        observed_negatives = (
            {
                row.get("assertion")
                for row in negatives
                if isinstance(row, dict)
                and row.get("evidence_state")
                == "UNREPRODUCIBLE_SOURCE_NOTE_ASSERTION"
            }
            if isinstance(negatives, list)
            else set()
        )
        if observed_negatives != EXPECTED_NEGATIVE_ASSERTIONS or len(
            negatives or []
        ) != len(EXPECTED_NEGATIVE_ASSERTIONS):
            errors.append("historical search negative assertions changed")

    protocol = data.get("future_search_protocol")
    if not isinstance(protocol, dict):
        errors.append("missing future search protocol")
    else:
        if protocol.get("protocol_id") != "engraph-novelty-search-protocol.v1":
            errors.append("future search protocol ID changed")
        if protocol.get("state") != "REQUIRED_BEFORE_REASSERTION":
            errors.append("future search protocol is not required")
        capture = protocol.get("required_capture")
        if not isinstance(capture, list) or set(capture) != EXPECTED_PROTOCOL_FIELDS:
            errors.append("future search protocol capture set is incomplete")
        if protocol.get("decision_rule") != EXPECTED_DECISION_RULE:
            errors.append("future search decision rule is not fail-closed")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--overlay", type=Path, default=OVERLAY)
    args = parser.parse_args()
    try:
        data = read_json(args.overlay)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable overlay: {exc}") from exc
    errors = verify(data)
    if errors:
        raise SystemExit("\n".join(errors))
    print("OK: Engraph claim durability overlay")


if __name__ == "__main__":
    main()
