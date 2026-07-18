#!/usr/bin/env python3
"""Run, create, and verify no-clobber engraph research receipts.

The ``run`` path records a planned event before executing an argv-only command.
The ``create`` path binds an already-produced raw output and is explicitly
posthoc. Receipt self-digests detect accidental corruption but are unsigned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_V1 = "research-claim-run-manifest.v1"
SCHEMA_V2 = "research-claim-run-manifest.v2"
SCHEMA = SCHEMA_V1
WRITE_SCHEMA = SCHEMA_V2
SUPPORTED_SCHEMAS = frozenset({SCHEMA_V1, SCHEMA_V2})
V2_CONTRACT_ID = (
    "https://schemas.local.invalid/research-claim-run-manifest/v2/schema.json"
)
V2_CONTRACT_SHA256 = (
    "a278934b14bdfcf4bb911680c0f67f58ec9a3a2fb1f62e78d47e70dbcc298d43"
)
V2_SCHEMA_PATH = (
    ROOT / "research-evidence/contracts/v2/research-claim-run-manifest-v2.schema.json"
)
MODEL_MANIFEST = ROOT / "models/model-manifest-v1.json"
TERMINAL_STATES = {"completed", "failed", "timeout", "aborted", "null"}
COMPARABILITY_CLASSES = {
    "same_manifest_attempt",
    "hardware_variant",
    "runtime_variant",
    "incomparable",
}
TERMINAL_UNAVAILABLE_ERRORS = {
    "tool binary unavailable",
    "model artifact unavailable",
    "model acquisition receipt unavailable",
    "external tokenizer unavailable",
    "configuration unavailable",
}
SECRET_ARGUMENT_NAMES = {
    "api-key",
    "api_key",
    "authorization",
    "credential",
    "passwd",
    "password",
    "secret",
    "token",
}
RESEARCH_ENVIRONMENT_KEYS = (
    "ENGRAPH_P3_NCTX",
    "FABLE_N_GPU_LAYERS",
    "LLAMA_ARG_N_GPU_LAYERS",
    "OMP_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "T1_PASSES",
)


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )


def _unknown_artifact(reason: str) -> dict[str, str]:
    return {"status": "UNKNOWN", "reason": reason}


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= set("0123456789abcdef")
    )


def _require_v2_contract_bytes() -> None:
    if not V2_SCHEMA_PATH.is_file() or sha256(V2_SCHEMA_PATH) != V2_CONTRACT_SHA256:
        raise ValueError("v2 contract bytes differ from the pinned writer contract")


def _verify_v2_artifact(value: Any, label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} is not an object"]
    if value.get("status") == "UNKNOWN":
        return (
            []
            if isinstance(value.get("reason"), str) and value["reason"]
            else [f"{label} UNKNOWN has no reason"]
        )
    if value.get("status") != "BOUND":
        return [f"{label} status is unsupported"]
    errors = []
    digest = value.get("sha256")
    if not _is_digest(digest):
        errors.append(f"{label} digest is invalid")
    if not isinstance(value.get("bytes"), int) or value["bytes"] < 0:
        errors.append(f"{label} byte count is invalid")
    if not value.get("locator") or value.get("locator_kind") not in {
        "repository_relative",
        "evidence_root_relative",
        "home_relative",
        "content_addressed",
    }:
        errors.append(f"{label} locator is unavailable")
    return errors


def _v2_artifact(
    value: dict[str, Any] | None,
    *,
    home: bool = False,
    sensitivity: str | None = None,
) -> dict[str, Any]:
    value = value or {}
    locator_field = "home_relative_path" if home else "path"
    locator = value.get(locator_field)
    digest = value.get("sha256")
    byte_count = value.get("bytes")
    if (
        not isinstance(locator, str)
        or locator in {"", "UNKNOWN"}
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(byte_count, int)
    ):
        return _unknown_artifact("A byte-counted artifact is not bound.")
    artifact = {
        "status": "BOUND",
        "sha256": digest,
        "bytes": byte_count,
        "locator_kind": "home_relative" if home else "repository_relative",
        "locator": locator,
    }
    if sensitivity is not None:
        artifact["sensitivity"] = sensitivity
    return artifact


def _capture(value: Any, reason: str) -> dict[str, str]:
    if value is None or value == "UNKNOWN":
        return _unknown_artifact(reason)
    return {"status": "BOUND", "sha256": canonical_digest(value)}


def _rights_unknown() -> dict[str, Any]:
    return {
        "declared_license": {
            "status": "UNKNOWN",
            "value": "UNKNOWN",
            "reason": "No versioned license artifact is bound.",
            "evidence": _unknown_artifact("No versioned license artifact is bound."),
        },
        "license_conclusion": {
            "status": "NOT_ASSERTED",
            "reason": "No legal conclusion is authorized.",
            "evidence": _unknown_artifact(
                "No versioned license evidence is bound."
            ),
        },
        "consent_status": {
            "status": "UNKNOWN",
            "reason": "Consent evidence is unavailable.",
            "evidence": _unknown_artifact("No consent artifact is bound."),
        },
        "redistribution_status": {
            "status": "UNKNOWN",
            "reason": "Redistribution rights were not established.",
            "evidence": _unknown_artifact("No redistribution artifact is bound."),
        },
        "derivative_use_status": {
            "status": "UNKNOWN",
            "reason": "Derivative-use rights were not established.",
            "evidence": _unknown_artifact("No derivative-use artifact is bound."),
        },
    }


def _verify_adapter_rights(rights: Any, label: str) -> list[str]:
    if not isinstance(rights, dict):
        return [f"{label} is not an object"]
    expected = {
        "declared_license": "UNKNOWN",
        "license_conclusion": "NOT_ASSERTED",
        "consent_status": "UNKNOWN",
        "redistribution_status": "UNKNOWN",
        "derivative_use_status": "UNKNOWN",
    }
    errors = []
    if set(rights) != set(expected):
        errors.append(f"{label} fields differ")
    for name, status in expected.items():
        decision = rights.get(name) or {}
        if decision.get("status") != status:
            errors.append(f"{label}.{name} status exceeds adapter authority")
        if not decision.get("reason"):
            errors.append(f"{label}.{name} has no reason")
        evidence = decision.get("evidence") or {}
        if evidence.get("status") != "UNKNOWN" or not evidence.get("reason"):
            errors.append(f"{label}.{name} evidence is not fail-closed UNKNOWN")
    if (rights.get("declared_license") or {}).get("value") != "UNKNOWN":
        errors.append(f"{label}.declared_license value exceeds adapter authority")
    return errors


def _v2_envelope(
    record: dict[str, Any],
    *,
    event_id: str,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    """Add the shared v2 envelope while retaining verified v1-compatible fields."""
    _require_v2_contract_bytes()
    model = record.get("model") or {}
    runtime = record.get("runtime") or {}
    repository = dict(record.get("repository") or {})
    repository.setdefault("branch", "UNKNOWN")
    repository.setdefault("replay_material", "UNKNOWN")
    prompt = _v2_artifact(record.get("prompt_or_fixture"))
    plan = _v2_artifact(record.get("planned_event"))
    exact_input = (
        plan
        if plan.get("status") == "BOUND"
        else _v2_artifact(record.get("input_manifest"))
    )
    if exact_input.get("status") != "BOUND":
        exact_input = prompt
    if exact_input.get("status") != "BOUND":
        exact_input = _unknown_artifact("No exact input artifact is bound.")
    tokenizer = (
        {
            "status": "BOUND",
            "sha256": model["sha256"],
            "bytes": model["bytes"],
            "locator_kind": "content_addressed",
            "locator": f"sha256:{model['sha256']}",
            "sensitivity": "private_not_embedded",
        }
        if (
            isinstance(model.get("sha256"), str)
            and len(model["sha256"]) == 64
            and isinstance(model.get("bytes"), int)
        )
        else _unknown_artifact("Tokenizer artifact bytes are unavailable.")
    )
    record_type = record["record_type"]
    timestamp = record["timestamp"]
    state = record["state"]
    result = {
        **record,
        "schema": WRITE_SCHEMA,
        "contract": {
            "id": V2_CONTRACT_ID,
            "sha256": V2_CONTRACT_SHA256,
            "source_schema": "ENGRAPH_V1_ADAPTER",
        },
        "producer": {"system": "ENGRAPH", "version": "v1-adapter"},
        "event_id": event_id,
        "outcome": {
            "state": state,
            "started_at": (
                timestamp
                if record_type == "attempt_planned"
                else record.get("run_started_at", "UNKNOWN")
            ),
            "ended_at": (
                "UNKNOWN"
                if record_type == "attempt_planned"
                else record.get("run_ended_at", timestamp)
            ),
            "exit_code": record.get("exit_code", "UNKNOWN"),
            "failure_reason": (
                "measured command failed"
                if state in {"failed", "timeout", "aborted"}
                else ""
            ),
            "null_reason": "unspecified null result" if state == "null" else "",
        },
        "evidence": {
            "exact_input": exact_input,
            "case": prompt,
            "prompt": prompt,
            "system_harness": _v2_artifact(record.get("tool_binary")),
            "dependency_lock": _v2_artifact(
                (record.get("dependencies") or {}).get("cargo_lock")
            ),
            "model": _v2_artifact(
                model, home=True, sensitivity="private_not_embedded"
            ),
            "tokenizer": tokenizer,
            "raw_output": _v2_artifact(
                record.get("raw_output"),
                sensitivity=record.get("sensitivity", "UNKNOWN"),
            ),
            "scorer": _v2_artifact(record.get("scorer")),
            "summary": _v2_artifact(record.get("summary")),
        },
        "repository": repository,
        "environment": {
            "cli": _capture(
                record.get("command_argv"),
                "Measured command capture is unavailable.",
            ),
            "runtime": _capture(runtime, "Runtime capture is unavailable."),
            "toolchain": _capture(
                {
                    "rust_toolchain_declared": runtime.get(
                        "rust_toolchain_declared"
                    ),
                    "rustc": runtime.get("rustc"),
                    "cargo": runtime.get("cargo"),
                    "cmake": runtime.get("cmake"),
                },
                "Toolchain capture is unavailable.",
            ),
            "hardware": _capture(
                runtime.get("hardware"),
                "Hardware capture is unavailable.",
            ),
            "os": _capture(
                {
                    "os": runtime.get("os"),
                    "os_release": runtime.get("os_release"),
                    "architecture": runtime.get("architecture"),
                },
                "OS capture is unavailable.",
            ),
            "seed": _capture(
                record.get("seed"),
                "Seed capture is unavailable.",
            ),
            "settings": _capture(
                record.get("settings"),
                "Settings capture is unavailable.",
            ),
        },
        "model_identity": {
            "requested": str(model.get("uri") or "UNKNOWN"),
            "observed": (
                [str(model["object_identity"])]
                if model.get("object_identity")
                else "UNKNOWN"
            ),
            "artifact_sha256": model.get("sha256", "UNKNOWN"),
            "tokenizer_sha256": (
                tokenizer.get("sha256")
                if tokenizer.get("status") == "BOUND"
                else "UNKNOWN"
            ),
        },
        "rights": _rights_unknown(),
        "comparison": {
            "class": record.get("comparability_class", "unclassified"),
            "baseline": _unknown_artifact(
                "No authenticated external baseline is bound."
            ),
            "errors": [],
        },
        "integrity": {
            "self_digest": _unknown_artifact(
                "RFC8785/JCS self-digest production is not implemented."
            ),
            "canonicalization": "UNKNOWN",
            "authentication": "UNKNOWN",
        },
    }
    if parent_event_id is not None:
        result["parent_event_id"] = parent_event_id
    return result


def git_text(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "UNKNOWN"


def git_bytes(*args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else b""


def _safe_repo_relative(value: str) -> Path:
    posix = PurePosixPath(value)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise ValueError(f"unsafe repository-relative path: {value!r}")
    return Path(*posix.parts)


def _repo_artifact(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"artifact must not be a symlink: {path}")
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"artifact must be inside repository: {resolved}") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"artifact must be a regular file: {resolved}")
    return {
        "path": relative.as_posix(),
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def _home_artifact(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"model artifact must not be a symlink: {path}")
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(Path.home().resolve())
    except ValueError as exc:
        raise ValueError(f"model artifact must be below HOME: {resolved}") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"model artifact must be a regular file: {resolved}")
    return {
        "home_relative_path": relative.as_posix(),
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def _model_manifest_entry(uri: str) -> tuple[dict[str, Any], str]:
    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != "engraph-model-manifest.v1":
        raise ValueError("unsupported engraph model manifest")
    matches = [row for row in manifest.get("models", []) if row.get("uri") == uri]
    if len(matches) != 1:
        raise ValueError(f"model URI must have exactly one manifest entry: {uri}")
    return matches[0], sha256(MODEL_MANIFEST)


def _command_output(*argv: str) -> str:
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    output = proc.stdout.strip()
    return output if proc.returncode == 0 and output else "UNKNOWN"


def _runtime_context(rust_toolchain: str) -> dict[str, Any]:
    cpu = platform.processor() or "UNKNOWN"
    memory_bytes = "UNKNOWN"
    if platform.system() == "Darwin":
        cpu = _command_output("sysctl", "-n", "machdep.cpu.brand_string")
        memory_bytes = _command_output("sysctl", "-n", "hw.memsize")
    elif platform.system() == "Linux":
        cpu = _command_output("uname", "-p")
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    memory_bytes = str(int(line.split()[1]) * 1024)
                    break
        except (OSError, ValueError, IndexError):
            pass
    gpu_identity = "UNKNOWN"
    if platform.system() == "Darwin":
        display_data = _command_output(
            "system_profiler", "SPDisplaysDataType", "-json"
        )
        if display_data != "UNKNOWN":
            gpu_identity = f"sha256:{sha256_bytes(display_data.encode())}"
    load_average = list(os.getloadavg()) if hasattr(os, "getloadavg") else "UNKNOWN"
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "hardware": {
            "platform": platform.platform(),
            "processor": cpu,
            "logical_cpu_count": os.cpu_count() or "UNKNOWN",
            "memory_bytes": memory_bytes,
            "gpu_display_identity": gpu_identity,
            "load_average_1m_5m_15m": load_average,
        },
        "python": platform.python_version(),
        "rust_toolchain_declared": rust_toolchain,
        "rustc": _command_output("rustc", "--version", "--verbose"),
        "cargo": _command_output("cargo", "--version", "--verbose"),
        "cmake": _command_output("cmake", "--version"),
        "research_environment": {
            key: os.environ.get(key, "UNSET") for key in RESEARCH_ENVIRONMENT_KEYS
        },
        "environment_capture": "ALLOWLIST_ONLY_NO_SECRETS",
    }


def _require_runtime_declaration(
    comparability_class: str, runtime: dict[str, Any]
) -> None:
    if comparability_class != "same_manifest_attempt":
        return
    declared = runtime.get("rust_toolchain_declared")
    observed = runtime.get("rustc")
    observed_version = (
        observed.splitlines()[0]
        if isinstance(observed, str) and observed != "UNKNOWN"
        else None
    )
    if not isinstance(declared, str) or declared != observed_version:
        raise ValueError(
            "same-manifest declared Rust toolchain differs from observed rustc"
        )


def _model_acquisition(
    model_path: Path,
    uri: str,
    manifest_entry: dict[str, Any],
) -> dict[str, Any]:
    receipt_path = Path(f"{model_path.resolve()}.provenance.json")
    if not receipt_path.exists():
        return {
            "status": "UNKNOWN_LEGACY_CACHE",
            "reason": "No acquisition receipt exists for this cached artifact.",
        }
    artifact = _home_artifact(receipt_path)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"model acquisition receipt is unreadable: {type(exc).__name__}"
        ) from exc
    repo_and_file = uri.removeprefix("hf:")
    repo, filename = repo_and_file.rsplit("/", 1)
    expected = {
        "schema": "engraph-model-acquisition.v1",
        "uri": uri,
        "download_url": (
            f"https://huggingface.co/{repo}/resolve/"
            f"{manifest_entry['upstream_revision']}/{filename}"
        ),
        "artifact_sha256": manifest_entry["artifact_sha256"],
        "object_identity": manifest_entry["object_identity"],
        "upstream_revision": manifest_entry["upstream_revision"],
        "base_model": manifest_entry["base_model"],
        "tokenizer_identity": manifest_entry["tokenizer_identity"],
        "declared_license": manifest_entry["declared_license"],
        "license_evidence_url": manifest_entry["license_evidence_url"],
        "license_conclusion": "NOT_ASSERTED",
        "redistribution_status": "UNKNOWN",
    }
    if (
        any(receipt.get(key) != value for key, value in expected.items())
        or not isinstance(receipt.get("acquired_at_unix"), int)
        or receipt["acquired_at_unix"] <= 0
    ):
        raise ValueError("model acquisition receipt does not match manifest")
    return {
        "status": "LOCAL_SIDECAR_BOUND_UNAUTHENTICATED",
        "artifact": artifact,
        "acquired_at_unix_claimed": receipt["acquired_at_unix"],
    }


def _dirty_context(exclude_paths: tuple[str, ...] = ()) -> dict[str, Any]:
    tracked_diff = git_bytes("diff", "--binary", "--no-ext-diff", "HEAD", "--", ".")
    untracked_output = git_bytes(
        "ls-files", "--others", "--exclude-standard", "-z"
    )
    untracked = []
    for value in sorted(filter(None, untracked_output.decode().split("\0"))):
        if value in exclude_paths:
            continue
        relative = _safe_repo_relative(value)
        path = ROOT / relative
        if path.is_file() and not path.is_symlink():
            untracked.append(
                {
                    "path": relative.as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
        else:
            untracked.append(
                {
                    "path": relative.as_posix(),
                    "bytes": "UNAVAILABLE",
                    "sha256": "UNAVAILABLE",
                }
            )
    payload = {
        "tracked_diff_sha256": sha256_bytes(tracked_diff),
        "tracked_diff_bytes": len(tracked_diff),
        "untracked": untracked,
    }
    return {
        **payload,
        "dirty": bool(tracked_diff or untracked),
        "dirty_state_sha256": canonical_digest(payload),
        "replay_material": (
            "digest_only_not_reconstructable"
            if tracked_diff or untracked
            else "clean_commit"
        ),
    }


def _repository_state(exclude_paths: tuple[str, ...] = ()) -> dict[str, Any]:
    repository = _dirty_context(exclude_paths)
    repository.update(
        {
            "commit": git_text("rev-parse", "HEAD"),
            "branch": git_text("branch", "--show-current") or "UNKNOWN",
        }
    )
    return repository


def _optional_repo_artifact(value: Path | None) -> dict[str, Any]:
    return _repo_artifact(value) if value else {
        "path": "UNKNOWN",
        "bytes": "UNKNOWN",
        "sha256": "UNKNOWN",
    }


def _configuration(mode: str, path: Path | None) -> dict[str, Any]:
    if mode == "builtin_default":
        if path is not None:
            raise ValueError("builtin-default configuration cannot bind a file")
        return {"mode": "builtin_default"}
    if mode == "file":
        if path is None:
            raise ValueError("file configuration requires --config")
        return {"mode": "file", "artifact": _home_artifact(path)}
    raise ValueError("unsupported configuration mode")


def _parse_settings(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("settings JSON must be an object")
    return parsed


def _contains_unknown(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unknown(item) for item in value)
    return value == "UNKNOWN"


def _stable_runtime(value: dict[str, Any]) -> dict[str, Any]:
    stable = json.loads(json.dumps(value))
    (stable.get("hardware") or {}).pop("load_average_1m_5m_15m", None)
    return stable


def _safe_command_argv(argv: list[str]) -> list[str]:
    def localize_path(value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            return value
        resolved = path.resolve()
        try:
            return f"$REPO/{resolved.relative_to(ROOT).as_posix()}"
        except ValueError:
            try:
                return f"$HOME/{resolved.relative_to(Path.home().resolve()).as_posix()}"
            except ValueError:
                return f"$ABS_PATH_SHA256/{sha256_bytes(str(resolved).encode())}"

    safe = []
    for index, value in enumerate(argv):
        lower = value.lower()
        previous = argv[index - 1].lower() if index else ""
        name = lower.lstrip("-").split("=", 1)[0]
        previous_name = previous.lstrip("-").split("=", 1)[0]
        if (
            name in SECRET_ARGUMENT_NAMES
            or previous_name in SECRET_ARGUMENT_NAMES
            or lower.startswith("bearer ")
            or lower.startswith("sk-")
        ):
            raise ValueError("secret-bearing command arguments are not permitted")
        if "=" in value:
            name, candidate = value.split("=", 1)
            value = f"{name}={localize_path(candidate)}"
        else:
            value = localize_path(value)
        safe.append(value)
    return safe


def _validate_tool_command(tool_binary: Path | None, command_argv: list[str]) -> None:
    if tool_binary is None:
        raise ValueError("measured runs require a bound tool binary")
    command = command_argv[0]
    located = shutil.which(command) if "/" not in command else command
    if located is None:
        raise ValueError("measured command executable cannot be resolved")
    command_path = Path(located)
    if not command_path.is_absolute():
        command_path = ROOT / command_path
    if command_path.resolve() != tool_binary.resolve():
        raise ValueError("measured argv[0] does not match bound tool binary")
    if not os.access(tool_binary.resolve(), os.X_OK):
        raise ValueError("bound tool binary is not executable")


def _terminate_process_group(
    process_group_id: int,
    direct_process: subprocess.Popen[bytes] | None = None,
) -> None:
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        if direct_process is not None:
            direct_process.wait()
        return
    if direct_process is not None:
        direct_process.wait()
    deadline = time.monotonic() + 5
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("measured command process group did not terminate")
        time.sleep(0.01)


def _validate_kind_args(args: argparse.Namespace) -> None:
    if args.kind == "performance" and (
        args.input_manifest is None
        or args.scorer is None
        or args.summary is None
        or args.tool_binary is None
        or args.seed == "UNKNOWN"
    ):
        raise ValueError(
            "performance receipts require input manifest, tool, scorer, "
            "summary, and explicit seed"
        )


def create_receipt(args: argparse.Namespace) -> dict[str, Any]:
    raw = _repo_artifact(args.raw)
    model = _home_artifact(args.model)
    output = args.output.resolve()
    if not args.command.strip():
        raise ValueError("measured command must be recorded")
    declared_argv = getattr(args, "_command_argv", None)
    if declared_argv is None:
        declared_argv = _safe_command_argv(shlex.split(args.command))
        args.command = " ".join(declared_argv)
    if args.state not in TERMINAL_STATES:
        raise ValueError("unsupported terminal state")
    if args.comparability not in COMPARABILITY_CLASSES:
        raise ValueError("unsupported comparability class")
    if args.state == "completed" and args.exit_code != 0:
        raise ValueError("completed receipts require exit code 0")
    if args.state == "failed" and args.exit_code == 0:
        raise ValueError("failed receipts require a nonzero exit code")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite receipt: {output}")

    manifest_entry, manifest_sha256 = _model_manifest_entry(args.model_uri)
    if model["sha256"] != manifest_entry.get("artifact_sha256"):
        raise ValueError("local model does not match manifested model identity")
    if args.tokenizer:
        raise ValueError(
            "external tokenizer receipts require a separately manifested URI"
        )
    tokenizer = {
        "source": "embedded_in_model",
        "model_artifact_sha256": model["sha256"],
        "sha256": model["sha256"],
    }
    repository = getattr(args, "_repository", None) or _repository_state()
    repository_after = getattr(args, "_repository_after", None) or repository
    execution_provenance = (
        "tool_executed"
        if getattr(args, "_executed_by_tool", False)
        else "posthoc_declared"
    )
    runtime = getattr(args, "_runtime", None) or _runtime_context(
        args.rust_toolchain
    )
    runtime_after = getattr(args, "_runtime_after", None) or runtime
    settings = _parse_settings(args.settings_json)
    prompt_or_fixture = _optional_repo_artifact(args.prompt_or_fixture)
    tool_binary = _optional_repo_artifact(args.tool_binary)
    dependencies = {
        "cargo_lock": _repo_artifact(ROOT / "Cargo.lock"),
        "cargo_toml": _repo_artifact(ROOT / "Cargo.toml"),
    }
    configuration = _configuration(args.config_mode, args.config)
    input_manifest = _optional_repo_artifact(args.input_manifest)
    acquisition = _model_acquisition(args.model, args.model_uri, manifest_entry)
    _validate_kind_args(args)
    if args.comparability == "same_manifest_attempt":
        if execution_provenance != "tool_executed":
            raise ValueError("same-manifest attempt must be executed by the tool")
        if repository["dirty"] or repository_after["dirty"]:
            raise ValueError("same-manifest attempt requires a clean repository")
        if repository_after["commit"] != repository["commit"]:
            raise ValueError("same-manifest attempt changed commit during execution")
        if args.prompt_or_fixture is None or args.tool_binary is None:
            raise ValueError("same-manifest attempt requires fixture and tool artifacts")
        if args.seed == "UNKNOWN" or _contains_unknown(runtime):
            raise ValueError("same-manifest attempt requires complete run capture")
        if args.process_policy != "source_reviewed_no_process_spawning":
            raise ValueError(
                "same-manifest attempt requires a source-reviewed non-spawning tool"
            )
        if _stable_runtime(runtime_after) != _stable_runtime(runtime):
            raise ValueError("stable runtime identity changed during measured command")
        plan_record = getattr(args, "_plan_record", None)
        if plan_record is None:
            raise ValueError("same-manifest attempt requires a bound planned event")
        planned_inputs = {
            "model": plan_record.get("model"),
            "repository": plan_record.get("repository"),
            "dependencies": plan_record.get("dependencies"),
            "configuration": plan_record.get("configuration"),
            "input_manifest": plan_record.get("input_manifest"),
            "runtime": plan_record.get("runtime"),
            "seed": plan_record.get("seed"),
            "settings": plan_record.get("settings"),
            "prompt_or_fixture": plan_record.get("prompt_or_fixture"),
            "tool_binary": plan_record.get("tool_binary"),
        }
        terminal_inputs = {
            "model": {
                **model,
                "uri": args.model_uri,
                "manifest_sha256": manifest_sha256,
                "object_identity": manifest_entry["object_identity"],
                "acquisition": acquisition,
            },
            "repository": repository,
            "dependencies": dependencies,
            "configuration": configuration,
            "input_manifest": input_manifest,
            "runtime": runtime,
            "seed": args.seed,
            "settings": settings,
            "prompt_or_fixture": prompt_or_fixture,
            "tool_binary": tool_binary,
        }
        if planned_inputs != terminal_inputs:
            raise ValueError(
                "same-manifest inputs changed between plan and result"
            )

    receipt: dict[str, Any] = {
        "schema": WRITE_SCHEMA,
        "record_type": "attempt_result",
        "run_id": args.run_id or f"engraph-{uuid.uuid4()}",
        "attempt_id": getattr(args, "_attempt_id", None) or str(uuid.uuid4()),
        "receipt_created_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "timestamp": args.ended_at,
        "run_started_at": args.started_at,
        "run_ended_at": args.ended_at,
        "state": args.state,
        "exit_code": args.exit_code,
        "case_id": args.claim_id,
        "claim_id": args.claim_id,
        "claim_kind": args.kind,
        "command": args.command,
        "command_argv": declared_argv,
        "execution_provenance": execution_provenance,
        "sensitivity": args.sensitivity,
        "process_policy": args.process_policy,
        "planned_event": _optional_repo_artifact(
            getattr(args, "_planned_event", None)
        ),
        "raw_output": raw,
        "raw_output_path": raw["path"],
        "raw_output_sha256": raw["sha256"],
        "prompt_or_fixture": prompt_or_fixture,
        "tool_binary": tool_binary,
        "scorer": _optional_repo_artifact(args.scorer),
        "summary": _optional_repo_artifact(args.summary),
        "scorer_sha256": (
            sha256(args.scorer.resolve()) if args.scorer else "NOT_APPLICABLE"
        ),
        "summary_sha256": (
            sha256(args.summary.resolve()) if args.summary else "UNKNOWN"
        ),
        "model": {
            **model,
            "uri": args.model_uri,
            "manifest_sha256": manifest_sha256,
            "object_identity": manifest_entry["object_identity"],
            "role": manifest_entry["role"],
            "base_model": manifest_entry["base_model"],
            "upstream_revision": manifest_entry["upstream_revision"],
            "declared_license": manifest_entry["declared_license"],
            "license_evidence_url": manifest_entry["license_evidence_url"],
            "license_conclusion": "NOT_ASSERTED",
            "acquisition_timestamp": manifest_entry["acquisition_timestamp"],
            "verified_at": manifest_entry["verified_at"],
            "acquisition": acquisition,
            "tokenizer": tokenizer,
        },
        "repository": repository,
        "repository_after": repository_after,
        "dependencies": dependencies,
        "configuration": configuration,
        "input_manifest": input_manifest,
        "runtime": runtime,
        "runtime_after": runtime_after,
        "comparability_class": args.comparability,
        "seed": args.seed,
        "settings": settings,
        "receipt_integrity": "SELF_DIGEST_ONLY_NOT_AUTHENTICATED",
    }
    receipt = _v2_envelope(
        receipt,
        event_id=str(uuid.uuid4()),
        parent_event_id=(
            (getattr(args, "_plan_record", None) or {}).get("event_id")
        ),
    )
    receipt["receipt_sha256"] = canonical_digest(receipt)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(output, flags, 0o600)
    try:
        encoded = (
            json.dumps(receipt, indent=2, sort_keys=True) + "\n"
        ).encode()
        written = 0
        while written < len(encoded):
            written += os.write(fd, encoded[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    return receipt


def _verify_repo_artifact(
    artifact: dict[str, Any], label: str, *, optional: bool = False
) -> list[str]:
    if optional and artifact.get("path") == "UNKNOWN":
        return []
    try:
        relative = _safe_repo_relative(artifact.get("path", ""))
    except ValueError:
        return [f"{label} path is unsafe"]
    path = ROOT / relative
    errors = []
    if path.is_symlink() or (path.exists() and not path.is_file()):
        errors.append(f"{label} is not a regular file")
    elif not path.is_file():
        errors.append(f"{label} unavailable")
    else:
        if path.stat().st_size != artifact.get("bytes"):
            errors.append(f"{label} size mismatch")
        if sha256(path) != artifact.get("sha256"):
            errors.append(f"{label} hash mismatch")
    return errors


def _verify_home_artifact(artifact: dict[str, Any], label: str) -> list[str]:
    try:
        relative = _safe_repo_relative(artifact.get("home_relative_path", ""))
    except ValueError:
        return [f"{label} path is unsafe"]
    path = Path.home() / relative
    errors = []
    if path.is_symlink() or (path.exists() and not path.is_file()):
        errors.append(f"{label} is not a regular file")
    elif not path.is_file():
        errors.append(f"{label} unavailable")
    else:
        if path.stat().st_size != artifact.get("bytes"):
            errors.append(f"{label} size mismatch")
        if sha256(path) != artifact.get("sha256"):
            errors.append(f"{label} hash mismatch")
    return errors


def _verify_plan_binding(data: dict[str, Any]) -> list[str]:
    if data.get("execution_provenance") != "tool_executed":
        return []
    artifact = data.get("planned_event") or {}
    try:
        relative = _safe_repo_relative(artifact.get("path", ""))
        plan = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"planned event unreadable: {type(exc).__name__}"]
    model = data.get("model") or {}
    terminal_model = {
        key: model.get(key)
        for key in (
            "home_relative_path",
            "bytes",
            "sha256",
            "uri",
            "manifest_sha256",
            "object_identity",
            "acquisition",
        )
    }
    expected = {
        "run_id": data.get("run_id"),
        "attempt_id": data.get("attempt_id"),
        "case_id": data.get("case_id"),
        "claim_kind": data.get("claim_kind"),
        "command_argv": data.get("command_argv"),
        "model": terminal_model,
        "repository": data.get("repository"),
        "dependencies": data.get("dependencies"),
        "configuration": data.get("configuration"),
        "input_manifest": data.get("input_manifest"),
        "runtime": data.get("runtime"),
        "seed": data.get("seed"),
        "settings": data.get("settings"),
        "prompt_or_fixture": data.get("prompt_or_fixture"),
        "tool_binary": data.get("tool_binary"),
        "comparability_class": data.get("comparability_class"),
        "sensitivity": data.get("sensitivity"),
        "process_policy": data.get("process_policy"),
    }
    actual = {key: plan.get(key) for key in expected}
    errors = (
        [] if actual == expected else ["planned event inputs differ from result"]
    )
    if data.get("schema") == SCHEMA_V2:
        if plan.get("schema") != SCHEMA_V2:
            errors.append("planned event schema differs from result")
        if plan.get("record_type") != "attempt_planned":
            errors.append("planned event record type mismatch")
        if data.get("parent_event_id") != plan.get("event_id"):
            errors.append("result parent does not reference planned event")
        if data.get("event_id") == plan.get("event_id"):
            errors.append("plan and result event ids are not distinct")
        for field in ("contract", "producer", "run_id", "attempt_id", "case_id"):
            if data.get(field) != plan.get(field):
                errors.append(f"planned event {field} differs from result")
    return errors


def _verify_v2_envelope(data: dict[str, Any]) -> list[str]:
    if data.get("schema") != SCHEMA_V2:
        return []
    errors = []
    contract = data.get("contract") or {}
    if (
        contract.get("id") != V2_CONTRACT_ID
        or contract.get("sha256") != V2_CONTRACT_SHA256
        or contract.get("source_schema") != "ENGRAPH_V1_ADAPTER"
    ):
        errors.append("v2 contract binding mismatch")
    if (data.get("producer") or {}).get("system") != "ENGRAPH":
        errors.append("v2 producer mismatch")
    if not data.get("event_id"):
        errors.append("v2 event id missing")
    outcome = data.get("outcome") or {}
    if outcome.get("state") != data.get("state"):
        errors.append("v2 outcome state mismatch")
    required = {
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
    if set(data.get("evidence") or {}) != required:
        errors.append("v2 evidence fields differ")
    for name, artifact in (data.get("evidence") or {}).items():
        errors.extend(_verify_v2_artifact(artifact, f"v2 evidence.{name}"))
    for field in ("commit", "branch", "dirty", "dirty_state_sha256", "replay_material"):
        if field not in (data.get("repository") or {}):
            errors.append(f"v2 repository missing {field}")
    environment = data.get("environment") or {}
    required_environment = {
        "cli", "runtime", "toolchain", "hardware", "os", "seed", "settings"
    }
    if set(environment) != required_environment:
        errors.append("v2 environment fields differ")
    for name, capture in environment.items():
        if not isinstance(capture, dict) or capture.get("status") not in {
            "BOUND", "UNKNOWN"
        }:
            errors.append(f"v2 environment.{name} status is unsupported")
        elif capture.get("status") == "UNKNOWN" and not capture.get("reason"):
            errors.append(f"v2 environment.{name} UNKNOWN has no reason")
        elif capture.get("status") == "BOUND" and not _is_digest(
            capture.get("sha256")
        ):
            errors.append(f"v2 environment.{name} digest is invalid")
    model_identity = data.get("model_identity") or {}
    for field in ("requested", "observed", "artifact_sha256", "tokenizer_sha256"):
        if field not in model_identity:
            errors.append(f"v2 model identity missing {field}")
    if not model_identity.get("requested"):
        errors.append("v2 requested model identity is unavailable")
    if model_identity.get("observed") != "UNKNOWN" and not (
        isinstance(model_identity.get("observed"), list)
        and bool(model_identity["observed"])
        and all(isinstance(value, str) and value for value in model_identity["observed"])
    ):
        errors.append("v2 observed model identity is invalid")
    for field in ("artifact_sha256", "tokenizer_sha256"):
        if model_identity.get(field) != "UNKNOWN" and not _is_digest(
            model_identity.get(field)
        ):
            errors.append(f"v2 model identity {field} is invalid")
    rights = data.get("rights") or {}
    errors.extend(_verify_adapter_rights(rights, "v2 rights"))
    comparison = data.get("comparison") or {}
    if not {"class", "baseline", "errors"} <= set(comparison):
        errors.append("v2 comparison fields are incomplete")
    else:
        errors.extend(
            _verify_v2_artifact(comparison["baseline"], "v2 comparison baseline")
        )
    integrity = data.get("integrity") or {}
    if (
        (integrity.get("self_digest") or {}).get("status") != "UNKNOWN"
        or integrity.get("canonicalization") != "UNKNOWN"
        or integrity.get("authentication") != "UNKNOWN"
    ):
        errors.append("v2 integrity boundary is overstated")
    return errors


def verify_receipt(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"receipt unreadable: {type(exc).__name__}"]
    errors = []
    if data.get("schema") not in SUPPORTED_SCHEMAS:
        errors.append("unsupported schema")
    errors.extend(_verify_v2_envelope(data))
    if data.get("record_type") != "attempt_result":
        errors.append("unsupported record type")
    if data.get("state") not in TERMINAL_STATES:
        errors.append("receipt has unsupported terminal state")
    if data.get("comparability_class") not in COMPARABILITY_CLASSES:
        errors.append("receipt has unsupported comparability class")
    if data.get("sensitivity") not in {"synthetic", "public_non_sensitive"}:
        errors.append("receipt has unsupported sensitivity")
    if data.get("process_policy") not in {
        "group_contained",
        "source_reviewed_no_process_spawning",
    }:
        errors.append("receipt has unsupported process policy")
    if data.get("receipt_integrity") != "SELF_DIGEST_ONLY_NOT_AUTHENTICATED":
        errors.append("receipt integrity boundary unavailable")
    if data.get("state") == "completed" and data.get("exit_code") != 0:
        errors.append("completed receipt has nonzero exit code")
    if data.get("state") == "failed" and data.get("exit_code") == 0:
        errors.append("failed receipt has zero exit code")
    recorded_digest = data.get("receipt_sha256")
    digest_payload = {
        key: value for key, value in data.items() if key != "receipt_sha256"
    }
    if recorded_digest != canonical_digest(digest_payload):
        errors.append("receipt digest mismatch")

    required = {
        "run_id",
        "attempt_id",
        "timestamp",
        "run_started_at",
        "run_ended_at",
        "state",
        "exit_code",
        "case_id",
        "raw_output_sha256",
        "scorer_sha256",
        "summary_sha256",
        "comparability_class",
        "receipt_sha256",
    }
    if missing := sorted(required - data.keys()):
        errors.append(f"missing required fields: {', '.join(missing)}")

    errors.extend(_verify_repo_artifact(data.get("raw_output") or {}, "raw output"))
    for field, label in (
        ("prompt_or_fixture", "prompt or fixture"),
        ("tool_binary", "tool binary"),
        ("scorer", "scorer"),
        ("summary", "summary"),
        ("planned_event", "planned event"),
    ):
        errors.extend(
            _verify_repo_artifact(data.get(field) or {}, label, optional=True)
        )
    errors.extend(
        _verify_repo_artifact(
            data.get("input_manifest") or {},
            "input manifest",
            optional=True,
        )
    )
    errors.extend(_verify_plan_binding(data))
    if (data.get("raw_output") or {}).get("sha256") != data.get(
        "raw_output_sha256"
    ):
        errors.append("raw output compatibility digest mismatch")

    model_info = data.get("model") or {}
    try:
        model_relative = _safe_repo_relative(
            model_info.get("home_relative_path", "")
        )
    except ValueError:
        errors.append("model path is unsafe")
        model_path = None
    else:
        model_path = Path.home() / model_relative
    if model_path is not None:
        if model_path.is_symlink() or (
            model_path.exists() and not model_path.is_file()
        ):
            errors.append("model artifact is not a regular file")
        elif not model_path.is_file():
            errors.append("model artifact unavailable")
        elif (
            model_path.stat().st_size != model_info.get("bytes")
            or sha256(model_path) != model_info.get("sha256")
        ):
            errors.append("model artifact hash mismatch")
    try:
        manifest_entry, manifest_sha256 = _model_manifest_entry(
            model_info.get("uri", "")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"model manifest validation failed: {type(exc).__name__}")
    else:
        if manifest_sha256 != model_info.get("manifest_sha256"):
            errors.append("model manifest digest mismatch")
        if manifest_entry.get("artifact_sha256") != model_info.get("sha256"):
            errors.append("model identity differs from manifest")
        if manifest_entry.get("object_identity") != model_info.get(
            "object_identity"
        ):
            errors.append("model object identity mismatch")
    acquisition = model_info.get("acquisition") or {}
    if acquisition.get("status") == "LOCAL_SIDECAR_BOUND_UNAUTHENTICATED":
        artifact = acquisition.get("artifact") or {}
        try:
            acquisition_relative = _safe_repo_relative(
                artifact.get("home_relative_path", "")
            )
        except ValueError:
            errors.append("model acquisition receipt path is unsafe")
        else:
            acquisition_path = Path.home() / acquisition_relative
            if acquisition_path.is_symlink() or (
                acquisition_path.exists() and not acquisition_path.is_file()
            ):
                errors.append("model acquisition receipt is not a regular file")
            elif not acquisition_path.is_file():
                errors.append("model acquisition receipt unavailable")
            elif (
                acquisition_path.stat().st_size != artifact.get("bytes")
                or sha256(acquisition_path) != artifact.get("sha256")
            ):
                errors.append("model acquisition receipt hash mismatch")
    elif acquisition.get("status") != "UNKNOWN_LEGACY_CACHE":
        errors.append("model acquisition status unavailable")
    tokenizer = model_info.get("tokenizer") or {}
    if tokenizer.get("source") == "embedded_in_model":
        if tokenizer.get("sha256") != model_info.get("sha256"):
            errors.append("embedded tokenizer is not bound to model artifact")
    elif tokenizer.get("source") == "external_artifact":
        try:
            token_relative = _safe_repo_relative(
                tokenizer.get("home_relative_path", "")
            )
        except ValueError:
            errors.append("external tokenizer path is unsafe")
        else:
            token_path = Path.home() / token_relative
            if token_path.is_symlink() or (
                token_path.exists() and not token_path.is_file()
            ):
                errors.append("external tokenizer is not a regular file")
            elif not token_path.is_file():
                errors.append("external tokenizer unavailable")
            elif (
                token_path.stat().st_size != tokenizer.get("bytes")
                or sha256(token_path) != tokenizer.get("sha256")
            ):
                errors.append("external tokenizer hash mismatch")
    else:
        errors.append("tokenizer identity unavailable")

    for field, label in (
        ("cargo_lock", "Cargo.lock"),
        ("cargo_toml", "Cargo.toml"),
    ):
        errors.extend(
            _verify_repo_artifact(
                (data.get("dependencies") or {}).get(field) or {},
                label,
            )
        )
    configuration = data.get("configuration") or {}
    if configuration.get("mode") == "file":
        errors.extend(
            _verify_home_artifact(
                configuration.get("artifact") or {},
                "configuration",
            )
        )
    elif configuration.get("mode") != "builtin_default":
        errors.append("configuration identity unavailable")
    if data.get("claim_kind") == "performance":
        for field, label in (
            ("input_manifest", "input manifest"),
            ("tool_binary", "tool binary"),
            ("scorer", "scorer"),
            ("summary", "summary"),
        ):
            if (data.get(field) or {}).get("path") == "UNKNOWN":
                errors.append(f"performance receipt has no {label}")
        if data.get("seed") == "UNKNOWN":
            errors.append("performance receipt has no explicit seed")
    repository = data.get("repository") or {}
    repository_after = data.get("repository_after") or {}
    if data.get("comparability_class") == "same_manifest_attempt":
        if data.get("execution_provenance") != "tool_executed":
            errors.append("same-manifest receipt was not tool-executed")
        if repository.get("dirty"):
            errors.append("same-manifest receipt has dirty pre-execution repository")
        if repository_after.get("dirty"):
            errors.append("same-manifest receipt has dirty post-execution repository")
        if repository_after.get("commit") != repository.get("commit"):
            errors.append("same-manifest receipt changed commit during execution")
        if (data.get("prompt_or_fixture") or {}).get("path") == "UNKNOWN":
            errors.append("same-manifest receipt has no fixture artifact")
        if (data.get("tool_binary") or {}).get("path") == "UNKNOWN":
            errors.append("same-manifest receipt has no tool binary artifact")
        if data.get("seed") == "UNKNOWN":
            errors.append("same-manifest receipt has no explicit seed")
        if data.get("process_policy") != "source_reviewed_no_process_spawning":
            errors.append("same-manifest tool was not reviewed as non-spawning")
    runtime = data.get("runtime") or {}
    runtime_after = data.get("runtime_after") or {}
    for field in (
        "os",
        "os_release",
        "architecture",
        "hardware",
        "python",
        "rust_toolchain_declared",
        "rustc",
        "cargo",
        "cmake",
        "research_environment",
        "environment_capture",
    ):
        if field not in runtime:
            errors.append(f"runtime capture missing {field}")
    if data.get("comparability_class") == "same_manifest_attempt" and _contains_unknown(
        runtime
    ):
        errors.append("same-manifest receipt has incomplete runtime capture")
    try:
        _require_runtime_declaration(data.get("comparability_class"), runtime)
    except ValueError as exc:
        errors.append(str(exc))
    if data.get("comparability_class") == "same_manifest_attempt" and _stable_runtime(
        runtime_after
    ) != _stable_runtime(runtime):
        errors.append("same-manifest receipt changed stable runtime identity")
    return errors


def verify_receipt_record(path: Path) -> list[str]:
    """Verify a committed receipt without claiming external-byte custody."""
    return [
        error
        for error in verify_receipt(path)
        if error not in TERMINAL_UNAVAILABLE_ERRORS
    ]


def _write_exclusive_json(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = 0
        while written < len(encoded):
            written += os.write(fd, encoded[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def run_measured(args: argparse.Namespace) -> dict[str, Any]:
    command_argv = list(args.measured_command)
    if command_argv and command_argv[0] == "--":
        command_argv = command_argv[1:]
    if not command_argv:
        raise ValueError("measured command argv is required after --")
    _validate_kind_args(args)
    _validate_tool_command(args.tool_binary, command_argv)
    safe_command_argv = _safe_command_argv(command_argv)
    raw = args.raw.resolve()
    plan = args.plan.resolve()
    output = args.output.resolve()
    for path in (raw, plan, output):
        try:
            path.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError(f"run artifact must be inside repository: {path}") from exc
        if path.exists():
            raise FileExistsError(f"refusing to overwrite run artifact: {path}")
    if plan.suffix != ".json" or output.suffix != ".json":
        raise ValueError("plan and terminal receipt paths must use .json")

    model = _home_artifact(args.model)
    manifest_entry, manifest_sha256 = _model_manifest_entry(args.model_uri)
    if model["sha256"] != manifest_entry.get("artifact_sha256"):
        raise ValueError("local model does not match manifested model identity")
    repository = _repository_state()
    if args.comparability == "same_manifest_attempt" and repository["dirty"]:
        raise ValueError("same-manifest attempt requires a clean repository")
    settings = _parse_settings(args.settings_json)
    runtime = _runtime_context(args.rust_toolchain)
    _require_runtime_declaration(args.comparability, runtime)
    acquisition = _model_acquisition(args.model, args.model_uri, manifest_entry)
    configuration = _configuration(args.config_mode, args.config)
    input_manifest = _optional_repo_artifact(args.input_manifest)

    run_id = args.run_id or f"engraph-{uuid.uuid4()}"
    attempt_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    plan_record = {
        "schema": WRITE_SCHEMA,
        "record_type": "attempt_planned",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "timestamp": started_at,
        "state": "pending",
        "case_id": args.claim_id,
        "claim_kind": args.kind,
        "command_argv": safe_command_argv,
        "sensitivity": args.sensitivity,
        "process_policy": args.process_policy,
        "model": {
            **model,
            "uri": args.model_uri,
            "manifest_sha256": manifest_sha256,
            "object_identity": manifest_entry["object_identity"],
            "acquisition": acquisition,
        },
        "repository": repository,
        "dependencies": {
            "cargo_lock": _repo_artifact(ROOT / "Cargo.lock"),
            "cargo_toml": _repo_artifact(ROOT / "Cargo.toml"),
        },
        "configuration": configuration,
        "input_manifest": input_manifest,
        "runtime": runtime,
        "seed": args.seed,
        "settings": settings,
        "prompt_or_fixture": _optional_repo_artifact(args.prompt_or_fixture),
        "tool_binary": _optional_repo_artifact(args.tool_binary),
        "comparability_class": args.comparability,
    }
    plan_record = _v2_envelope(
        plan_record,
        event_id=str(uuid.uuid4()),
    )
    _write_exclusive_json(plan, plan_record)
    plan_sha256 = sha256(plan)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw_fd = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(raw_fd, "wb", closefd=True) as raw_file:
        try:
            process = subprocess.Popen(
                command_argv,
                cwd=ROOT,
                stdout=raw_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raw_file.write(
                (
                    json.dumps(
                        {
                            "runner_error": type(exc).__name__,
                            "message": "measured command could not be launched",
                        }
                    )
                    + "\n"
                ).encode()
            )
            exit_code = 126
            state = "failed"
        else:
            try:
                exit_code = process.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process.pid, process)
                exit_code = 124
                state = "timeout"
            else:
                _terminate_process_group(process.pid)
                state = "completed" if exit_code == 0 else "failed"
    ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if sha256(plan) != plan_sha256:
        raise ValueError("planned event changed during measured command")
    args.raw = raw
    args.command = " ".join(safe_command_argv)
    args.state = state
    args.exit_code = exit_code
    args.started_at = started_at
    args.ended_at = ended_at
    args.run_id = run_id
    args._attempt_id = attempt_id
    args._repository = repository
    generated_paths = (
        plan.relative_to(ROOT).as_posix(),
        raw.relative_to(ROOT).as_posix(),
    )
    args._repository_after = _repository_state(generated_paths)
    args._runtime = runtime
    args._runtime_after = _runtime_context(args.rust_toolchain)
    args._executed_by_tool = True
    args._command_argv = safe_command_argv
    args._planned_event = plan
    args._plan_record = plan_record
    return create_receipt(args)


def _terminal_error_status(errors: list[str]) -> str:
    """Separate absent external replay material from corrupted evidence."""
    if errors and all(error in TERMINAL_UNAVAILABLE_ERRORS for error in errors):
        return "TERMINAL_RESULT_UNAVAILABLE"
    return "TERMINAL_RESULT_INVALID"


def incomplete_attempts(
    directory: Path,
    *,
    attempt_id_filter: str | None = None,
    record_only: bool = False,
) -> list[dict[str, Any]]:
    resolved = directory.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("attempt directory must be inside repository") from exc
    planned: dict[str, list[tuple[Path, list[str]]]] = {}
    terminal: dict[str, list[tuple[Path, list[str]]]] = {}
    event_paths: dict[str, list[tuple[Path, str]]] = {}
    terminal_plan_required: dict[str, bool] = {}
    for path in sorted(resolved.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        attempt_id = value.get("attempt_id")
        if not isinstance(attempt_id, str):
            continue
        event_id = value.get("event_id")
        if value.get("schema") == SCHEMA_V2 and isinstance(event_id, str):
            event_paths.setdefault(event_id, []).append((path, attempt_id))
        if attempt_id_filter is not None and attempt_id != attempt_id_filter:
            continue
        if value.get("record_type") == "attempt_planned":
            errors = []
            if value.get("schema") == SCHEMA_V2:
                errors.extend(_verify_v2_envelope(value))
                if value.get("state") != "pending":
                    errors.append("planned event state is not pending")
                if value.get("parent_event_id") is not None:
                    errors.append("planned event unexpectedly has a parent")
            planned.setdefault(attempt_id, []).append((path, errors))
        elif value.get("record_type") == "attempt_result":
            receipt_errors = (
                verify_receipt_record(path)
                if record_only
                else verify_receipt(path)
            )
            terminal.setdefault(attempt_id, []).append((path, receipt_errors))
            terminal_plan_required[attempt_id] = (
                terminal_plan_required.get(attempt_id, False)
                or value.get("execution_provenance") == "tool_executed"
                or isinstance(value.get("parent_event_id"), str)
            )
    issues = []
    if attempt_id_filter is not None and not planned and not terminal:
        return [
            {
                "attempt_id": attempt_id_filter,
                "status": "ATTEMPT_NOT_FOUND",
            }
        ]
    for event_id, entries in sorted(event_paths.items()):
        collision_in_scope = (
            attempt_id_filter is None
            or any(attempt_id == attempt_id_filter for _, attempt_id in entries)
        )
        if len(entries) > 1 and collision_in_scope:
            issues.append(
                {
                    "event_id": event_id,
                    "paths": [
                        path.relative_to(ROOT).as_posix() for path, _ in entries
                    ],
                    "status": "AMBIGUOUS_DUPLICATE_EVENT_ID",
                }
            )
    for attempt_id, plan_paths in sorted(planned.items()):
        if len(plan_paths) != 1:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "plans": [
                        path.relative_to(ROOT).as_posix() for path, _ in plan_paths
                    ],
                    "status": "AMBIGUOUS_DUPLICATE_PLAN",
                }
            )
            continue
        plan_path, plan_errors = plan_paths[0]
        if plan_errors:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "plan": plan_path.relative_to(ROOT).as_posix(),
                    "status": "PLANNED_EVENT_INVALID",
                    "errors": plan_errors,
                }
            )
            continue
        results = terminal.get(attempt_id, [])
        if not results:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "plan": plan_path.relative_to(ROOT).as_posix(),
                    "status": "INCOMPLETE_NO_TERMINAL_RESULT",
                }
            )
            continue
        if len(results) != 1:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "plan": plan_path.relative_to(ROOT).as_posix(),
                    "terminals": [
                        path.relative_to(ROOT).as_posix() for path, _ in results
                    ],
                    "status": "AMBIGUOUS_DUPLICATE_TERMINAL",
                }
            )
            continue
        terminal_path, errors = results[0]
        if errors:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "plan": plan_path.relative_to(ROOT).as_posix(),
                    "terminal": terminal_path.relative_to(ROOT).as_posix(),
                    "status": _terminal_error_status(errors),
                    "errors": errors,
                }
            )
    for attempt_id, results in sorted(terminal.items()):
        if attempt_id not in planned:
            terminal_paths = [
                path.relative_to(ROOT).as_posix() for path, _ in results
            ]
            if len(results) != 1:
                issues.append(
                    {
                        "attempt_id": attempt_id,
                        "terminals": terminal_paths,
                        "status": "AMBIGUOUS_DUPLICATE_TERMINAL",
                    }
                )
            elif terminal_plan_required.get(attempt_id, False):
                issues.append(
                    {
                        "attempt_id": attempt_id,
                        "terminals": terminal_paths,
                        "status": "ORPHAN_TERMINAL_NO_PLAN",
                    }
                )
            elif results[0][1]:
                errors = results[0][1]
                issues.append(
                    {
                        "attempt_id": attempt_id,
                        "terminal": terminal_paths[0],
                        "status": _terminal_error_status(errors),
                        "errors": errors,
                    }
                )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    def common(run_parser: argparse.ArgumentParser) -> None:
        run_parser.add_argument("--claim-id", required=True)
        run_parser.add_argument(
            "--kind", choices=["correctness", "performance"], required=True
        )
        run_parser.add_argument("--raw", type=Path, required=True)
        run_parser.add_argument("--model", type=Path, required=True)
        run_parser.add_argument("--model-uri", required=True)
        run_parser.add_argument("--tokenizer", type=Path)
        run_parser.add_argument("--output", type=Path, required=True)
        run_parser.add_argument("--run-id")
        run_parser.add_argument(
            "--comparability",
            choices=sorted(COMPARABILITY_CLASSES),
            default="incomparable",
        )
        run_parser.add_argument("--seed", default="UNKNOWN")
        run_parser.add_argument("--rust-toolchain", required=True)
        run_parser.add_argument("--settings-json", default="{}")
        run_parser.add_argument(
            "--config-mode",
            choices=["builtin_default", "file"],
            required=True,
        )
        run_parser.add_argument("--config", type=Path)
        run_parser.add_argument("--input-manifest", type=Path)
        run_parser.add_argument(
            "--sensitivity",
            choices=["synthetic", "public_non_sensitive"],
            required=True,
        )
        run_parser.add_argument(
            "--process-policy",
            choices=[
                "group_contained",
                "source_reviewed_no_process_spawning",
            ],
            required=True,
        )
        run_parser.add_argument("--prompt-or-fixture", type=Path)
        run_parser.add_argument("--tool-binary", type=Path)
        run_parser.add_argument("--scorer", type=Path)
        run_parser.add_argument("--summary", type=Path)

    create = sub.add_parser("create")
    common(create)
    create.add_argument("--command", required=True)
    create.add_argument("--started-at", required=True)
    create.add_argument("--ended-at", required=True)
    create.add_argument(
        "--state", choices=sorted(TERMINAL_STATES), default="completed"
    )
    create.add_argument("--exit-code", type=int, required=True)
    run = sub.add_parser("run")
    common(run)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument("measured_command", nargs=argparse.REMAINDER)
    verify = sub.add_parser("verify")
    verify.add_argument("receipt", type=Path)
    verify_record = sub.add_parser("verify-record")
    verify_record.add_argument("receipt", type=Path)
    audit = sub.add_parser("audit-plans")
    audit.add_argument("directory", type=Path)
    audit.add_argument("--attempt-id")
    audit.add_argument(
        "--record-only",
        action="store_true",
        help="do not claim custody of unavailable external artifacts",
    )
    args = parser.parse_args()
    if args.action == "create":
        print(json.dumps(create_receipt(args), sort_keys=True))
    elif args.action == "run":
        print(json.dumps(run_measured(args), sort_keys=True))
    elif args.action == "verify":
        errors = verify_receipt(args.receipt)
        if errors:
            raise SystemExit("\n".join(errors))
        print("OK")
    elif args.action == "verify-record":
        errors = verify_receipt_record(args.receipt)
        if errors:
            raise SystemExit("\n".join(errors))
        print("OK: receipt record verified; external byte custody not claimed")
    else:
        issues = incomplete_attempts(
            args.directory,
            attempt_id_filter=args.attempt_id,
            record_only=args.record_only,
        )
        print(json.dumps(issues, indent=2, sort_keys=True))
        if issues:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
