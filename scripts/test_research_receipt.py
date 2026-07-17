#!/usr/bin/env python3

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research_receipt as rr


class ReceiptTests(unittest.TestCase):
    def test_v2_verifier_rejects_rights_and_artifact_overclaims(self) -> None:
        representatives = json.loads(
            (
                rr.ROOT
                / "research-evidence/contracts/v2/representative-records.json"
            ).read_text(encoding="utf-8")
        )["records"]
        record = next(
            row for row in representatives
            if row["producer"]["system"] == "ENGRAPH"
        )
        record["state"] = record["outcome"]["state"]
        self.assertEqual(rr._verify_v2_envelope(record), [])
        rights_overclaim = json.loads(json.dumps(record))
        rights_overclaim["rights"]["declared_license"]["status"] = "DECLARED"
        self.assertTrue(rr._verify_v2_envelope(rights_overclaim))
        invalid_digest = json.loads(json.dumps(record))
        invalid_digest["evidence"]["raw_output"]["sha256"] = "z" * 64
        self.assertTrue(rr._verify_v2_envelope(invalid_digest))

    def _args(
        self,
        *,
        raw: Path,
        model: Path,
        output: Path,
        state: str,
        exit_code: int,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            raw=raw,
            model=model,
            model_uri="hf:fixture/model/model.gguf",
            tokenizer=None,
            output=output,
            command="synthetic fixture",
            claim_id="fixture",
            kind="correctness",
            run_id="fixture-run",
            comparability="incomparable",
            seed="0",
            rust_toolchain="rustc fixture",
            started_at="2026-07-17T00:00:00Z",
            ended_at="2026-07-17T00:00:01Z",
            settings_json='{"passes": 1}',
            config_mode="builtin_default",
            config=None,
            input_manifest=None,
            sensitivity="synthetic",
            process_policy="group_contained",
            state=state,
            exit_code=exit_code,
            prompt_or_fixture=None,
            tool_binary=None,
            scorer=None,
            summary=None,
        )

    def _manifest_entry(self, model: Path) -> tuple[dict, str]:
        digest = rr.sha256(model)
        return (
            {
                "artifact_sha256": digest,
                "object_identity": f"sha256:{digest}",
                "role": "fixture",
                "base_model": "fixture/base",
                "upstream_revision": "fixture-revision",
                "download_url": (
                    "https://huggingface.co/fixture/model/resolve/"
                    "fixture-revision/model.gguf"
                ),
                "tokenizer_identity": "embedded fixture",
                "declared_license": "UNKNOWN",
                "license_evidence_url": "UNKNOWN",
                "acquisition_timestamp": "UNKNOWN",
                "verified_at": "2026-07-17",
            },
            "f" * 64,
        )

    def test_command_evidence_rejects_secrets_without_false_tokenizer_match(
        self,
    ) -> None:
        self.assertEqual(
            rr._safe_command_argv(["tool", "--tokenizer", "embedded"]),
            ["tool", "--tokenizer", "embedded"],
        )
        self.assertEqual(
            rr._safe_command_argv(["tool", "--input=/Users/d/private/file"])[1],
            "--input=$HOME/private/file",
        )
        with self.assertRaisesRegex(ValueError, "secret-bearing"):
            rr._safe_command_argv(["tool", "--token", "private-value"])

    def test_no_clobber_and_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic measurement", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=raw,
                model=model,
                output=output,
                state="completed",
                exit_code=0,
            )
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                rr.create_receipt(args)
                self.assertEqual(rr.verify_receipt(output), [])
                with self.assertRaises(FileExistsError):
                    rr.create_receipt(args)
            raw.write_text("mutated", encoding="utf-8")
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                self.assertIn("raw output size mismatch", rr.verify_receipt(output))

    def test_acquisition_receipt_is_bound_and_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic measurement", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            manifest_entry, manifest_digest = self._manifest_entry(model)
            sidecar = Path(f"{model}.provenance.json")
            sidecar.write_text(
                __import__("json").dumps(
                    {
                        "schema": "engraph-model-acquisition.v1",
                        "uri": "hf:fixture/model/model.gguf",
                        "download_url": manifest_entry["download_url"],
                        "artifact_sha256": manifest_entry["artifact_sha256"],
                        "object_identity": manifest_entry["object_identity"],
                        "upstream_revision": manifest_entry["upstream_revision"],
                        "base_model": manifest_entry["base_model"],
                        "tokenizer_identity": manifest_entry["tokenizer_identity"],
                        "declared_license": manifest_entry["declared_license"],
                        "license_evidence_url": manifest_entry["license_evidence_url"],
                        "acquired_at_unix": 1,
                        "license_conclusion": "NOT_ASSERTED",
                        "redistribution_status": "UNKNOWN",
                    }
                ),
                encoding="utf-8",
            )
            args = self._args(
                raw=raw,
                model=model,
                output=output,
                state="completed",
                exit_code=0,
            )
            with mock.patch.object(
                rr,
                "_model_manifest_entry",
                return_value=(manifest_entry, manifest_digest),
            ):
                receipt = rr.create_receipt(args)
                self.assertEqual(
                    receipt["model"]["acquisition"]["status"],
                    "LOCAL_SIDECAR_BOUND_UNAUTHENTICATED",
                )
                self.assertEqual(rr.verify_receipt(output), [])
                sidecar.write_text("{}", encoding="utf-8")
                self.assertIn(
                    "model acquisition receipt missing or hash-mismatched",
                    rr.verify_receipt(output),
                )

    def test_failed_receipt_is_preserved_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic failure output", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=raw,
                model=model,
                output=output,
                state="failed",
                exit_code=1,
            )
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                receipt = rr.create_receipt(args)
                self.assertEqual(receipt["state"], "failed")
                self.assertEqual(rr.verify_receipt(output), [])

    def test_receipt_metadata_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic measurement", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=raw,
                model=model,
                output=output,
                state="completed",
                exit_code=0,
            )
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                rr.create_receipt(args)
                data = __import__("json").loads(output.read_text(encoding="utf-8"))
                data["command"] = "different command"
                output.write_text(__import__("json").dumps(data), encoding="utf-8")
                self.assertIn("receipt digest mismatch", rr.verify_receipt(output))

    def test_same_manifest_attempt_rejects_dirty_repository(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic measurement", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=raw,
                model=model,
                output=output,
                state="completed",
                exit_code=0,
            )
            args.comparability = "same_manifest_attempt"
            args.process_policy = "source_reviewed_no_process_spawning"
            args._executed_by_tool = True
            with (
                mock.patch.object(
                    rr,
                    "_model_manifest_entry",
                    return_value=self._manifest_entry(model),
                ),
                mock.patch.object(
                    rr,
                    "_dirty_context",
                    return_value={
                        "dirty": True,
                        "dirty_state_sha256": "1" * 64,
                        "tracked_diff_sha256": "2" * 64,
                        "tracked_diff_bytes": 1,
                        "untracked": [],
                        "replay_material": "digest_only_not_reconstructable",
                    },
                ),
            ):
                with self.assertRaisesRegex(ValueError, "clean repository"):
                    rr.create_receipt(args)

    def test_run_records_plan_raw_output_and_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            model = root / "model.gguf"
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=root / "raw.txt",
                model=model,
                output=root / "receipt.json",
                state="completed",
                exit_code=0,
            )
            args.plan = root / "planned.json"
            args.timeout_seconds = 5
            tool = root / "synthetic-tool"
            tool.write_text(
                f"#!{sys.executable}\nprint('synthetic correctness path')\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            args.tool_binary = tool
            args.measured_command = [
                "--",
                str(tool),
            ]
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                receipt = rr.run_measured(args)
                plan_record = json.loads(args.plan.read_text(encoding="utf-8"))
                self.assertEqual(receipt["state"], "completed")
                self.assertEqual(receipt["execution_provenance"], "tool_executed")
                self.assertEqual(plan_record["schema"], rr.SCHEMA_V2)
                self.assertEqual(receipt["schema"], rr.SCHEMA_V2)
                self.assertEqual(
                    receipt["contract"]["sha256"], rr.V2_CONTRACT_SHA256
                )
                self.assertNotEqual(plan_record["event_id"], receipt["event_id"])
                self.assertEqual(
                    receipt["parent_event_id"], plan_record["event_id"]
                )
                for field in ("producer", "run_id", "attempt_id", "case_id"):
                    self.assertEqual(plan_record[field], receipt[field])
                self.assertTrue(args.plan.is_file())
                self.assertEqual(
                    receipt["planned_event"]["sha256"], rr.sha256(args.plan)
                )
                self.assertEqual(rr.verify_receipt(args.output), [])
                validator = shutil.which("jsonschema")
                if validator:
                    schema = (
                        rr.ROOT
                        / "research-evidence/contracts/v2/"
                        "research-claim-run-manifest-v2.schema.json"
                    )
                    for path in (args.plan, args.output):
                        with self.subTest(v2_schema=path.name):
                            validation = subprocess.run(
                                [validator, "-i", str(path), str(schema)],
                                check=False,
                                capture_output=True,
                                text=True,
                            )
                            self.assertEqual(
                                validation.returncode, 0, validation.stderr
                            )
                forged = json.loads(args.output.read_text(encoding="utf-8"))
                forged["parent_event_id"] = "unrelated-event"
                forged["receipt_sha256"] = rr.canonical_digest(
                    {
                        key: value
                        for key, value in forged.items()
                        if key != "receipt_sha256"
                    }
                )
                args.output.write_text(json.dumps(forged), encoding="utf-8")
                self.assertIn(
                    "result parent does not reference planned event",
                    rr.verify_receipt(args.output),
                )

    def test_same_manifest_attempt_succeeds_with_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            model = root / "model.gguf"
            model.write_bytes(b"synthetic model fixture")
            fixture = root / "fixture.txt"
            fixture.write_text("synthetic input", encoding="utf-8")
            tool = root / "synthetic-tool"
            tool.write_text(
                f"#!{sys.executable}\nprint('synthetic correctness path')\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            args = self._args(
                raw=root / "raw.txt",
                model=model,
                output=root / "receipt.json",
                state="completed",
                exit_code=0,
            )
            args.plan = root / "planned.json"
            args.timeout_seconds = 5
            args.prompt_or_fixture = fixture
            args.tool_binary = tool
            args.measured_command = ["--", str(tool)]
            args.comparability = "same_manifest_attempt"
            args.process_policy = "source_reviewed_no_process_spawning"
            clean_repository = {
                "dirty": False,
                "dirty_state_sha256": "1" * 64,
                "tracked_diff_sha256": "2" * 64,
                "tracked_diff_bytes": 0,
                "untracked": [],
                "replay_material": "clean_commit",
                "commit": "3" * 40,
                "branch": "fixture",
            }
            stable_runtime = {
                "os": "fixture",
                "os_release": "fixture",
                "architecture": "fixture",
                "hardware": {
                    "platform": "fixture",
                    "processor": "fixture",
                    "logical_cpu_count": 1,
                    "memory_bytes": "1",
                    "gpu_display_identity": "sha256:" + "4" * 64,
                    "load_average_1m_5m_15m": [0.0, 0.0, 0.0],
                },
                "python": "fixture",
                "rust_toolchain_declared": "fixture",
                "rustc": "fixture",
                "cargo": "fixture",
                "cmake": "fixture",
                "research_environment": {"T1_PASSES": "UNSET"},
                "environment_capture": "ALLOWLIST_ONLY_NO_SECRETS",
            }
            with (
                mock.patch.object(
                    rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
                ),
                mock.patch.object(
                    rr,
                    "_repository_state",
                    return_value=clean_repository,
                ),
                mock.patch.object(
                    rr,
                    "_runtime_context",
                    return_value=stable_runtime,
                ),
            ):
                receipt = rr.run_measured(args)
                self.assertEqual(
                    receipt["comparability_class"], "same_manifest_attempt"
                )
                self.assertEqual(rr.verify_receipt(args.output), [])

    def test_run_records_launch_failure_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            model = root / "model.gguf"
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=root / "raw.txt",
                model=model,
                output=root / "receipt.json",
                state="completed",
                exit_code=0,
            )
            args.plan = root / "planned.json"
            args.timeout_seconds = 5
            tool = root / "invalid-tool"
            tool.write_bytes(b"not an executable format")
            tool.chmod(0o700)
            args.tool_binary = tool
            args.measured_command = ["--", str(tool)]
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                receipt = rr.run_measured(args)
                self.assertEqual(receipt["state"], "failed")
                self.assertEqual(receipt["exit_code"], 126)
                self.assertEqual(rr.verify_receipt(args.output), [])

    def test_cleanup_failure_leaves_attempt_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            model = root / "model.gguf"
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=root / "raw.txt",
                model=model,
                output=root / "receipt.json",
                state="completed",
                exit_code=0,
            )
            args.plan = root / "planned.json"
            args.timeout_seconds = 5
            tool = root / "synthetic-tool"
            tool.write_text(
                f"#!{sys.executable}\nprint('synthetic correctness path')\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            args.tool_binary = tool
            args.measured_command = ["--", str(tool)]
            with (
                mock.patch.object(
                    rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
                ),
                mock.patch.object(
                    rr,
                    "_terminate_process_group",
                    side_effect=RuntimeError("group survived"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "group survived"):
                    rr.run_measured(args)
            self.assertTrue(args.plan.is_file())
            self.assertTrue(args.raw.is_file())
            self.assertFalse(args.output.exists())
            self.assertEqual(len(rr.incomplete_attempts(root)), 1)

    def test_run_records_timeout_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            model = root / "model.gguf"
            model.write_bytes(b"synthetic model fixture")
            args = self._args(
                raw=root / "raw.txt",
                model=model,
                output=root / "receipt.json",
                state="completed",
                exit_code=0,
            )
            args.plan = root / "planned.json"
            args.timeout_seconds = 0.01
            tool = root / "slow-tool"
            tool.write_text(
                f"#!{sys.executable}\nimport time\ntime.sleep(2)\n",
                encoding="utf-8",
            )
            tool.chmod(0o700)
            args.tool_binary = tool
            args.measured_command = ["--", str(tool)]
            with mock.patch.object(
                rr, "_model_manifest_entry", return_value=self._manifest_entry(model)
            ):
                receipt = rr.run_measured(args)
                self.assertEqual(receipt["state"], "timeout")
                self.assertEqual(receipt["exit_code"], 124)
                self.assertEqual(rr.verify_receipt(args.output), [])

    def test_incomplete_attempt_inventory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            plan = root / "orphan.json"
            plan.write_text(
                json.dumps(
                    {
                        "record_type": "attempt_planned",
                        "attempt_id": "orphan-attempt",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                rr.incomplete_attempts(root),
                [
                    {
                        "attempt_id": "orphan-attempt",
                        "plan": plan.relative_to(rr.ROOT).as_posix(),
                        "status": "INCOMPLETE_NO_TERMINAL_RESULT",
                    }
                ],
            )
            (root / "forged-result.json").write_text(
                json.dumps(
                    {
                        "record_type": "attempt_result",
                        "attempt_id": "orphan-attempt",
                    }
                ),
                encoding="utf-8",
            )
            issues = rr.incomplete_attempts(root)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["status"], "TERMINAL_RESULT_INVALID")
            self.assertEqual(
                issues[0]["terminal"],
                (root / "forged-result.json").relative_to(rr.ROOT).as_posix(),
            )
            self.assertTrue(issues[0]["errors"])

    def test_attempt_inventory_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            for name in ("plan-a.json", "plan-b.json"):
                (root / name).write_text(
                    json.dumps(
                        {
                            "record_type": "attempt_planned",
                            "attempt_id": "duplicate-attempt",
                        }
                    ),
                    encoding="utf-8",
                )
            issues = rr.incomplete_attempts(root)
            self.assertEqual(issues[0]["status"], "AMBIGUOUS_DUPLICATE_PLAN")
            self.assertEqual(len(issues[0]["plans"]), 2)

            (root / "plan-b.json").unlink()
            for name in ("result-a.json", "result-b.json"):
                (root / name).write_text(
                    json.dumps(
                        {
                            "record_type": "attempt_result",
                            "attempt_id": "duplicate-attempt",
                        }
                    ),
                    encoding="utf-8",
                )
            issues = rr.incomplete_attempts(root)
            self.assertEqual(issues[0]["status"], "AMBIGUOUS_DUPLICATE_TERMINAL")
            self.assertEqual(len(issues[0]["terminals"]), 2)


if __name__ == "__main__":
    unittest.main()
