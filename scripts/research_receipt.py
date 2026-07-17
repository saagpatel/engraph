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
import signal
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "research-claim-run-manifest.v1"
MODEL_MANIFEST = ROOT / "models/model-manifest-v1.json"
TERMINAL_STATES = {"completed", "failed", "timeout", "aborted", "null"}
COMPARABILITY_CLASSES = {
    "same_manifest_attempt",
    "hardware_variant",
    "runtime_variant",
    "incomparable",
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
        "schema": SCHEMA,
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
    if not path.is_file() or path.is_symlink():
        errors.append(f"{label} missing or not a regular file")
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
    if not path.is_file() or path.is_symlink():
        errors.append(f"{label} missing or not a regular file")
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
    return [] if actual == expected else ["planned event inputs differ from result"]


def verify_receipt(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"receipt unreadable: {type(exc).__name__}"]
    errors = []
    if data.get("schema") != SCHEMA:
        errors.append("unsupported schema")
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
    if (
        model_path is None
        or not model_path.is_file()
        or model_path.is_symlink()
        or model_path.stat().st_size != model_info.get("bytes")
        or sha256(model_path) != model_info.get("sha256")
    ):
        errors.append("model missing or hash-mismatched")
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
            if (
                not acquisition_path.is_file()
                or acquisition_path.is_symlink()
                or acquisition_path.stat().st_size != artifact.get("bytes")
                or sha256(acquisition_path) != artifact.get("sha256")
            ):
                errors.append("model acquisition receipt missing or hash-mismatched")
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
            if (
                not token_path.is_file()
                or token_path.is_symlink()
                or token_path.stat().st_size != tokenizer.get("bytes")
                or sha256(token_path) != tokenizer.get("sha256")
            ):
                errors.append("external tokenizer missing or hash-mismatched")
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
    if data.get("comparability_class") == "same_manifest_attempt" and _stable_runtime(
        runtime_after
    ) != _stable_runtime(runtime):
        errors.append("same-manifest receipt changed stable runtime identity")
    return errors


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
    acquisition = _model_acquisition(args.model, args.model_uri, manifest_entry)
    configuration = _configuration(args.config_mode, args.config)
    input_manifest = _optional_repo_artifact(args.input_manifest)

    run_id = args.run_id or f"engraph-{uuid.uuid4()}"
    attempt_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    plan_record = {
        "schema": SCHEMA,
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


def incomplete_attempts(directory: Path) -> list[dict[str, Any]]:
    resolved = directory.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("attempt directory must be inside repository") from exc
    planned: dict[str, list[Path]] = {}
    terminal: dict[str, list[tuple[Path, list[str]]]] = {}
    for path in sorted(resolved.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        attempt_id = value.get("attempt_id")
        if not isinstance(attempt_id, str):
            continue
        if value.get("record_type") == "attempt_planned":
            planned.setdefault(attempt_id, []).append(path)
        elif value.get("record_type") == "attempt_result":
            terminal.setdefault(attempt_id, []).append((path, verify_receipt(path)))
    issues = []
    for attempt_id, plan_paths in sorted(planned.items()):
        if len(plan_paths) != 1:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "plans": [
                        path.relative_to(ROOT).as_posix() for path in plan_paths
                    ],
                    "status": "AMBIGUOUS_DUPLICATE_PLAN",
                }
            )
            continue
        plan_path = plan_paths[0]
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
                    "status": "TERMINAL_RESULT_INVALID",
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
    audit = sub.add_parser("audit-plans")
    audit.add_argument("directory", type=Path)
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
    else:
        issues = incomplete_attempts(args.directory)
        print(json.dumps(issues, indent=2, sort_keys=True))
        if issues:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
