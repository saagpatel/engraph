#!/usr/bin/env python3
"""Create and verify no-clobber engraph research receipts.

This tool never executes the measured command. It binds an already-produced raw
output to the current repository, lockfile, host, and exact local model bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "research-claim-run-manifest.v1"


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "UNKNOWN"


def create_receipt(args: argparse.Namespace) -> dict:
    raw = args.raw.resolve()
    model = args.model.resolve()
    output = args.output.resolve()
    if not raw.is_file() or not model.is_file():
        raise ValueError("raw output and model must already exist")
    if not args.command.strip():
        raise ValueError("measured command must be recorded")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite receipt: {output}")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    receipt = {
        "schema": SCHEMA,
        "record_type": "attempt_result",
        "run_id": args.run_id or f"engraph-{uuid.uuid4()}",
        "attempt_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "state": args.state,
        "case_id": args.claim_id,
        "claim_id": args.claim_id,
        "claim_kind": args.kind,
        "command": args.command,
        "raw_output_path": str(raw.relative_to(ROOT)),
        "raw_output_sha256": sha256(raw),
        "scorer_sha256": "NOT_APPLICABLE",
        "summary_sha256": "UNKNOWN",
        "prompt_or_fixture": (
            {
                "path": str(prompt_or_fixture.resolve().relative_to(ROOT)),
                "sha256": sha256(prompt_or_fixture.resolve()),
            }
            if (prompt_or_fixture := getattr(args, "prompt_or_fixture", None))
            else {"path": "UNKNOWN", "sha256": "UNKNOWN"}
        ),
        "tool_binary": (
            {
                "path": str(tool_binary.resolve().relative_to(ROOT)),
                "sha256": sha256(tool_binary.resolve()),
            }
            if (tool_binary := getattr(args, "tool_binary", None))
            else {"path": "UNKNOWN", "sha256": "UNKNOWN"}
        ),
        "model": {
            "path": str(model.relative_to(Path.home())),
            "artifact_sha256": sha256(model),
            "tokenizer_sha256": "UNKNOWN",
        },
        "repository": {
            "commit": git("rev-parse", "HEAD"),
            "branch": git("branch", "--show-current") or "UNKNOWN",
            "dirty": bool(status and status != "UNKNOWN"),
            "dirty_state_sha256": hashlib.sha256(status.encode()).hexdigest(),
        },
        "dependencies": {
            "cargo_lock_sha256": sha256(ROOT / "Cargo.lock"),
        },
        "runtime": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "hardware": platform.platform(),
            "rust_toolchain": args.rust_toolchain,
        },
        "comparability_class": args.comparability,
        "seed": args.seed,
        "settings": {"temperature": "NOT_APPLICABLE"},
        "license_conclusion": "NOT_ASSERTED",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(output, flags, 0o644)
    try:
        os.write(fd, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return receipt


def verify_receipt(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    if data.get("schema") != SCHEMA:
        errors.append("unsupported schema")
    if data.get("state") not in {"completed", "failed", "aborted", "null"}:
        errors.append("receipt has unsupported terminal state")
    required = {
        "record_type",
        "run_id",
        "attempt_id",
        "timestamp",
        "state",
        "case_id",
        "raw_output_sha256",
        "scorer_sha256",
        "summary_sha256",
        "comparability_class",
    }
    if missing := sorted(required - data.keys()):
        errors.append(f"missing required fields: {', '.join(missing)}")
    raw = ROOT / data.get("raw_output_path", "")
    if not raw.is_file() or sha256(raw) != data.get("raw_output_sha256"):
        errors.append("raw output missing or hash-mismatched")
    model_info = data.get("model") or {}
    model = Path.home() / model_info.get("path", "")
    if not model.is_file() or sha256(model) != model_info.get("artifact_sha256"):
        errors.append("model missing or hash-mismatched")
    if sha256(ROOT / "Cargo.lock") != (data.get("dependencies") or {}).get(
        "cargo_lock_sha256"
    ):
        errors.append("Cargo.lock hash mismatch")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create")
    create.add_argument("--claim-id", required=True)
    create.add_argument("--kind", choices=["correctness", "performance"], required=True)
    create.add_argument("--raw", type=Path, required=True)
    create.add_argument("--model", type=Path, required=True)
    create.add_argument("--command", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--run-id")
    create.add_argument("--comparability", default="incomparable")
    create.add_argument("--seed", default="UNKNOWN")
    create.add_argument("--rust-toolchain", default="UNKNOWN")
    create.add_argument(
        "--state",
        choices=["completed", "failed", "aborted", "null"],
        default="completed",
    )
    create.add_argument("--prompt-or-fixture", type=Path)
    create.add_argument("--tool-binary", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("receipt", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        print(json.dumps(create_receipt(args), sort_keys=True))
    else:
        errors = verify_receipt(args.receipt)
        if errors:
            raise SystemExit("\n".join(errors))
        print("OK")


if __name__ == "__main__":
    main()
