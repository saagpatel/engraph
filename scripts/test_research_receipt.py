#!/usr/bin/env python3

import argparse
import tempfile
import unittest
from pathlib import Path

import research_receipt as rr


class ReceiptTests(unittest.TestCase):
    def test_no_clobber_and_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic measurement", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            args = argparse.Namespace(
                raw=raw,
                model=model,
                output=output,
                command="synthetic fixture",
                claim_id="fixture",
                kind="correctness",
                run_id="fixture-run",
                comparability="incomparable",
                seed="0",
                rust_toolchain="fixture",
                state="completed",
            )
            rr.create_receipt(args)
            self.assertEqual(rr.verify_receipt(output), [])
            with self.assertRaises(FileExistsError):
                rr.create_receipt(args)
            raw.write_text("mutated", encoding="utf-8")
            self.assertIn("raw output missing or hash-mismatched", rr.verify_receipt(output))

    def test_failed_receipt_is_preserved_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory(dir=rr.ROOT) as tmp:
            root = Path(tmp)
            raw = root / "raw.txt"
            model = root / "model.gguf"
            output = root / "receipt.json"
            raw.write_text("synthetic failure output", encoding="utf-8")
            model.write_bytes(b"synthetic model fixture")
            args = argparse.Namespace(
                raw=raw,
                model=model,
                output=output,
                command="synthetic failing fixture",
                claim_id="failure-fixture",
                kind="correctness",
                run_id="failure-fixture-run",
                comparability="incomparable",
                seed="0",
                rust_toolchain="fixture",
                state="failed",
            )
            receipt = rr.create_receipt(args)
            self.assertEqual(receipt["state"], "failed")
            self.assertEqual(rr.verify_receipt(output), [])


if __name__ == "__main__":
    unittest.main()
