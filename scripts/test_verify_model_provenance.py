import unittest
from unittest import mock

import verify_model_provenance


class VerifyModelProvenanceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
