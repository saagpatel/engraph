import importlib.util
import unittest
from pathlib import Path
from unittest import mock

import verify_model_provenance

CONTRACT_VERIFIER_PATH = (
    Path(__file__).resolve().parents[1]
    / "research-evidence"
    / "contracts"
    / "v2"
    / "verify_contract.py"
)
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "engraph_contract_v2_verifier", CONTRACT_VERIFIER_PATH
)
assert CONTRACT_SPEC is not None and CONTRACT_SPEC.loader is not None
contract_v2 = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(contract_v2)


class VerifyModelProvenanceTests(unittest.TestCase):
    def test_shared_v2_contract_semantics(self):
        with (
            mock.patch.object(contract_v2, "_validate_with_reference_cli"),
            mock.patch.object(contract_v2, "_require_reference_rejection"),
        ):
            contract_v2.verify()

    def test_repository_contracts_verify_offline(self):
        verify_model_provenance.verify()

    def test_canonical_hash_has_no_trailing_newline(self):
        self.assertEqual(
            verify_model_provenance._sha256_canonical({"b": 2, "a": 1}),
            "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
        )

    def test_hex_validation_is_lowercase_and_exact_length(self):
        self.assertTrue(verify_model_provenance._is_hex("a" * 40, 40))
        self.assertFalse(verify_model_provenance._is_hex("A" * 40, 40))
        self.assertFalse(verify_model_provenance._is_hex("a" * 39, 40))

    def test_semantic_gate_remains_active_under_optimized_python(self):
        invalid = {
            "schema": "wrong",
            "claim_boundary": "",
            "models": [],
        }
        with mock.patch.object(
            verify_model_provenance,
            "_load",
            side_effect=[
                invalid,
                {"schema": "wrong", "claim_boundary": "", "receipts": []},
                {},
            ],
        ):
            with self.assertRaisesRegex(ValueError, "unsupported model manifest schema"):
                verify_model_provenance.verify()

    def test_shared_v2_gate_is_not_an_optimized_python_assertion(self):
        with self.assertRaisesRegex(ValueError, "synthetic fail-closed gate"):
            contract_v2._require(False, "synthetic fail-closed gate")


if __name__ == "__main__":
    unittest.main()
